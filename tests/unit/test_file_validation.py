import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.security.file_validation import (
    FileValidationError,
    detect_mime_type,
    inspect_ooxml,
    inspect_unit_count,
    safe_filename,
    validate_file_header,
    validate_stored_file,
)


@pytest.mark.parametrize(
    ("header", "mime_type"),
    [
        (b"%PDF-1.7\n", "application/pdf"),
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"\xff\xd8\xff\xe0", "image/jpeg"),
        (b"II*\x00", "image/tiff"),
        (b"BM\x00\x00", "image/bmp"),
        (b"RIFF\x00\x00\x00\x00WEBP", "image/webp"),
        (b"RIFF\x00\x00\x00\x00AVI ", "video/x-msvideo"),
        (b"\x00\x00\x00\x18ftypisom", "video/mp4"),
        (b"\x1aE\xdf\xa3webm", "video/webm"),
    ],
)
def test_detect_mime_from_magic(header: bytes, mime_type: str) -> None:
    assert detect_mime_type(header) == mime_type


def test_filename_is_confined_to_a_basename() -> None:
    assert safe_filename("../../<report>?.PDF") == "_report_.pdf"
    assert safe_filename("CON.pdf") == "_CON.pdf"


def test_extension_must_match_content() -> None:
    with pytest.raises(FileValidationError) as caught:
        validate_file_header("image.png", b"%PDF-1.7\n")
    assert caught.value.code == "file_type_mismatch"


def test_archives_are_explicitly_rejected() -> None:
    with pytest.raises(FileValidationError) as caught:
        validate_file_header("document.pdf", b"PK\x03\x04payload")
    assert caught.value.code == "archive_not_supported"


def test_fixture_page_count_is_inspected() -> None:
    fixture = Path("tests/fixtures/native-report.pdf")
    filename, mime_type, size, pages = validate_stored_file(
        fixture,
        fixture.name,
        max_bytes=1_000_000,
        max_pages=10,
    )
    assert filename == fixture.name
    assert mime_type == "application/pdf"
    assert size > 0
    assert pages == 1


@pytest.mark.parametrize(
    ("name", "mime_type", "units"),
    [
        (
            "embedded-image.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            1,
        ),
        (
            "embedded-image.pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            1,
        ),
    ],
)
def test_ooxml_fixtures_are_identified_from_package_content(
    name: str, mime_type: str, units: int
) -> None:
    path = Path("tests/fixtures") / name
    assert inspect_ooxml(path) == (mime_type, units)
    cleaned, detected, _, count = validate_stored_file(
        path, path.name, max_bytes=5_000_000, max_pages=10
    )
    assert cleaned == name
    assert detected == mime_type
    assert count == units


def _minimal_docx(
    path: Path,
    *,
    relationship: str | None = None,
    extra: tuple[str, bytes] | None = None,
    content_types: bytes | None = None,
) -> None:
    types = content_types or b"""<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", types)
        archive.writestr("word/document.xml", "<document/>")
        if relationship is not None:
            archive.writestr("word/_rels/document.xml.rels", relationship)
        if extra is not None:
            archive.writestr(*extra)


def test_ooxml_rejects_external_relationships(tmp_path: Path) -> None:
    path = tmp_path / "external.docx"
    _minimal_docx(
        path,
        relationship="""<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://example/image" Target="https://example.test/a.png" TargetMode="External"/>
</Relationships>""",
    )
    with pytest.raises(FileValidationError) as caught:
        inspect_ooxml(path)
    assert caught.value.code == "ooxml_external_relationship"


@pytest.mark.parametrize("target", ["../../../escape.png", "%252e%252e/%252e%252e/escape.png"])
def test_ooxml_rejects_relationship_path_traversal(tmp_path: Path, target: str) -> None:
    path = tmp_path / "traversal.docx"
    _minimal_docx(
        path,
        relationship=f"""<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://example/image" Target="{target}"/>
</Relationships>""",
    )
    with pytest.raises(FileValidationError) as caught:
        inspect_ooxml(path)
    assert caught.value.code == "archive_path_traversal"


def test_ooxml_rejects_unsafe_zip_member_and_compression_ratio(tmp_path: Path) -> None:
    traversal = tmp_path / "member.docx"
    _minimal_docx(traversal, extra=("../escape.bin", b"x"))
    with pytest.raises(FileValidationError) as caught:
        inspect_ooxml(traversal)
    assert caught.value.code == "archive_path_traversal"

    compressed = tmp_path / "compressed.docx"
    _minimal_docx(compressed, extra=("word/media/huge.bin", b"A" * (2 * 1024 * 1024)))
    with pytest.raises(FileValidationError) as caught:
        inspect_ooxml(compressed)
    assert caught.value.code == "archive_suspicious_ratio"


def test_ooxml_rejects_malformed_content_types(tmp_path: Path) -> None:
    path = tmp_path / "broken.docx"
    _minimal_docx(path, content_types=b"<Types>")
    with pytest.raises(FileValidationError) as caught:
        inspect_ooxml(path)
    assert caught.value.code == "invalid_ooxml"


def test_video_probe_enforces_stream_duration_and_frame_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypisom")
    monkeypatch.setattr("app.security.file_validation.shutil.which", lambda _: "/usr/bin/ffprobe")

    def completed(payload: dict[str, object]) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload).encode())

    monkeypatch.setattr(
        "app.security.file_validation.subprocess.run",
        lambda *args, **kwargs: completed(
            {"format": {"duration": "6.0"}, "streams": [{"index": 0, "width": 640, "height": 360}]}
        ),
    )
    assert inspect_unit_count(path, "video/mp4") == 1

    monkeypatch.setattr(
        "app.security.file_validation.subprocess.run",
        lambda *args, **kwargs: completed(
            {"format": {"duration": "1801"}, "streams": [{"index": 0, "width": 640, "height": 360}]}
        ),
    )
    with pytest.raises(FileValidationError) as caught:
        inspect_unit_count(path, "video/mp4", max_video_seconds=1800)
    assert caught.value.code == "video_too_long"

    monkeypatch.setattr(
        "app.security.file_validation.subprocess.run",
        lambda *args, **kwargs: completed(
            {"format": {"duration": "6"}, "streams": [{"index": 0, "width": 9000, "height": 9000}]}
        ),
    )
    with pytest.raises(FileValidationError) as caught:
        inspect_unit_count(path, "video/mp4", max_video_frame_pixels=33_177_600)
    assert caught.value.code == "video_frame_too_large"

    monkeypatch.setattr(
        "app.security.file_validation.subprocess.run",
        lambda *args, **kwargs: completed({"format": {"duration": "6"}, "streams": []}),
    )
    with pytest.raises(FileValidationError) as caught:
        inspect_unit_count(path, "video/mp4")
    assert caught.value.code == "video_stream_missing"
