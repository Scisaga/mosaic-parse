"""Reserved v0.2 parser boundary.

The class reports its explicit disabled state instead of pretending the official
GLM SDK pipeline exists in v0.1.
"""

from __future__ import annotations

from app.models.backend import BackendState, BackendStatus
from app.models.parse_options import DocumentParseOptions
from app.models.parse_result import DocumentParseResult
from app.models.source import StoredSource
from app.parsers.base import DocumentParser, ParserUnavailableError, ProgressCallback


class GlmSdkRemoteParser(DocumentParser):
    name = "glm-sdk-remote"

    async def initialize(self) -> None:
        return None

    async def probe(self, *, force: bool = False) -> BackendStatus:
        return BackendStatus(
            name=self.name,
            state=BackendState.DISABLED,
            enabled=False,
            detail="official GLM SDK fallback is planned for v0.2",
        )

    async def parse(
        self,
        source: StoredSource,
        options: DocumentParseOptions,
        *,
        document_id: str,
        progress_callback: ProgressCallback | None = None,
        cancel_event: object | None = None,
    ) -> DocumentParseResult:
        raise ParserUnavailableError("official GLM SDK fallback is not enabled in v0.1")

