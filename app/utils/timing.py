"""Monotonic timing helpers."""

from __future__ import annotations

import time
from contextlib import AbstractContextManager
from dataclasses import dataclass, field


@dataclass(slots=True)
class Timer(AbstractContextManager["Timer"]):
    started: float = field(default=0.0, init=False)
    ended: float = field(default=0.0, init=False)

    def __enter__(self) -> Timer:
        self.started = time.perf_counter()
        self.ended = 0.0
        return self

    def __exit__(self, *args: object) -> None:
        self.ended = time.perf_counter()

    @property
    def elapsed_ms(self) -> int:
        end = self.ended or time.perf_counter()
        return max(0, round((end - self.started) * 1000))
