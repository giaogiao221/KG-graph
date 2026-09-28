from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Sequence

from book_engine.document.markdown_loader import MarkdownDocument

_HEADING_RE = re.compile(r"^(?P<marks>#{1,6})\s*(?P<title>.*?)\s*$")
_CHAPTER_RE = re.compile(r"^第\s*[一二三四五六七八九十百零〇0-9]+\s*章")
_NUMBERED_RE = re.compile(r"^\s*(?P<num>\d+(?:\s*[\.．]\s*\d+){0,5})(?:\s+|[、．.])?")

_NOISE_TITLES = {
    "目录", "目 录", "contents", "content", "前言", "序", "序言", "译者前言", "原书前言",
    "作者", "原书主编及作者", "图书在版编目", "版权页", "致读者", "感谢", "符号含义",
    "参考文献", "文献", "索引", "思考题", "附录", "推荐阅读书目",
}


def _compact(text: str) -> str:
    value = re.sub(r"\s+", " ", text or "").strip()
    value = value.strip("# \t\r\n")
    return value


def _noise_key(text: str) -> str:
    return re.sub(r"\s+", "", text or "").casefold()


def _logical_level(title: str, markdown_level: int) -> int:
    value = _compact(title)
    if _CHAPTER_RE.match(value):
        return 1
    match = _NUMBERED_RE.match(value)
    if match:
        normalized = re.sub(r"\s+", "", match.group("num")).replace("．", ".")
        return max(1, min(6, normalized.count(".") + 2))
    if re.match(r"^[（(]?[一二三四五六七八九十0-9]+[)）、.]", value):
        return min(6, markdown_level + 1)
    return max(1, min(6, markdown_level))


@dataclass
class HeadingNode:
    heading_id: str
    line_index: int
    raw_title: str
    title: str
    markdown_level: int
    logical_level: int
    path: List[str] = field(default_factory=list)
    is_noise: bool = False


def rebuild_headings(document: MarkdownDocument) -> List[HeadingNode]:
    nodes: List[HeadingNode] = []
    stack: Dict[int, str] = {}
    noise_keys = {_noise_key(item) for item in _NOISE_TITLES}

    for index, raw_line in enumerate(document.lines, start=1):
        match = _HEADING_RE.match(raw_line)
        if not match:
            continue
        raw_title = match.group("title")
        title = _compact(raw_title)
        if not title:
            continue
        markdown_level = len(match.group("marks"))
        logical_level = _logical_level(title, markdown_level)
        stack = {level: label for level, label in stack.items() if level < logical_level}
        stack[logical_level] = title
        path = [stack[level] for level in sorted(stack)]
        key = _noise_key(title)
        is_noise = key in noise_keys or key.startswith("参考文献") or key.startswith("思考题")
        nodes.append(
            HeadingNode(
                heading_id=f"H{len(nodes)+1:06d}",
                line_index=index,
                raw_title=raw_title,
                title=title,
                markdown_level=markdown_level,
                logical_level=logical_level,
                path=path,
                is_noise=is_noise,
            )
        )
    return nodes


def heading_by_line(nodes: Sequence[HeadingNode]) -> Dict[int, HeadingNode]:
    return {node.line_index: node for node in nodes}


__all__ = ["HeadingNode", "rebuild_headings", "heading_by_line"]
