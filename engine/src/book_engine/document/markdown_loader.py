from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List


@dataclass(frozen=True)
class MarkdownDocument:
    path: Path
    text: str
    lines: List[str]


def load_markdown(path: Path) -> MarkdownDocument:
    if not path.exists():
        raise FileNotFoundError(f"Input markdown does not exist: {path}")
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    return MarkdownDocument(path=path, text=text, lines=text.splitlines())
