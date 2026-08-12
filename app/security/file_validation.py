"""Content-based validation for the v0.1 document formats."""

from __future__ import annotations

import re
import unicodedata
import warnings
from pathlib import Path

SUPPORTED_MIME_TYPES = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/tiff",
}

MIME_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "application/pdf": (".pdf",),
    "image/png": (".png",),
    "image/jpeg": (".jpg", ".jpeg"),
    "image/webp": (".webp",),
    "image/tiff": (".tif", ".tiff"),
}

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_UNSAFE_RE = re.compile(r"[\\/:*?\"<>|]+")
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


def safe_filename(filename: str | None, *, fallback: str = "document", max_length: int = 180) -> str:
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


def detect_mime_type(header: bytes) -> str | None:
    """Detect supported formats from magic bytes; never trusts request headers."""

    if header.startswith(b"%PDF-"):
        return "application/pdf"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(header) >= 4 and header[:4] in {b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"}:
        return "image/tiff"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    return None


def validate_file_header(filename: str | None, header: bytes) -> tuple[str, str]:
    mime_type = detect_mime_type(header)
    if mime_type is None:
        # Explicitly reject archives, including documents renamed as images/PDFs.
        if header.startswith((b"PK\x03\x04", b"Rar!", b"7z\xbc\xaf\x27\x1c", b"\x1f\x8b")):
            raise FileValidationError("archive_not_supported", "archives are not supported")
        raise FileValidationError("unsupported_file_type", "file content is not a supported PDF or image")

    cleaned = safe_filename(filename)
    suffix = Path(cleaned).suffix.lower()
    allowed_suffixes = MIME_EXTENSIONS[mime_type]
    if suffix and suffix not in allowed_suffixes:
        raise FileValidationError(
            "file_type_mismatch",
            f"filename extension {suffix!r} does not match detected MIME type {mime_type}",
        )
    if not suffix:
        cleaned += allowed_suffixes[0]
    return cleaned, mime_type


def inspect_page_count(path: str | Path, mime_type: str) -> int:
    """Read a trustworthy page/frame count using PyMuPDF or Pillow."""

    source = Path(path)
    if mime_type == "application/pdf":
        try:
            try:
                import pymupdf
            except ImportError:
                import fitz as pymupdf  # type: ignore[no-redef]
            with pymupdf.open(source) as document:
                if document.needs_pass:
                    raise FileValidationError("encrypted_pdf", "password-protected PDFs are not supported")
                count = int(document.page_count)
        except FileValidationError:
            raise
        except ImportError as exc:
            raise FileValidationError("pdf_inspector_unavailable", "PyMuPDF is required to validate PDF page count") from exc
        except Exception as exc:
            raise FileValidationError("invalid_pdf", "PDF is corrupt, encrypted, or otherwise unreadable") from exc
        if count < 1:
            raise FileValidationError("empty_document", "document contains no pages")
        return count

    try:
        from PIL import Image, UnidentifiedImageError

        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as image:
                image.verify()
            with Image.open(source) as image:
                return max(1, int(getattr(image, "n_frames", 1)))
    except ImportError as exc:
        raise FileValidationError("image_inspector_unavailable", "Pillow is required to validate images") from exc
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise FileValidationError("decompression_bomb", "image dimensions exceed the safe pixel limit") from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise FileValidationError("invalid_image", "image is corrupt or unreadable") from exc


def validate_stored_file(
    path: str | Path,
    filename: str | None,
    *,
    max_bytes: int,
    max_pages: int,
) -> tuple[str, str, int, int]:
    source = Path(path)
    size = source.stat().st_size
    if size <= 0:
        raise FileValidationError("empty_file", "uploaded file is empty")
    if size > max_bytes:
        raise FileValidationError("file_too_large", f"file exceeds maximum size of {max_bytes} bytes")
    with source.open("rb") as handle:
        header = handle.read(64)
    cleaned, mime_type = validate_file_header(filename, header)
    page_count = inspect_page_count(source, mime_type)
    if page_count > max_pages:
        raise FileValidationError("too_many_pages", f"document exceeds maximum page count of {max_pages}")
    return cleaned, mime_type, size, page_count
