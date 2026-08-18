"""Lazy service exports.

Keeping this package initializer import-free prevents parser adapters from
forming a cycle when they use focused service helpers such as table export.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "CleanupResult": ("app.services.cleanup_service", "CleanupResult"),
    "CleanupService": ("app.services.cleanup_service", "CleanupService"),
    "ExportService": ("app.services.export_service", "ExportService"),
    "JobService": ("app.services.job_service", "JobService"),
    "DocumentIRService": ("app.services.ir_service", "DocumentIRService"),
    "ParserService": ("app.services.parser_service", "ParserService"),
    "QualityAssessment": ("app.services.quality_service", "QualityAssessment"),
    "QualityService": ("app.services.quality_service", "QualityService"),
    "SourceService": ("app.services.source_service", "SourceService"),
    "StorageService": ("app.services.storage_service", "StorageService"),
    "StoredResultPaths": ("app.services.storage_service", "StoredResultPaths"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value
