from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .language_compat import detect_language
from .markdown_units import decode_hash_u_name, parse_markdown_units


@dataclass(frozen=True)
class ProcessSpan:
    span_id: str
    kind: str  # process_text | process_table
    book_title: str
    heading_path: tuple[str, ...]
    line_start: int
    line_end: int
    text: str
    expected_labels: tuple[int, ...]
    process_type_hint: str
    language: str = "zh"


@dataclass(frozen=True)
class _Block:
    kind: str  # paragraph | table
    heading_path: tuple[str, ...]
    line_start: int
    line_end: int
    text: str


_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$")
_HTML_TABLE_START = re.compile(r"<table\b", re.I)
_HTML_TABLE_END = re.compile(r"</table\s*>", re.I)
_PIPE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")

# Explicit step labels. Numeric labels are intentionally required to be small,
# because chapter/figure numbers must not be treated as process steps.
_NUMERIC_LABEL = re.compile(
    r"(?:^|[\n；;。])\s*(?:步骤\s*)?(?:[（(]\s*(\d{1,2})\s*[）)]|(\d{1,2})\s*[、.)）])",
    re.M,
)
_CN_STEP_LABEL = re.compile(r"(?:第|步骤)\s*([一二三四五六七八九十]{1,3})\s*(?:步|骤)?")
_EN_STEP_LABEL = re.compile(r"(?:^|[\n;])\s*(?:step\s*)?(\d{1,2})\s*(?:[.:)\-]|\b)", re.I | re.M)
_EN_TABLE_STEP_LABEL = re.compile(r"(?:^|\n)\s*\|\s*(\d{1,2})\s*\|", re.M)
_EN_ORDINAL_LABEL = re.compile(
    r"(?:^|[\n;])\s*(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)"
    r"(?:\s+step)?\s*[,.:)\-]",
    re.I | re.M,
)
_EN_ORDINAL_MAP = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
}
_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
_CIRCLED_MAP = {char: index + 1 for index, char in enumerate(_CIRCLED)}

_PROCESS_LEAD = re.compile(
    r"(?:制备方法|合成方法|生产工艺|加工工艺|装配过程|装填过程|试验方法|测试方法|测定步骤|"
    r"计算步骤|求解步骤|处理方法|纯化方法|结晶工艺|操作步骤|工艺流程|具体过程|实验步骤)"
    r".{0,16}(?:如下|为|包括|依次|分为|可按|见下)",
    re.I,
)
_SEQUENCE = re.compile(r"(?:首先|先将|先把|第一步|其次|然后|随后|接着|再将|再把|待.{0,18}后|完成后|最后|依次|分别)")
_STEP_WORD = re.compile(r"(?:步骤|工序|操作|阶段|次序|流程|程序)")
_EN_PROCESS_LEAD = re.compile(
    r"\b(?:preparation|experimental|experiment|synthesis|manufacturing|production|processing|test|testing|"
    r"assembly|loading|calculation|computational|operating)\s+(?:procedure|process|method|steps?|workflow)\b"
    r"(?:.{0,60}\b(?:as follows|includes|comprises|consists of|involves|is described below)\b)?",
    re.I | re.S,
)
_EN_SEQUENCE = re.compile(
    r"\b(?:first|second|third|then|next|subsequently|afterwards?|after that|finally|followed by|"
    r"upon completion|once .{0,30} is complete)\b",
    re.I,
)
_EN_PROCESS_TOPIC = re.compile(
    r"\b(?:preparation|synthesis|manufacturing|production|processing|testing|test|assembly|loading|"
    r"calculation|computational|operation|procedure|process|workflow)\b",
    re.I,
)

# Physical/experimental/computational actions. The detector only proposes spans;
# Qwen and the graph validator still decide whether a real executable process exists.
_ACTIONS = (
    "称取", "量取", "加入", "添加", "投入", "装入", "放入", "注入", "滴加", "喷入", "导入",
    "混合", "搅拌", "研磨", "粉碎", "筛分", "过筛", "分散", "乳化", "捏合", "揉合", "共混",
    "加热", "升温", "保温", "冷却", "降温", "熔融", "溶解", "蒸发", "浓缩", "结晶", "固化",
    "过滤", "抽滤", "离心", "分离", "洗涤", "干燥", "真空干燥", "除去", "脱除", "纯化",
    "聚合", "硝化", "中和", "氧化", "还原", "水解", "缩合", "交联", "熟化",
    "压制", "压片", "压药", "装药", "装填", "灌装", "浇注", "挤出", "切割", "造粒", "成型",
    "点火", "起爆", "引爆", "测试", "测量", "测定", "记录", "采集", "校准", "标定",
    "连接", "安装", "固定", "密封", "拆卸", "组合", "装配", "焊接", "粘接", "包覆",
    "输入", "初始化", "计算", "求解", "迭代", "修正", "输出", "建立模型",
)
_ACTION_RE = re.compile("|".join(sorted((re.escape(x) for x in _ACTIONS), key=len, reverse=True)))
_EN_ACTION_RE = re.compile(
    r"\b(?:"
    r"weigh(?:s|ed|ing)?|measure(?:s|d|ing)?|add(?:s|ed|ing)?|charge(?:s|d|ing)?|load(?:s|ed|ing)?|"
    r"pour(?:s|ed|ing)?|inject(?:s|ed|ing)?|transfer(?:s|red|ring)?|mix(?:es|ed|ing)?|blend(?:s|ed|ing)?|"
    r"stir(?:s|red|ring)?|grind(?:s|ed|ing)?|mill(?:s|ed|ing)?|sieve(?:s|d|ing)?|disperse(?:s|d|ing)?|"
    r"heat(?:s|ed|ing)?|cool(?:s|ed|ing)?|maintain(?:s|ed|ing)?|hold(?:s|held|ing)?|melt(?:s|ed|ing)?|"
    r"dissolve(?:s|d|ing)?|evaporate(?:s|d|ing)?|concentrate(?:s|d|ing)?|crystallize(?:s|d|ing)?|"
    r"filter(?:s|ed|ing)?|centrifuge(?:s|d|ing)?|separate(?:s|d|ing)?|wash(?:es|ed|ing)?|dry(?:ies|ied|ing)?|"
    r"purify(?:ies|ied|ing)?|polymerize(?:s|d|ing)?|nitrate(?:s|d|ing)?|neutralize(?:s|d|ing)?|"
    r"press(?:es|ed|ing)?|cast(?:s|ed|ing)?|extrude(?:s|d|ing)?|granulate(?:s|d|ing)?|form(?:s|ed|ing)?|"
    r"cure(?:s|d|ing)?|assemble(?:s|d|ing)?|connect(?:s|ed|ing)?|install(?:s|ed|ing)?|seal(?:s|ed|ing)?|"
    r"ignite(?:s|d|ing)?|initiate(?:s|d|ing)?|test(?:s|ed|ing)?|record(?:s|ed|ing)?|calibrate(?:s|d|ing)?|"
    r"input(?:s|ted|ting)?|initialize(?:s|d|ing)?|calculate(?:s|d|ing)?|solve(?:s|d|ing)?|iterate(?:s|d|ing)?|"
    r"correct(?:s|ed|ing)?|output(?:s|ted|ting)?"
    r")\b",
    re.I,
)

# Article-outline and historical narration are frequent false positives.
_DISCOURSE_ACTIONS = re.compile(r"(?:介绍|讨论|分析|阐述|综述|说明|叙述|探讨|回顾|列举|概述|本章|本节|下文)")
_HISTORY = re.compile(r"(?:研制时间|研制国家|发明者|发展历史|年代|年研制|首次报道|由.+提出)")
_MECHANISM_ONLY = re.compile(r"(?:反应机理|链引发|链增长|链终止|热分解阶段|爆轰形成阶段|机理阶段)")
_EN_DISCOURSE_ACTIONS = re.compile(r"\b(?:introduces?|discusses?|analyzes?|describes?|reviews?|summarizes?|outlines?|presents?)\b", re.I)
_EN_HISTORY = re.compile(r"\b(?:history|historical|developed in|invented by|first reported|was proposed by|in the \d{4}s?)\b", re.I)
_EN_MECHANISM_ONLY = re.compile(r"\b(?:reaction mechanism|chain initiation|chain propagation|chain termination|thermal decomposition stage|detonation formation stage)\b", re.I)
_REFERENCE_HEADING = re.compile(r"^(?:参考文献|references?|索引|index|目录|contents?)$", re.I)
_TOC_LINE = re.compile(r"(?:\.{2,}|…{2,}|\s\d{1,4}\s*$)")


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _cn_number(value: str) -> int | None:
    digits = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value == "十":
        return 10
    if "十" in value:
        left, _, right = value.partition("十")
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        number = tens * 10 + ones
        return number if 1 <= number <= 40 else None
    number = digits.get(value)
    return number if number and number <= 40 else None


def _label_positions(text: str) -> list[tuple[int, int]]:
    source = text or ""
    found: list[tuple[int, int]] = []
    for match in _NUMERIC_LABEL.finditer(source):
        raw = match.group(1) or match.group(2)
        try:
            number = int(raw)
        except Exception:
            continue
        if 1 <= number <= 40:
            found.append((match.start(), number))
    for match in _CN_STEP_LABEL.finditer(source):
        number = _cn_number(match.group(1))
        if number:
            found.append((match.start(), number))
    for match in _EN_STEP_LABEL.finditer(source):
        try:
            number = int(match.group(1))
        except Exception:
            continue
        if 1 <= number <= 40:
            found.append((match.start(), number))
    for match in _EN_TABLE_STEP_LABEL.finditer(source):
        number = int(match.group(1))
        if 1 <= number <= 40:
            found.append((match.start(), number))
    for match in _EN_ORDINAL_LABEL.finditer(source):
        number = _EN_ORDINAL_MAP.get(match.group(1).casefold())
        if number:
            found.append((match.start(), number))
    for index, char in enumerate(source):
        number = _CIRCLED_MAP.get(char)
        if number:
            found.append((index, number))
    unique: dict[tuple[int, int], None] = {}
    for item in found:
        unique.setdefault(item, None)
    return sorted(unique, key=lambda item: (item[0], item[1]))


def extract_expected_labels(text: str) -> tuple[int, ...]:
    return tuple(sorted({number for _, number in _label_positions(text)}))


def _is_pipe_table(lines: Sequence[str], index: int) -> bool:
    return index + 1 < len(lines) and "|" in lines[index] and bool(_PIPE_SEPARATOR.match(lines[index + 1]))


def _read_blocks(path: Path) -> list[_Block]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    lines = text.splitlines()
    headings: list[str] = []
    blocks: list[_Block] = []
    buffer: list[str] = []
    start = 0
    block_heading: tuple[str, ...] = ()

    def flush(end_line: int) -> None:
        nonlocal buffer, start, block_heading
        if buffer:
            raw = "\n".join(buffer).strip()
            if raw:
                blocks.append(_Block("paragraph", block_heading, start, end_line, raw))
        buffer = []
        start = 0
        block_heading = tuple(headings)

    i = 0
    while i < len(lines):
        line = lines[i]
        line_no = i + 1
        heading = _HEADING.match(line)
        if heading:
            flush(line_no - 1)
            level = len(heading.group(1))
            title = re.sub(r"\s+", " ", heading.group(2)).strip()
            headings = headings[: level - 1]
            headings.append(title)
            block_heading = tuple(headings)
            i += 1
            continue
        if _HTML_TABLE_START.search(line):
            flush(line_no - 1)
            table_start = line_no
            table_lines = [line]
            i += 1
            while i < len(lines):
                table_lines.append(lines[i])
                if _HTML_TABLE_END.search(lines[i]):
                    i += 1
                    break
                i += 1
            blocks.append(_Block("table", tuple(headings), table_start, table_start + len(table_lines) - 1, "\n".join(table_lines)))
            continue
        if _is_pipe_table(lines, i):
            flush(line_no - 1)
            table_start = line_no
            table_lines = [line, lines[i + 1]]
            i += 2
            while i < len(lines) and lines[i].strip() and "|" in lines[i]:
                table_lines.append(lines[i])
                i += 1
            blocks.append(_Block("table", tuple(headings), table_start, table_start + len(table_lines) - 1, "\n".join(table_lines)))
            continue
        if not line.strip():
            flush(line_no - 1)
            i += 1
            continue
        if start == 0:
            start = line_no
            block_heading = tuple(headings)
        buffer.append(line)
        i += 1
    flush(len(lines))
    return blocks


def _action_count(text: str) -> int:
    source = text or ""
    return len(_ACTION_RE.findall(source)) + len(_EN_ACTION_RE.findall(source))


def _numbered_action_steps(text: str) -> int:
    source = text or ""
    positions = sorted({pos for pos, _ in _label_positions(source)})
    count = 0
    for pos_index, pos in enumerate(positions):
        stop = positions[pos_index + 1] if pos_index + 1 < len(positions) else min(len(source), pos + 700)
        clause = source[pos:stop]
        if _ACTION_RE.search(clause) or _EN_ACTION_RE.search(clause):
            count += 1
    return count


def _looks_like_toc(text: str) -> bool:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return False
    tocish = sum(1 for line in lines if _TOC_LINE.search(line) or re.match(r"^\d+(?:\.\d+){1,4}\s*[^。；]{1,50}\s+\d{1,4}$", line))
    return tocish >= max(2, len(lines) // 2)


def _text_process_score(text: str) -> int:
    compact = _compact(text)
    spaced = re.sub(r"\s+", " ", str(text or "")).strip()
    labels = extract_expected_labels(text)
    actions = _action_count(text)
    sequences = len(_SEQUENCE.findall(text or "")) + len(_EN_SEQUENCE.findall(text or ""))
    numbered_actions = _numbered_action_steps(text)
    lead = bool(_PROCESS_LEAD.search(compact) or _EN_PROCESS_LEAD.search(spaced))
    score = 0
    if lead and actions >= 2:
        score += 8
    if numbered_actions >= 2:
        score += 8
    elif numbered_actions == 1 and lead:
        score += 3
    if sequences >= 2 and actions >= 2:
        score += 7
    elif sequences == 1 and actions >= 3:
        score += 3
    if actions >= 5 and (re.search(r"(?:制备|工艺|操作|试验|测试|计算|处理|装配|装填)", compact) or _EN_PROCESS_TOPIC.search(spaced)):
        score += 4
    if len(labels) >= 2 and numbered_actions == 0:
        score -= 5
    if _looks_like_toc(text):
        score -= 12
    if _HISTORY.search(compact) or _EN_HISTORY.search(spaced):
        score -= 8
    if (_DISCOURSE_ACTIONS.search(compact) or _EN_DISCOURSE_ACTIONS.search(spaced)) and actions <= 2:
        score -= 7
    executable = bool(_ACTION_RE.search(text or "") or _EN_ACTION_RE.search(text or ""))
    if (_MECHANISM_ONLY.search(compact) or _EN_MECHANISM_ONLY.search(spaced)) and not executable:
        score -= 8
    return score


def _continuation_score(text: str) -> int:
    actions = _action_count(text)
    numbered_actions = _numbered_action_steps(text)
    score = actions * 2 + numbered_actions * 3
    if (_SEQUENCE.search(text or "") or _EN_SEQUENCE.search(text or "")) and actions:
        score += 2
    if _looks_like_toc(text) or _HISTORY.search(_compact(text)) or _EN_HISTORY.search(text or ""):
        score -= 8
    return score


def _type_hint(text: str) -> str:
    compact = _compact(text)
    spaced = re.sub(r"\s+", " ", str(text or "")).strip()
    if re.search(r"(?:测试|试验|测定|测量|标定|校准|检测)", compact) or re.search(r"\b(?:test|testing|measure|measurement|calibrate|calibration|experimental procedure)\b", spaced, re.I):
        return "试验流程"
    if re.search(r"(?:装配|装填|压装|安装|连接|固定|密封)", compact) or re.search(r"\b(?:assembly|assemble|loading|load|install|connect|seal)\b", spaced, re.I):
        return "装配流程"
    if re.search(r"(?:计算|求解|迭代|算法|输入参数|输出结果)", compact) or re.search(r"\b(?:calculate|calculation|compute|solve|iterate|algorithm|input|output)\b", spaced, re.I):
        return "计算流程"
    if re.search(r"(?:制备|合成|聚合|硝化|结晶|生产|加工|配制)", compact) or re.search(r"\b(?:preparation|prepare|synthesis|synthesize|manufacturing|produce|polymerize|crystallize|mixing procedure)\b", spaced, re.I):
        return "制备工艺"
    if re.search(r"(?:纯化|洗涤|干燥|分离|处理|筛分|粉碎)", compact) or re.search(r"\b(?:purify|wash|dry|separate|process|sieve|grind|mill)\b", spaced, re.I):
        return "处理流程"
    return "操作流程"


def _span_id(book: str, kind: str, start: int, end: int, text: str) -> str:
    digest = hashlib.sha1(f"{book}|{kind}|{start}|{end}|{text}".encode("utf-8")).hexdigest()
    return f"process_span:{digest[:24]}"


def detect_process_spans(
    path: Path,
    *,
    max_span_chars: int = 12000,
    max_spans: int = 0,
    include_tables: bool = True,
) -> list[ProcessSpan]:
    """Detect complete process candidates without creating graph facts.

    Detection deliberately favors recall, while filtering article outlines,
    historical statements and mechanism-only enumerations. Final publication is
    controlled by the LLM process extractor and deterministic graph validator.
    """

    blocks = _read_blocks(path)
    book = decode_hash_u_name(path.stem)
    spans: list[ProcessSpan] = []
    occupied: list[tuple[int, int]] = []

    paragraph_indices = [i for i, block in enumerate(blocks) if block.kind == "paragraph"]
    for block_index in paragraph_indices:
        block = blocks[block_index]
        if any(_REFERENCE_HEADING.match(h.strip()) for h in block.heading_path):
            continue
        if _text_process_score(block.text) < 7:
            continue

        left = block_index
        right = block_index
        total_chars = len(block.text)

        # Include one lead-in paragraph when it names the method or process.
        if left > 0:
            prev = blocks[left - 1]
            if prev.kind == "paragraph" and prev.heading_path == block.heading_path and len(prev.text) <= 1000:
                if _PROCESS_LEAD.search(_compact(prev.text)) or _EN_PROCESS_LEAD.search(prev.text) or (_action_count(prev.text) >= 1 and _continuation_score(block.text) >= 4):
                    left -= 1
                    total_chars += len(prev.text) + 2

        cursor = right + 1
        while cursor < len(blocks):
            nxt = blocks[cursor]
            if nxt.kind != "paragraph" or nxt.heading_path != block.heading_path:
                break
            score = _continuation_score(nxt.text)
            if score < 2:
                break
            if total_chars + len(nxt.text) + 2 > max_span_chars:
                break
            right = cursor
            total_chars += len(nxt.text) + 2
            cursor += 1

        selected = blocks[left : right + 1]
        raw = "\n\n".join(item.text for item in selected if item.kind == "paragraph").strip()
        if _text_process_score(raw) < 8 or _action_count(raw) < 2:
            continue
        start_line = selected[0].line_start
        end_line = selected[-1].line_end
        if any(not (end_line < old_start or start_line > old_end) for old_start, old_end in occupied):
            continue
        occupied.append((start_line, end_line))
        spans.append(
            ProcessSpan(
                span_id=_span_id(book, "process_text", start_line, end_line, raw),
                kind="process_text",
                book_title=book,
                heading_path=block.heading_path,
                line_start=start_line,
                line_end=end_line,
                text=raw,
                expected_labels=extract_expected_labels(raw),
                process_type_hint=_type_hint(raw),
                language=detect_language(raw).language,
            )
        )

    if include_tables:
        # parse_markdown_units reliably preserves complete/chunked table evidence.
        _, tables = parse_markdown_units(path, max_table_chars=max_span_chars)
        for table in tables:
            compact = _compact(table.text)
            headerish = compact[:2000]
            row_count = max(len(re.findall(r"<tr\b", table.text, re.I)), max(0, len(table.text.splitlines()) - 2))
            process_header = bool(re.search(r"(?:操作步骤|试验步骤|工艺步骤|步骤.{0,8}(?:操作|方法|内容)|工序.{0,8}(?:操作|内容)|(?:序号|次序).{0,12}(?:操作|工序)|工艺流程)", headerish) or re.search(r"\b(?:step|sequence|operation|procedure|process)\b.{0,80}\b(?:operation|action|procedure|condition|temperature|time)\b", table.text, re.I | re.S))
            action_count = _action_count(table.text)
            labels = extract_expected_labels(table.text)
            if row_count < 2 or not process_header or (action_count < 2 and len(labels) < 2):
                continue
            spans.append(
                ProcessSpan(
                    span_id=_span_id(book, "process_table", table.line_start, table.line_end, table.text),
                    kind="process_table",
                    book_title=book,
                    heading_path=table.heading_path,
                    line_start=table.line_start,
                    line_end=table.line_end,
                    text=table.text,
                    expected_labels=labels,
                    process_type_hint=_type_hint(" ".join(table.heading_path) + "\n" + table.text),
                    language=detect_language(" ".join(table.heading_path) + "\n" + table.text).language,
                )
            )

    spans.sort(key=lambda item: (item.line_start, item.kind, item.line_end))
    if max_spans > 0:
        spans = spans[:max_spans]
    return spans


__all__ = ["ProcessSpan", "detect_process_spans", "extract_expected_labels"]
