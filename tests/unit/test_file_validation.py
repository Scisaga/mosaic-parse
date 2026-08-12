from pathlib import Path

import pytest

from app.security.file_validation import (
    FileValidationError,
    detect_mime_type,
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
        (b"RIFF\x00\x00\x00\x00WEBP", "image/webp"),
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
