"""Duck-typed Settings access so core services do not import app.config."""

from __future__ import annotations

from pathlib import Path


def setting[T](settings: object | None, name: str, default: T) -> T:
    value = getattr(settings, name, default)
    return default if value is None else value


def setting_any[T](settings: object | None, names: tuple[str, ...], default: T) -> T:
    for name in names:
        value = getattr(settings, name, None)
        if value is not None:
            return value
    return default


def setting_path(settings: object | None, name: str, default: str | Path) -> Path:
    return Path(setting(settings, name, default)).expanduser()
