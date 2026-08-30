"""Immutable source-file values shared by domain inventories."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from sigilicon.artifacts import read_nofollow_text


@dataclass(frozen=True)
class TextSourceSnapshot:
    """One exact UTF-8 read bound to its canonical source path."""

    source_path: Path
    text: str

    def __post_init__(self) -> None:
        source = Path(os.path.abspath(self.source_path))
        if source != self.source_path or source != source.resolve(strict=False):
            raise ValueError("text source snapshot path must be canonical")
        if not isinstance(self.text, str):
            raise ValueError("text source snapshot payload must be UTF-8 text")


def load_text_source_snapshot(path: Path) -> TextSourceSnapshot:
    """Read one canonical regular file exactly once without following links."""

    source = Path(os.path.abspath(path))
    return TextSourceSnapshot(
        source_path=source,
        text=read_nofollow_text(source),
    )
