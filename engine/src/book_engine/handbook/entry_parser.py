# -*- coding: utf-8 -*-
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Tuple

from .common import normalize_math_text, normalize_subject_name, category_by_entry_id

ENTRY_START_RE = re.compile(r"^\s*#?\s*(?P<entry_id>[123]\d{5})(?P<title_tail>.*)$")

PROPERTY_LABELS = [
    "化学式", "分子式", "结构式", "相对分子质量", "相对分子量", "CAS号",
    "中文别名", "英文别名", "含氮量", "氧平衡",
    "[理化性质]", "[热化学性质]", "[燃烧爆炸性能]", "[感度性能]", "[用途]", "[毒性]",
    "标准生成热", "标准生成自由能", "标准吉布斯自由能",
]

@dataclass
class ChemEntry:
    entry_id: str
    canonical_subject_name: str
    english_name: str
    entry_type: str
    start_line: int
    end_line: int
    title_raw: str
    title_normalized: str
    block_lines: List[str]
    title_confidence: float = 0.88

    @property
    def block_text(self) -> str:
        return "\n".join(self.block_lines)

def looks_like_entry_start(line: str) -> bool:
    m = ENTRY_START_RE.match(line)
    if not m:
        return False
    tail = m.group("title_tail").strip()
    # 索引页常见 “100001 2” 这种只有页码，不作为正文条目
    if re.fullmatch(r"\d{1,4}", tail):
        return False
    # 空标题也暂不作为 entry；正文中少量条目可能下一行补题，但实际数据基本有标题
    if not tail:
        return False
    # 标题中需要有中文或英文，不然多半是页码/索引噪声
    if not re.search(r"[\u4e00-\u9fffA-Za-z]", tail):
        return False
    # 过长 HTML 表格行不作为 entry
    if "<tr" in tail.lower() or "<td" in tail.lower():
        return False
    return True

def find_entry_starts(lines: List[str]) -> List[Tuple[int, str, str]]:
    starts = []
    for i, line in enumerate(lines):
        m = ENTRY_START_RE.match(line)
        if not m:
            continue
        if not looks_like_entry_start(line):
            continue
        starts.append((i, m.group("entry_id"), m.group("title_tail")))

    # 本手册前部有大量索引页，索引页也会出现 300xxx / 100xxx。
    # 正文条目从 100001 银 silver ... 开始。优先以第一个有效 100001 作为正文起点。
    body_start = None
    for i, eid, tail in starts:
        if eid == "100001" and re.search(r"[\u4e00-\u9fffA-Za-z]", tail):
            body_start = i
            break
    if body_start is not None:
        starts = [(i, eid, tail) for i, eid, tail in starts if i >= body_start]
    return starts

def _is_property_or_body_line(line: str) -> bool:
    s = normalize_math_text(line)
    if not s:
        return False
    if s.startswith("!") or s.startswith("<table"):
        return True
    if any(label.strip("[]") in s for label in PROPERTY_LABELS):
        return True
    if re.search(r"\[(理化性质|热化学性质|燃烧爆炸性能|感度性能|用途|毒性|储存|储运)", s):
        return True
    return False

def collect_title_lines(lines: List[str], start_idx: int) -> str:
    """合并标题续行：支持 2009562,4-... 下一行英文名续接。"""
    first = lines[start_idx]
    m = ENTRY_START_RE.match(first)
    assert m
    tail = m.group("title_tail").strip()
    parts = [tail]
    # 只向后看少量行，遇到属性/图片/空行后属性即停
    seen_non_empty = 0
    for j in range(start_idx + 1, min(len(lines), start_idx + 5)):
        raw = lines[j].strip()
        if not raw:
            continue
        if ENTRY_START_RE.match(raw):
            break
        if _is_property_or_body_line(raw):
            break
        # 标题续行通常较短，且含英文、数字、括号或中文
        if len(raw) <= 180:
            parts.append(raw)
            seen_non_empty += 1
        if seen_non_empty >= 2:
            break
    return " ".join(parts)

def _looks_like_english_name_start(rest: str) -> bool:
    """
    判断候选切分点后面的内容是否像英文名，而不是中文化学名中的 N/O/1H 等位号片段。
    """
    r = normalize_math_text(rest).strip(" ,，;；")
    if not r:
        return False
    first_cn = re.search(r"[\u4e00-\u9fff]", r)
    first_lower_word = re.search(r"[A-Za-z]*[a-z]{3,}[A-Za-z]*", r)
    if not first_lower_word:
        return False
    if first_cn and first_cn.start() < first_lower_word.start():
        return False
    prefix = r[:first_lower_word.start()]
    if len(prefix) > 40:
        return False
    if re.search(r"[\u4e00-\u9fff]", prefix):
        return False
    return True

def _tail_looks_english(tail: str) -> bool:
    r = normalize_math_text(tail).strip(" ，,;；。")
    if not r:
        return False
    r = r.lstrip(" -–—,，;；")
    low = re.search(r"[a-z]{3,}", r)
    if not low:
        return False
    first_cn = re.search(r"[\u4e00-\u9fff]", r)
    if first_cn and first_cn.start() < low.start():
        return False
    prefix = r[:low.start()]
    if len(prefix) > 45:
        return False
    return True


def split_cn_en_title(title_tail: str) -> Tuple[str, str, str]:
    """严格拆分中文名/英文名。"""
    title_norm = normalize_math_text(title_tail)
    title_norm = re.split(
        r"(化学式|分子式|结构式|相对分子质量|相对分子量|CAS号|中文别名|英文别名)\s*[:：]?",
        title_norm,
    )[0].strip()
    title_norm = title_norm.strip(" #，,;；。")
    if not title_norm:
        return "", "", ""

    split_pos = None
    for i in range(1, len(title_norm)):
        left = title_norm[:i]
        if not re.search(r"[\u4e00-\u9fff]", left):
            continue
        prev = title_norm[i - 1]
        if not (re.match(r"[\u4e00-\u9fff\)\]）]", prev) or prev in "αβγδⅠⅡⅢⅣⅤⅥ"):
            continue
        if _tail_looks_english(title_norm[i:]):
            split_pos = i
            break

    if split_pos is not None:
        cn = title_norm[:split_pos].strip(" ，,;；。")
        en = title_norm[split_pos:].strip(" -–—;，,")
    else:
        cn = title_norm.strip()
        en = ""

    cn = normalize_subject_name(cn)
    en = normalize_math_text(en)
    en = re.sub(r"^\^\s*", "", en)
    en = re.split(r"(中文别名|英文别名|化学式|分子式|相对分子质量|CAS号)\s*[:：]?", en)[0].strip(" ,;；。")
    return cn, en, title_norm

_INLINE_CODE_RE = re.compile(r"([123]\d{5})")
_FIELD_START_RE = re.compile(
    r"(?:化学式|分子式|结构式|相对分子质量|相对分子量|CAS\s*(?:号|登记号)?|中文别名|英文别名)\s*[：:]?",
    re.I,
)
_END_SECTION_RE = re.compile(r"(?m)^\s*#{1,6}\s*(?:附录|主要参考文献|参考文献|References)\b", re.I)
_INLINE_PRECEDERS = {"。", "；", ";", "]", "】", "$", "，", ","}


def _inline_candidate_starts(md_text: str) -> List[Tuple[int, str]]:
    """Find hard entry boundaries even when OCR glued a new code to the
    previous entry's last sentence.

    The detector is intentionally structural and generic:
    - a six-digit category code beginning with 1/2/3;
    - a Chinese material title on the same physical line;
    - either line-start position or a conservative previous-entry delimiter;
    - never inside image/table/index/reference material.
    """
    lines = md_text.splitlines(keepends=True)
    offsets: List[int] = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line)

    candidates: List[Tuple[int, str]] = []
    body_started = False
    body_end = len(md_text)
    end_match = _END_SECTION_RE.search(md_text)
    if end_match:
        body_end = end_match.start()

    for line_index, raw_line in enumerate(lines):
        line_start = offsets[line_index]
        if line_start >= body_end:
            break
        line = raw_line.rstrip("\r\n")
        low = line.lower()
        if "<table" in low or "![](" in line or "![" in line and "](" in line:
            continue
        for match in _INLINE_CODE_RE.finditer(line):
            code = match.group(1)
            suffix = line[match.end():match.end() + 260]
            title_surface = _FIELD_START_RE.split(suffix, maxsplit=1)[0]
            if not re.search(r"[\u4e00-\u9fff]", title_surface):
                continue
            prefix = line[:match.start()]
            prefix_stripped = prefix.rstrip()
            at_line_start = bool(re.fullmatch(r"\s*(?:#{1,6}\s*)?", prefix))
            preceded_by_boundary = bool(prefix_stripped and prefix_stripped[-1] in _INLINE_PRECEDERS)
            if not (at_line_start or preceded_by_boundary):
                continue
            absolute = line_start + match.start()
            # Front indexes contain code + page only and therefore fail the
            # Chinese-title check above. Start the body at the first real 100001.
            if not body_started:
                if code == "100001":
                    body_started = True
                else:
                    continue
            candidates.append((absolute, code))
    # Preserve order and deduplicate the same character offset.
    out: List[Tuple[int, str]] = []
    seen: set[int] = set()
    for item in sorted(candidates, key=lambda x: x[0]):
        if item[0] in seen:
            continue
        seen.add(item[0])
        out.append(item)
    return out


def parse_entries(md_text: str) -> List[ChemEntry]:
    starts = _inline_candidate_starts(md_text)
    entries: List[ChemEntry] = []
    for idx, (start_pos, entry_id) in enumerate(starts):
        end_pos = starts[idx + 1][0] if idx + 1 < len(starts) else len(md_text)
        chunk = md_text[start_pos:end_pos]
        chunk_lines = chunk.splitlines()
        if not chunk_lines:
            continue
        # The first six characters are the hard entry code. collect_title_lines
        # then supports wrapped English names exactly as the legacy parser did.
        title_raw = collect_title_lines(chunk_lines, 0)
        cn, en, title_norm = split_cn_en_title(title_raw)
        if not cn and en:
            cn = en
        conf = 0.92
        if len(cn) < 2 or len(cn) > 120 or re.search(r"[A-Za-z]{6,}", cn):
            conf = 0.58
        start_line = md_text.count("\n", 0, start_pos) + 1
        end_line = md_text.count("\n", 0, max(start_pos, end_pos - 1)) + 1
        entries.append(
            ChemEntry(
                entry_id=entry_id,
                canonical_subject_name=cn,
                english_name=en,
                entry_type=category_by_entry_id(entry_id),
                start_line=start_line,
                end_line=end_line,
                title_raw=title_raw,
                title_normalized=title_norm,
                block_lines=chunk_lines,
                title_confidence=conf,
            )
        )
    return entries

