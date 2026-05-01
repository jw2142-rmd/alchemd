"""Engine protocol + shared result/error types."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class EngineResult:
    markdown: str
    images: list[Path]
    engine: str
    elapsed: float
    notes: list[str] = field(default_factory=list)


class EngineError(Exception):
    """Raised by an engine when it cannot produce output. The driver catches this and retries."""

    def __init__(self, engine: str, stage: str, cause: BaseException | str):
        self.engine = engine
        self.stage = stage
        msg = f"[{engine}:{stage}] {cause}"
        super().__init__(msg)


class Engine(Protocol):
    name: str

    def convert(self, pdf: Path, out_dir: Path) -> EngineResult: ...
