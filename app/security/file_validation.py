"""Content-based validation for MosaicParse inputs."""

from __future__ import annotations

import json
import posixpath
import re
import shutil
import subprocess
import unicodedata
import warnings
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote
from xml.etree import ElementTree

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
VIDEO_MIME_TYPES = {
    "video/mp4",
    "video/quicktime",
    "video/x-matroska",
    "video/webm",
    "video/x-msvideo",
}
IMAGE_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/tiff",
    "image/bmp",
}
SUPPORTED_MIME_TYPES = (
    {"application/pdf", DOCX_MIME, PPTX_MIME} | IMAGE_MIME_TYPES | VIDEO_MIME_TYPES
)

MIME_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "application/pdf": (".pdf",),
    DOCX_MIME: (".docx",),
    PPTX_MIME: (".pptx",),
    "image/png": (".png",),
    "image/jpeg": (".jpg", ".jpeg"),
    "image/webp": (".webp",),
    "image/tiff": (".tif", ".tiff"),
    "image/bmp": (".bmp",),
    "video/mp4": (".mp4",),
    "video/quicktime": (".mov",),
    "video/x-matroska": (".mkv",),
    "video/webm": (".webm",),
    "video/x-msvideo": (".avi",),
}

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_UNSAFE_RE = re.compile(r"[\\/:*?\"<>|]+")
_SLIDE_RE = re.compile(r"^ppt/slides/slide\d+\.xml$")
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class FileValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def safe_filename(filename: str | None, *, fallback: str = "content", max_length: int = 180) -> str:
    """Return a basename safe for local storage and Content-Disposition."""

    name = unicodedata.normalize("NFKC", filename or "")
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = _CONTROL_RE.sub("", name)
    name = _UNSAFE_RE.sub("_", name).strip(" .")
    if not name or name in {".", ".."}:
        name = fallback
    path = Path(name)
    stem = path.stem.strip(" .") or fallback
    suffix = path.suffix.lower()[:16]
    if stem.upper() in _WINDOWS_RESERVED:
        stem = f"_{stem}"
    allowed_stem = max(1, max_length - len(suffix))
    return f"{stem[:allowed_stem]}{suffix}"


def detect_mime_type(header: bytes, *, suffix: str = "") -> str | None:
    """Detect non-OOXML formats from magic bytes; never trusts request headers."""

    suffix = suffix.lower()
    if header.startswith(b"%PDF-"):
        return "application/pdf"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(header) >= 4 and header[:4] in {b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"}:
        return "image/tiff"
    if header.startswith(b"BM"):
        return "image/bmp"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"AVI ":
        return "video/x-msvideo"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        return "video/quicktime" if suffix == ".mov" or b"qt  " in header else "video/mp4"
    if header.startswith(b"\x1aE\xdf\xa3"):
        return (
            "video/webm" if suffix == ".webm" or b"webm" in header.lower() else "video/x-matroska"
        )
    return None


def _safe_zip_members(
    archive: zipfile.ZipFile,
    *,
    max_entries: int = 10_000,
    max_entry_bytes: int = 50 * 1024 * 1024,
    max_uncompressed_bytes: int = 400 * 1024 * 1024,
    max_compression_ratio: float = 100.0,
) -> list[zipfile.ZipInfo]:
    members = archive.infolist()
    if len(members) > max_entries:
        raise FileValidationError("archive_too_many_entries", "OOXML archive has too many entries")
    total = 0
    for member in members:
        path = PurePosixPath(member.filename)
        if path.is_absolute() or ".." in path.parts or "\\" in member.filename:
            raise FileValidationError(
                "archive_path_traversal", "OOXML archive contains unsafe paths"
            )
        if member.flag_bits & 0x1:
            raise FileValidationError(
                "archive_encrypted_entry", "encrypted OOXML entries are not supported"
            )
        if member.file_size > max_entry_bytes:
            raise FileValidationError(
                "archive_entry_too_large", "OOXML entry exceeds the per-entry safety limit"
            )
        total += member.file_size
        if total > max_uncompressed_bytes:
            raise FileValidationError(
                "archive_too_large", "OOXML expanded size exceeds safety limit"
            )
        compressed = max(1, member.compress_size)
        if member.file_size > 1024 * 1024 and member.file_size / compressed > max_compression_ratio:
            raise FileValidationError(
                "archive_suspicious_ratio", "OOXML entry compression ratio is unsafe"
            )
    return members


def inspect_ooxml(path: str | Path) -> tuple[str, int]:
    """Return the supported OOXML MIME and stable unit count."""

    try:
        with zipfile.ZipFile(path) as archive:
            members = _safe_zip_members(archive)
            names = {member.filename for member in members}
            if "[Content_Types].xml" not in names:
                raise FileValidationError("invalid_ooxml", "OOXML content types are missing")
            content_types = archive.read("[Content_Types].xml")
            if len(content_types) > 2 * 1024 * 1024:
                raise FileValidationError("invalid_ooxml", "OOXML content types are oversized")
            try:
                content_type_root = ElementTree.fromstring(content_types)
            except ElementTree.ParseError as exc:
                raise FileValidationError(
                    "invalid_ooxml", "OOXML content types are malformed"
                ) from exc
            declared_types = {
                element.attrib.get("ContentType", "") for element in content_type_root
            }
            for relationship_name in (name for name in names if name.endswith(".rels")):
                payload = archive.read(relationship_name)
                if len(payload) > 2 * 1024 * 1024:
                    raise FileValidationError("invalid_ooxml", "OOXML relationships are oversized")
                try:
                    root = ElementTree.fromstring(payload)
                except ElementTree.ParseError as exc:
                    raise FileValidationError(
                        "invalid_ooxml", "OOXML relationships are malformed"
                    ) from exc
                for relation in root:
                    if relation.attrib.get("TargetMode", "").casefold() == "external":
                        raise FileValidationError(
                            "ooxml_external_relationship",
                            "external OOXML relationships are not supported",
                        )
                    target = relation.attrib.get("Target", "")
                    # URI escaping must not disguise traversal. Decode twice to
                    # catch common nested encodings while retaining a finite bound.
                    target = unquote(unquote(target))
                    if "\x00" in target:
                        raise FileValidationError(
                            "archive_path_traversal",
                            "OOXML relationship contains an unsafe target",
                        )
                    target_path = PurePosixPath(target.replace("\\", "/"))
                    relationship_path = PurePosixPath(relationship_name)
                    relationship_parts = relationship_path.parts
                    try:
                        relationships_index = relationship_parts.index("_rels")
                    except ValueError:
                        relationships_index = len(relationship_parts) - 1
                    owner_directory = "/".join(relationship_parts[:relationships_index])
                    normalized_target = posixpath.normpath(
                        posixpath.join(owner_directory, target_path.as_posix())
                    )
                    if (
                        target_path.is_absolute()
                        or normalized_target == ".."
                        or normalized_target.startswith("../")
                    ):
                        raise FileValidationError(
                            "archive_path_traversal",
                            "OOXML relationship contains an unsafe target",
                        )
            if (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
                in declared_types
                and "word/document.xml" in names
            ):
                return DOCX_MIME, 1
            if (
                "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
                in declared_types
                and "ppt/presentation.xml" in names
            ):
                slide_count = sum(bool(_SLIDE_RE.fullmatch(name)) for name in names)
                if slide_count < 1:
                    raise FileValidationError("empty_document", "presentation contains no slides")
                return PPTX_MIME, slide_count
    except FileValidationError:
        raise
    except (OSError, zipfile.BadZipFile, KeyError, RuntimeError) as exc:
        raise FileValidationError(
            "invalid_ooxml", "OOXML document is corrupt or unreadable"
        ) from exc
    raise FileValidationError("archive_not_supported", "archive is not a supported DOCX or PPTX")


def validate_file_header(
    filename: str | None, header: bytes, *, source_path: str | Path | None = None
) -> tuple[str, str]:
    cleaned = safe_filename(filename)
    suffix = Path(cleaned).suffix.lower()
    mime_type: str | None
    if header.startswith(b"PK\x03\x04"):
        if source_path is None:
            raise FileValidationError(
                "archive_not_supported", "OOXML validation requires the complete file"
            )
        mime_type, _ = inspect_ooxml(source_path)
    else:
        mime_type = detect_mime_type(header, suffix=suffix)
    if mime_type is None:
        if header.startswith((b"Rar!", b"7z\xbc\xaf\x27\x1c", b"\x1f\x8b")):
            raise FileValidationError("archive_not_supported", "archives are not supported")
        raise FileValidationError(
            "unsupported_file_type", "file content is not a supported content type"
        )

    allowed_suffixes = MIME_EXTENSIONS[mime_type]
    if suffix and suffix not in allowed_suffixes:
        raise FileValidationError(
            "file_type_mismatch",
            f"filename extension {suffix!r} does not match detected MIME type {mime_type}",
        )
    if not suffix:
        cleaned += allowed_suffixes[0]
    return cleaned, mime_type


def _inspect_pdf(path: Path) -> int:
    try:
        try:
            import pymupdf
        except ImportError:
            import fitz as pymupdf  # type: ignore[no-redef]
        with pymupdf.open(path) as document:
            if document.needs_pass:
                raise FileValidationError(
                    "encrypted_pdf", "password-protected PDFs are not supported"
                )
            count = int(document.page_count)
    except FileValidationError:
        raise
    except ImportError as exc:
        raise FileValidationError(
            "pdf_inspector_unavailable", "PyMuPDF is required to validate PDF page count"
        ) from exc
    except Exception as exc:
        raise FileValidationError("invalid_pdf", "PDF is corrupt or otherwise unreadable") from exc
    if count < 1:
        raise FileValidationError("empty_document", "document contains no pages")
    return count


def _inspect_video(
    path: Path, *, max_video_seconds: int, max_video_frame_pixels: int
) -> None:
    if shutil.which("ffprobe") is None:
        raise FileValidationError("video_probe_unavailable", "ffprobe is required for video inputs")
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=index,width,height:format=duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise FileValidationError(
            "video_probe_timeout", "video probing exceeded 30 seconds"
        ) from exc
    if completed.returncode != 0:
        raise FileValidationError(
            "invalid_video", "video is corrupt or uses an unreadable container"
        )
    try:
        payload = json.loads(completed.stdout)
        duration = float(payload.get("format", {}).get("duration", 0))
        streams = payload.get("streams", [])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise FileValidationError("invalid_video", "ffprobe returned invalid metadata") from exc
    if not streams:
        raise FileValidationError("video_stream_missing", "input contains no video stream")
    try:
        width = int(streams[0].get("width", 0))
        height = int(streams[0].get("height", 0))
    except (TypeError, ValueError, AttributeError) as exc:
        raise FileValidationError("invalid_video", "video dimensions are invalid") from exc
    if width <= 0 or height <= 0:
        raise FileValidationError("invalid_video", "video dimensions are missing or invalid")
    if width * height > max_video_frame_pixels:
        raise FileValidationError(
            "video_frame_too_large",
            f"video frame exceeds maximum pixel count of {max_video_frame_pixels}",
        )
    if duration <= 0:
        raise FileValidationError("invalid_video_duration", "video duration is missing or invalid")
    if duration > max_video_seconds:
        raise FileValidationError(
            "video_too_long", f"video exceeds maximum duration of {max_video_seconds} seconds"
        )


def inspect_unit_count(
    path: str | Path,
    mime_type: str,
    *,
    max_video_seconds: int = 1_800,
    max_video_frame_pixels: int = 33_177_600,
) -> int:
    """Read a trustworthy page, slide, frame, or top-level unit count."""

    source = Path(path)
    if mime_type == "application/pdf":
        return _inspect_pdf(source)
    if mime_type in {DOCX_MIME, PPTX_MIME}:
        _, count = inspect_ooxml(source)
        return count
    if mime_type in VIDEO_MIME_TYPES:
        _inspect_video(
            source,
            max_video_seconds=max_video_seconds,
            max_video_frame_pixels=max_video_frame_pixels,
        )
        return 1

    try:
        from PIL import Image, UnidentifiedImageError

        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as image:
                image.verify()
            with Image.open(source) as image:
                return max(1, int(getattr(image, "n_frames", 1)))
    except ImportError as exc:
        raise FileValidationError(
            "image_inspector_unavailable", "Pillow is required to validate images"
        ) from exc
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise FileValidationError(
            "decompression_bomb", "image dimensions exceed the safe limit"
        ) from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise FileValidationError("invalid_image", "image is corrupt or unreadable") from exc


def inspect_page_count(path: str | Path, mime_type: str) -> int:
    """Compatibility name for the internal page-oriented storage boundary."""

    return inspect_unit_count(path, mime_type)


def validate_stored_file(
    path: str | Path,
    filename: str | None,
    *,
    max_bytes: int,
    max_pages: int,
    max_video_seconds: int = 1_800,
    max_video_frame_pixels: int = 33_177_600,
) -> tuple[str, str, int, int]:
    source = Path(path)
    size = source.stat().st_size
    if size <= 0:
        raise FileValidationError("empty_file", "uploaded file is empty")
    if size > max_bytes:
        raise FileValidationError(
            "file_too_large", f"file exceeds maximum size of {max_bytes} bytes"
        )
    with source.open("rb") as handle:
        header = handle.read(64)
    cleaned, mime_type = validate_file_header(filename, header, source_path=source)
    unit_count = inspect_unit_count(
        source,
        mime_type,
        max_video_seconds=max_video_seconds,
        max_video_frame_pixels=max_video_frame_pixels,
    )
    if mime_type not in VIDEO_MIME_TYPES and unit_count > max_pages:
        raise FileValidationError(
            "too_many_units", f"content exceeds maximum unit count of {max_pages}"
        )
    return cleaned, mime_type, size, unit_count
