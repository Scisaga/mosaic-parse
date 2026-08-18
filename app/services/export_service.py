"""Deterministic Markdown/Text normalization and page joining."""

from __future__ import annotations

import html
import re
from collections.abc import Sequence

from app.models.parse_result import PageParseResult
from app.services.table_service import TableFragment, assemble_logical_tables

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_FENCE_RE = re.compile(r"^\s*```[^\n]*\n|^\s*```\s*$", re.MULTILINE)
_EMPHASIS_RE = re.compile(r"(?<!\\)(?:\*\*|__|~~|`)(.*?)(?<!\\)(?:\*\*|__|~~|`)", re.DOTALL)


class ExportService:
    @staticmethod
    def normalize_markdown(content: str) -> str:
        content = content.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
        content = "\n".join(line.rstrip() for line in content.splitlines())
        content = re.sub(r"\n{4,}", "\n\n\n", content)
        return content.strip()

    @staticmethod
    def markdown_to_text(markdown: str) -> str:
        text = _FENCE_RE.sub("", markdown)
        text = _LINK_RE.sub(lambda match: match.group(1), text)
        text = _HEADING_RE.sub("", text)
        text = re.sub(r"^[ \t]{0,3}>[ \t]?", "", text, flags=re.MULTILINE)
        text = re.sub(r"^[ \t]*[-+*][ \t]+", "", text, flags=re.MULTILINE)
        text = re.sub(r"^[ \t]*\d+[.)][ \t]+", "", text, flags=re.MULTILINE)
        text = _EMPHASIS_RE.sub(lambda match: match.group(1), text)
        text = _HTML_TAG_RE.sub("", text)
        text = html.unescape(text)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def join_pages(
        self, pages: Sequence[PageParseResult], *, preserve_page_breaks: bool
    ) -> tuple[str, str]:
        markdown_parts: list[str] = []
        text_parts: list[str] = []
        for page in pages:
            markdown = self.normalize_markdown(page.content or "")
            plain = (page.plain_text or self.markdown_to_text(markdown)).strip()
            if preserve_page_breaks:
                markdown_parts.append(f"<!-- page: {page.page_number} -->\n\n{markdown}".rstrip())
                text_parts.append(f"--- Page {page.page_number} ---\n\n{plain}".rstrip())
            else:
                markdown_parts.append(markdown)
                text_parts.append(plain)
        separator = "\n\n\f\n\n" if preserve_page_breaks else "\n\n"
        return separator.join(markdown_parts).strip(), separator.join(text_parts).strip()

    def join_document(
        self,
        pages: Sequence[PageParseResult],
        *,
        preserve_page_breaks: bool,
        table_fragments: list[object] | None = None,
        merge_cross_page_tables: bool = True,
    ) -> tuple[str, str]:
        fragments = [item for item in table_fragments or [] if isinstance(item, TableFragment)]
        content_by_page = assemble_logical_tables(
            list(pages),
            fragments,
            enabled=merge_cross_page_tables,
        )
        canonical = [
            page.model_copy(
                update={
                    "content": content_by_page.get(page.page_number, page.content or ""),
                    "plain_text": None,
                }
            )
            for page in pages
        ]
        return self.join_pages(canonical, preserve_page_breaks=preserve_page_breaks)
