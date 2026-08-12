"""Parser adapter protocol and backend-neutral failures."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from app.models.backend import BackendStatus
from app.models.parse_options import DocumentParseOptions
from app.models.parse_result import DocumentParseResult
from app.models.source import StoredSource

ProgressCallback = Callable[[int, int, str], Awaitable[None] | None]


class ParserError(RuntimeError):
    code = "parse_failed"

    def __init__(self, message: str, *, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.details = details


class ParserUnavailableError(ParserError):
    code = "backend_unavailable"


class ParserTimeoutError(ParserError):
    code = "parse_timeout"


class ParserCancelledError(ParserError):
    code = "job_cancelled"


class DocumentParser(ABC):
    name: str

    @abstractmethod
    async def initialize(self) -> None: ...

    @abstractmethod
    async def probe(self, *, force: bool = False) -> BackendStatus: ...

    @abstractmethod
    async def parse(
        self,
        source: StoredSource,
        options: DocumentParseOptions,
        *,
        document_id: str,
        progress_callback: ProgressCallback | None = None,
        cancel_event: object | None = None,
    ) -> DocumentParseResult: ...

    async def close(self) -> None:
        return None

