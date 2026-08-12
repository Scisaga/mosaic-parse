"""Small explicit parser registry."""

from __future__ import annotations

from app.models.parse_options import ParseMode
from app.parsers.base import DocumentParser, ParserUnavailableError


class ParserRegistry:
    def __init__(self) -> None:
        self._parsers: dict[ParseMode, DocumentParser] = {}

    def register(self, mode: ParseMode | str, parser: DocumentParser) -> None:
        self._parsers[ParseMode(mode)] = parser

    def get(self, mode: ParseMode | str) -> DocumentParser:
        parsed_mode = ParseMode(mode)
        parser = self._parsers.get(parsed_mode)
        if parser is None:
            raise ParserUnavailableError(f"parser mode {parsed_mode.value!r} is not configured")
        return parser

    resolve = get

    def all(self) -> list[DocumentParser]:
        return list(dict.fromkeys(self._parsers.values()))

    def clear(self) -> None:
        self._parsers.clear()

