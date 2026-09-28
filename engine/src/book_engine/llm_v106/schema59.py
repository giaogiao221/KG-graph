from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, Sequence

from .english_aliases import load_english_property_aliases, normalize_english_alias
from .language_compat import detect_language, is_english_like

try:
    from book_engine.export.schema59_columns import SCHEMA59_COLUMNS
except Exception:  # pragma: no cover
    SCHEMA59_COLUMNS = (
        "fact_id", "graph_fact_key", "文档ID", "书名", "章节路径", "来源定位", "来源类型",
        "所属表格ID", "所属表格标题", "主体名称", "主体类型", "事实类型", "predicate_raw",
        "edge_verb", "fact_node_label", "attribute_category", "attribute_category_key", "attribute_name",
        "attribute_key", "尾实体/取值文本", "value_hidden", "value_display_policy", "value_display_reason",
        "normalized_value_text", "normalized_value_num", "normalized_unit", "数值", "范围下限", "范围上限",
        "单位", "数值类型", "value_type", "条件文本", "structured_condition_json", "condition_attributes",
        "方法名称", "process_id", "process_name", "process_type", "step_id", "step_index", "step_label",
        "step_action", "step_object", "step_condition_text", "step_result_text", "previous_step_id", "next_step_id",
        "component_name", "component_role", "component_amount_text", "component_amount_value", "component_amount_unit",
        "component_amount_attribute", "table_semantic_type", "condition_metric_role", "置信度", "抽取来源", "证据文本",
    )


RELATION_EXPORT = {
    "属性": ("属性事实", "具有"),
    "组成": ("组成事实", "包含组分"),
    "分类": ("分类关系事实", "分为"),
    "定义": ("定义关系事实", "定义为"),
    "用途": ("应用关系事实", "用于"),
    "功能": ("功能关系事实", "具有功能"),
    "影响": ("影响关系事实", "影响"),
    "因果": ("因果关系事实", "导致"),
    "比较": ("比较关系事实", "比较"),
    "方法": ("方法关系事实", "采用方法"),
    "步骤": ("方法步骤事实", "执行步骤"),
    "位置": ("位置关系事实", "位于"),
    "连接": ("连接关系事实", "连接"),
    "条件": ("条件关系事实", "条件为"),
    "命名/别名": ("命名关系事实", "又称"),
}

_ENTITY_TYPE_MAP = {
    "材料": "材料",
    "材料类别": "材料类别",
    "配方/材料组分": "配方/材料组分",
    "样品/产品": "材料",
    "性能参数": "性能参数",
    "过程/现象": "过程/现象",
    "方法/模型": "方法/模型",
    "设备/系统": "设备/系统",
    "条件/环境": "条件/环境",
    "机构/人员": "机构/人员",
    "其他明确实体": "其他实体",
}

_BAD_SUBJECT_EXACT = {
    "法", "图", "所", "称", "只", "仅", "均", "都", "不", "不仅", "缺点", "优点", "特点", "结果",
    "原因", "作用", "用途", "定义", "方法", "步骤", "条件", "性能", "材料", "体系", "过程",
}
_BAD_SUBJECT_END = (
    "只", "仅", "均", "都", "不", "不仅", "也", "的", "为", "而", "则", "所", "称", "可", "能", "会",
)
_RELATION_TOKENS = (
    "取决于", "决定于", "属于", "分为", "定义为", "称为", "用于", "用作", "具有", "含有", "包含",
    "导致", "影响", "提高", "降低", "采用", "通过", "位于", "连接", "分别为", "证明", "表明",
)
_EN_BAD_SUBJECT_EXACT = {
    "it", "this", "that", "which", "they", "these", "those", "there", "only", "also", "therefore",
    "the result", "this result", "that result", "the method", "this method", "the process", "this process",
    "advantage", "disadvantage", "result", "method", "process", "property", "value", "condition",
}
_EN_BAD_LAST_TOKENS = {
    "is", "are", "was", "were", "has", "have", "had", "can", "may", "might", "will", "would",
    "shall", "should", "could", "must", "only", "also", "therefore", "and", "or", "of", "for", "to",
}
_EN_RELATION_TOKENS = (
    "is defined as", "is known as", "also known as", "consists of", "is composed of", "contains",
    "comprises", "belongs to", "is classified as", "is used for", "is used as", "serves as",
    "results in", "leads to", "depends on", "is affected by", "is higher than", "is lower than",
)
_EN_DEICTIC_START = re.compile(r"^(?:it|this|that|these|those|which|they|there)\b", re.I)
_EN_WORD = re.compile(r"[A-Za-z][A-Za-z0-9'/-]*")
_SENTENCE_PUNCT = re.compile(r"[。；;!?！？]")
_NUMBER = re.compile(r"(?<![A-Za-z0-9])([-+]?\d+(?:[.,]\d+)?)(?:\s*[~～—-]\s*([-+]?\d+(?:[.,]\d+)?))?")

# --- v107 value_text math-notation cleanup (offline, no model call) -----------
# MinerU emits inline math as LaTeX (e.g. "$2 6 ~ \mathrm { ^ { \circ } C }$",
# "$8 6 \% \sim 8 8 \%$", "$7 1 0 0 ~ \mathrm { m / s }$").  The v106 LLM is
# required to echo values verbatim, so LaTeX scaffolding frequently reaches the
# 59-column row and defeats _NUMBER (first digit only, or nothing).  Clean the
# text used for numeric projection; 尾实体/取值文本 keeps the raw LLM echo for
# provenance — only the parsing side is changed.
_MATH_FRAGILE = re.compile(r"\\(?:operatorname|mathfrak|mathsf|mathrm|mathcal|mathbf|text)\s*\*?\s*\{\s*([^}]*)\s*\}")
_MATH_COMMAND = re.compile(r"\\(?:sim|approx|times|cdot|div|pm|leq|geq|neq|ne|le|ge|to|rightarrow|leftarrow)\b")
_MATH_LEFTOVER = re.compile(r"\\[a-zA-Z]+")
_MATH_DELIM = re.compile(r"[${}\\]")
_MATH_COMMAND_MAP = {
    "\\sim": "~", "\\approx": "~", "\\times": "×", "\\cdot": "·", "\\pm": "±",
    "\\leq": "<=", "\\geq": ">=", "\\neq": "!=", "\\ne": "!=",
    "\\to": "~", "\\rightarrow": "~", "\\leftarrow": "~",
}
_MATH_DIGIT_SPACING = re.compile(r"(?<=\d)\s+(?=\d)")
_MATH_DECIMAL_SPACING = re.compile(r"(?<=\d)\s*[.,]\s*(?=\d)")
# MinerU exponent braces: '- 1 . 7' / '1 / 2' follow '^' with spaces.
# 10-based exponents are scientific notation and stay verbatim (see guard below).
_MATH_EXPONENT_SPACING = re.compile(r"\^\s*\{?\s*([-+]?\s*[\d.][\d\s./]*)\}?")
_MATH_RATIO = re.compile(r"^\d+(?:[.,]\d+)?\s*[:：]\s*\d+(?:[.,]\d+)?$")
_MATH_CJK_RATIO = re.compile(r"^\d+(?:[.,]\d+)?\s*[一二两]+\s*[分之百]+\s*[\d一二三四五六七八九十]+")
_MATH_SCIENTIFIC = re.compile(r"1\s*0\s*\^|[×xX]\s*1\s*0|\\times")
# LaTeX formulas (de_{b2}/dt = u_{21}(T) p) are relation expressions, not numeric
# values; reject them before _NUMBER can lock onto a stray subscript digit.
_MATH_FORMULA = re.compile(
    r"(?:^|[\s(=<>~～—])d[ec][{}_0-9A-Za-z]+/d[tc]"
    r"|[=<>~～—]\s*[A-Za-zα-ωΑ-Ω_]{1,3}\s*[\^_]"
    r"|\^\s*\{?[A-Za-z]"
    r"|(?:^|[\s=<>~～—])u_[0-9A-Za-z{}]+"
    r"|/dt\b"
    r"|d[ec][{}_0-9A-Za-z]*\s*/"
)
# '86 % ~ 88 %': optional short unit token between a range separator and the
# second number defeats _NUMBER, so pairs are detected separately.  The units
# must be short (<=4 chars, no digits/punctuation) and both sides must carry
# either no unit or the SAME unit; otherwise it is not a numeric range.
_MATH_RANGE_PAIR = re.compile(r"([-+]?\d+(?:[.,]\d+)?)(?:\s*([^\d~～—\-,;，。、]{0,4}))?\s*[~～—]\s*(?:([^\d~～—\-,;，。、]{0,4})\s*)?([-+]?\d+(?:[.,]\d+)?)")
_MATH_SPACED_RUN = re.compile(r"\d(?:\s+\d)+")


def _merge_spaced_digits(text: str) -> str:
    """Merge MinerU space-split digit runs ONLY when they form a complete token.

    A run like '2 0 0' (all-space-separated digits, no letter adjacent) is the
    MinerU artifact and merges to '200'.  A mixed token like 'KCl04 10' keeps
    its space, because merging would glue the formula digit '4' to the amount
    '10' and destroy both.
    """
    def _merge(match: re.Match) -> str:
        run = match.group(0)
        start = match.start()
        if start > 0 and (text[start - 1].isalnum() or text[start - 1] == "_"):
            return run
        return run.replace(" ", "")
    return _MATH_SPACED_RUN.sub(_merge, text)


def _clean_value_math(value: str) -> str:
    """Strip LaTeX scaffolding and merge MinerU space-split digits for numeric parsing.

    Deliberately conservative:
    - only used for the numeric projection, never to rewrite 尾实体/取值文本;
    - range separators ~ ～ — and plain '数字 - 数字' survive;
    - ratios (8:2, 1:2.5) and CJK fractions (三分之二) keep their original text;
    - scientific notation (10^{-17}, ×10^{-15}, $1 0 ^ { - 8 }$) is preserved verbatim;
    - runs always: MinerU space-split digits ('0 . 1 7') occur without any '$'
      or backslash, so the fast path cannot skip the digit merge.
    """
    text = str(value or "").strip()
    if not text:
        return text
    if _MATH_RATIO.match(text) or _MATH_CJK_RATIO.match(text) or _MATH_SCIENTIFIC.search(text):
        return text
    text = _MATH_FRAGILE.sub(lambda m: m.group(1).strip(), text)
    text = _MATH_COMMAND.sub(lambda m: _MATH_COMMAND_MAP.get(m.group(0), ""), text)
    text = _MATH_LEFTOVER.sub("", text)
    text = _MATH_DELIM.sub("", text)
    # Exponents like ^ { - 1 . 7 } / ^ { 1 / 2 }: rebuild first so the digit
    # merge below does not mangle the exponent into an integer.
    text = _MATH_EXPONENT_SPACING.sub(
        lambda m: "^" + re.sub(r"[\s{}]", "", m.group(1)).replace(".", ".", 1), text
    )
    text = _MATH_DECIMAL_SPACING.sub(".", text)
    text = _merge_spaced_digits(text)
    text = text.replace("_", "")
    # Caret is kept only inside rebuilt exponents; everywhere else it is LaTeX noise.
    text = re.sub(r"\^(?!\s*[-+]?\d)", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).casefold()


def _slug(text: str) -> str:
    value = re.sub(r"\s+", "", str(text or "")).casefold()
    value = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "-", value).strip("-")
    return value[:80] or "unknown"


def span_supported(span: str, evidence: str) -> bool:
    span = str(span or "").strip()
    evidence = str(evidence or "")
    if not span:
        return False
    return span in evidence or _compact(span) in _compact(evidence)


def _subject_is_valid_zh(subject: str) -> tuple[bool, str]:
    value = re.sub(r"\s+", " ", str(subject or "")).strip(" ，,。；;：:\t\r\n")
    if len(value) < 2:
        return False, "subject_too_short"
    if len(value) > 80:
        return False, "subject_too_long"
    if value in _BAD_SUBJECT_EXACT:
        return False, "subject_stopword"
    if value.endswith(_BAD_SUBJECT_END):
        return False, "subject_bad_suffix"
    if _SENTENCE_PUNCT.search(value):
        return False, "subject_contains_sentence_boundary"
    if sum(1 for token in _RELATION_TOKENS if token in value) >= 1:
        return False, "subject_contains_relation_trigger"
    if "上述" in value and len(value) > 10:
        return False, "subject_deictic_clause"
    if re.fullmatch(r"[\d\W_]+", value):
        return False, "subject_numeric_or_symbol"
    if len(re.findall(r"[,，:：]", value)) >= 2:
        return False, "subject_clause_like"
    return True, "ok"


def _subject_is_valid_en(subject: str) -> tuple[bool, str]:
    value = re.sub(r"\s+", " ", str(subject or "")).strip(" ,.;:：，。；\t\r\n")
    lowered = value.casefold()
    if len(value) < 2:
        return False, "subject_too_short"
    if len(value) > 140:
        return False, "subject_too_long"
    if lowered in _EN_BAD_SUBJECT_EXACT or _EN_DEICTIC_START.search(lowered):
        return False, "subject_stopword"
    if _SENTENCE_PUNCT.search(value):
        return False, "subject_contains_sentence_boundary"
    words = _EN_WORD.findall(value)
    if not words and re.fullmatch(r"[\d\W_]+", value):
        return False, "subject_numeric_or_symbol"
    if len(words) > 18:
        return False, "subject_clause_like"
    if words and words[-1].casefold() in _EN_BAD_LAST_TOKENS:
        return False, "subject_bad_suffix"
    if any(token in lowered for token in _EN_RELATION_TOKENS):
        return False, "subject_contains_relation_trigger"
    if len(re.findall(r"[,;:]", value)) >= 2:
        return False, "subject_clause_like"
    return True, "ok"


def subject_is_valid(subject: str, language: str = "auto") -> tuple[bool, str]:
    selected = str(language or "auto").lower()
    if selected == "auto":
        selected = detect_language(subject).language
    if selected == "zh":
        return _subject_is_valid_zh(subject)
    english = _subject_is_valid_en(subject)
    if not english[0]:
        return english
    if selected == "mixed":
        # Preserve Chinese trigger protection for mixed-language clause fragments.
        value = re.sub(r"\s+", " ", str(subject or "")).strip(" ，,。；;：:\t\r\n")
        if any(token in value for token in _RELATION_TOKENS):
            return False, "subject_contains_relation_trigger"
    return True, "ok"


@dataclass(frozen=True)
class PropertyInfo:
    canonical_name: str
    category: str
    property_id: str
    mapped: bool = True
    source: str = "ontology"


class PropertyOntologyIndex:
    def __init__(
        self,
        rows: Sequence[Mapping[str, str]],
        *,
        english_alias_rows: Sequence[Mapping[str, str]] = (),
    ) -> None:
        self.by_alias: dict[str, PropertyInfo] = {}
        self.by_english_alias: dict[str, PropertyInfo] = {}
        self.invalid_english_aliases: list[dict[str, str]] = []
        self.english_alias_pairs: list[tuple[str, str]] = []
        canonical_by_key: dict[str, PropertyInfo] = {}
        names: list[str] = []
        for row in rows:
            canonical = str(row.get("canonical_name", "") or "").strip()
            alias = str(row.get("alias", "") or "").strip()
            if not canonical:
                continue
            info = PropertyInfo(
                canonical_name=canonical,
                category=str(row.get("attribute_category", "") or "").strip(),
                property_id=str(row.get("property_id", "") or "").strip(),
                mapped=True,
                source="ontology",
            )
            canonical_by_key.setdefault(_compact(canonical), info)
            for key in {canonical, alias}:
                if key:
                    self.by_alias.setdefault(_compact(key), info)
            if canonical not in names:
                names.append(canonical)

        for row in english_alias_rows:
            alias = str(row.get("alias", "") or "").strip()
            canonical = str(row.get("canonical_name", "") or "").strip()
            normalized = normalize_english_alias(alias)
            info = canonical_by_key.get(_compact(canonical))
            if not alias or not canonical or not normalized or info is None:
                self.invalid_english_aliases.append({"alias": alias, "canonical_name": canonical})
                continue
            mapped_info = PropertyInfo(info.canonical_name, info.category, info.property_id, True, "english_alias")
            self.by_english_alias.setdefault(normalized, mapped_info)
            self.english_alias_pairs.append((alias, info.canonical_name))
        self.preferred_names = tuple(names)

    @classmethod
    def from_tsv(cls, path: Path, english_alias_path: Path | None = None) -> "PropertyOntologyIndex":
        if not path.exists():
            return cls([])
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle, delimiter="\t"))
        alias_path = english_alias_path or (path.parent / "english_property_aliases.tsv")
        aliases = load_english_property_aliases(alias_path) if alias_path.exists() else []
        return cls(rows, english_alias_rows=aliases)

    def resolve(self, raw: str, language: str = "zh") -> PropertyInfo:
        value = re.sub(r"\s+", " ", str(raw or "")).strip("：:，,。 ")
        if not value:
            return PropertyInfo("未解析属性", "", "", False, "empty")
        exact = self.by_alias.get(_compact(value))
        if exact:
            return exact
        if is_english_like(language):
            normalized = normalize_english_alias(value)
            english_exact = self.by_english_alias.get(normalized)
            if english_exact:
                return english_exact
            candidates = [
                (len(key), info)
                for key, info in self.by_english_alias.items()
                if len(key) >= 4 and (key in normalized or normalized in key)
            ]
            if candidates:
                candidates.sort(key=lambda item: item[0], reverse=True)
                return candidates[0][1]
            # Never run compact Chinese substring matching over a Latin-only
            # property. For example, the Chinese alias "pH" must not match the
            # letters "ph" inside "morphology". Mixed units may still contain
            # Chinese property text, so retain the legacy fallback only when a
            # CJK character is present.
            if not re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", value):
                return PropertyInfo(value[:80], "", "", False, "unmapped")
        # Conservative Chinese containment: useful for forms such as “理论密度值”.
        candidates = [(len(key), info) for key, info in self.by_alias.items() if len(key) >= 2 and key in _compact(value)]
        if candidates:
            candidates.sort(key=lambda item: item[0], reverse=True)
            return candidates[0][1]
        return PropertyInfo(value[:80], "", "", False, "unmapped")

    def validation_report(self) -> dict[str, Any]:
        return {
            "ok": not self.invalid_english_aliases,
            "english_alias_count": len(self.by_english_alias),
            "invalid_english_aliases": self.invalid_english_aliases[:100],
            "invalid_english_alias_count": len(self.invalid_english_aliases),
        }


def read_tsv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle, delimiter="\t")]


def _single_line_tsv_cell(value: Any) -> str:
    """Return a cell safe for a strict one-record-per-physical-line TSV."""
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        text = str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\x00", "")
    return text.replace("\t", r"\t").replace("\n", r"\n")


def _validate_physical_tsv(path: Path, expected_rows: int) -> None:
    raw_lines = path.read_bytes().splitlines()
    if len(raw_lines) != expected_rows + 1:
        raise ValueError(
            f"Physical TSV line count mismatch: expected {expected_rows + 1}, got {len(raw_lines)}: {path}"
        )
    bad = []
    for index, line in enumerate(raw_lines, start=1):
        count = line.count(b"\t")
        if count != 58:
            bad.append((index, count))
            if len(bad) >= 20:
                break
    if bad:
        raise ValueError(f"Physical TSV delimiter damage in {path}; examples={bad}")


def write_tsv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    """Write an exact 59-column TSV with no quoting and no embedded line breaks."""
    columns = list(SCHEMA59_COLUMNS)
    if len(columns) != 59 or columns[0] != "fact_id" or columns[-1] != "证据文本":
        raise ValueError(f"Invalid schema59 columns at runtime: count={len(columns)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".strict59.tmp")
    with temp.open("w", encoding="utf-8-sig", newline="") as handle:
        handle.write("\t".join(columns) + "\n")
        for row in rows:
            cells = [_single_line_tsv_cell(row.get(name, "")) for name in columns]
            handle.write("\t".join(cells) + "\n")
    _validate_physical_tsv(temp, len(rows))
    temp.replace(path)

def _numeric_projection(value: str, unit: str) -> dict[str, Any]:
    # v107: parse the math-cleaned echo; 尾实体/取值文本 keeps the raw LLM text.
    cleaned = _clean_value_math(value)
    # Range first: '86 % ~ 88 %' defeats _NUMBER because a unit sits between the
    # numbers.  Both sides must carry no unit or the same unit; a non-range
    # ratio ('8:2') is excluded upstream in _clean_value_math.
    pair = _MATH_RANGE_PAIR.search(cleaned or "")
    if pair:
        first_pair = pair.group(1).replace(",", ".")
        left_unit = (pair.group(2) or "").strip()
        right_unit = (pair.group(3) or "").strip()
        second_pair = pair.group(4).replace(",", ".")
        # MinerU renders per-mille as '‰' — do not read it as an angle/range unit.
        if left_unit == "‰" or right_unit == "‰":
            pair = None
        if pair:
            same_unit = (not left_unit or not right_unit or left_unit == right_unit)
            try:
                equal_pair = float(first_pair) == float(second_pair)
            except ValueError:
                equal_pair = False
            if same_unit and not equal_pair:
                projection_unit = unit or right_unit or left_unit
                return {
                    "value_hidden": "是", "value_display_policy": "hide_numeric_attribute",
                    "value_display_reason": "llm_explicit_numeric_metric_v106", "normalized_value_text": value,
                    "normalized_value_num": "", "normalized_unit": projection_unit, "数值": "",
                    "范围下限": first_pair, "范围上限": second_pair,
                    "单位": projection_unit, "数值类型": "范围", "value_type": "range",
                }
    # Formula formulas are relation expressions (de_{b2}/dt = u_{21}(T) p) that
    # the v106 model occasionally reports as values; they are not numeric metrics.
    # Plain 'de_2/dt' formulas also carry no digits worth keeping.
    if _MATH_FORMULA.search(cleaned or ""):
        return {
            "value_hidden": "否", "value_display_policy": "show_non_numeric_value",
            "value_display_reason": "llm_non_numeric_visible_v106", "normalized_value_text": value,
            "normalized_value_num": "", "normalized_unit": unit, "数值": "", "范围下限": "", "范围上限": "",
            "单位": unit, "数值类型": "文本", "value_type": "text",
        }
    match = _NUMBER.search(cleaned or "")
    if not match or _MATH_RATIO.match(cleaned or "") or _MATH_CJK_RATIO.match(cleaned or ""):
        # Ratios / fractions survive cleaning untouched and must not parse as a
        # single number — this preserves the v106 text-typed output for them.
        return {
            "value_hidden": "否", "value_display_policy": "show_non_numeric_value",
            "value_display_reason": "llm_non_numeric_visible_v106", "normalized_value_text": value,
            "normalized_value_num": "", "normalized_unit": unit, "数值": "", "范围下限": "", "范围上限": "",
            "单位": unit, "数值类型": "文本", "value_type": "text",
        }
    first = match.group(1).replace(",", ".")
    second = (match.group(2) or "").replace(",", ".")
    if second:
        return {
            "value_hidden": "是", "value_display_policy": "hide_numeric_attribute",
            "value_display_reason": "llm_explicit_numeric_metric_v106", "normalized_value_text": value,
            "normalized_value_num": "", "normalized_unit": unit, "数值": "", "范围下限": first, "范围上限": second,
            "单位": unit, "数值类型": "范围", "value_type": "range",
        }
    return {
        "value_hidden": "是", "value_display_policy": "hide_numeric_attribute",
        "value_display_reason": "llm_explicit_numeric_metric_v106", "normalized_value_text": value,
        "normalized_value_num": first, "normalized_unit": unit, "数值": first, "范围下限": "", "范围上限": "",
        "单位": unit, "数值类型": "标量", "value_type": "number",
    }


def _fact_ids(document_id: str, book: str, subject: str, prop: str, value: str, condition: str, locator: str) -> tuple[str, str]:
    payload = "|".join(_compact(x) for x in (document_id, book, subject, prop, value, condition, locator))
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return f"fact:{digest[:20]}", f"gfk:{digest}"


def apply_llm_fact(
    *,
    fact: Mapping[str, Any],
    ontology: PropertyOntologyIndex,
    evidence: str,
    book_title: str,
    document_id: str,
    heading_path: str,
    source_locator: str,
    source_type: str,
    table_id: str = "",
    table_title: str = "",
    base_row: Mapping[str, Any] | None = None,
    extraction_source: str = "qwen3_max_direct_v106",
    language: str = "zh",
) -> tuple[dict[str, Any] | None, list[str]]:
    reasons: list[str] = []
    subject = re.sub(r"\s+", " ", str(fact.get("subject", "") or "")).strip(" ，,。；;：:")
    value = re.sub(r"\s+", " ", str(fact.get("value", "") or "")).strip()
    subject_span = str(fact.get("subject_span", "") or "").strip()
    value_span = str(fact.get("value_span", "") or "").strip()
    valid, reason = subject_is_valid(subject, language=language)
    if not valid:
        reasons.append(reason)
    if not value:
        reasons.append("empty_value")
    if not span_supported(subject_span, evidence):
        reasons.append("subject_span_not_supported")
    if not span_supported(value_span, evidence):
        reasons.append("value_span_not_supported")
    polarity = str(fact.get("polarity", "肯定") or "肯定").strip()
    if polarity not in {"肯定", "否定"}:
        reasons.append("invalid_polarity")
    try:
        confidence = max(0.0, min(1.0, float(fact.get("confidence", 0.0) or 0.0)))
    except Exception:
        confidence = 0.0
    if confidence < 0.50:
        reasons.append("confidence_below_review_floor")
    if reasons:
        return None, reasons

    relation_type = str(fact.get("relation_type", "属性") or "属性").strip()
    fact_type, edge_verb = RELATION_EXPORT.get(relation_type, RELATION_EXPORT["属性"])
    if polarity == "否定":
        edge_verb = "不" + edge_verb.lstrip("不")
    prop_info = ontology.resolve(str(fact.get("property", "") or ""), language=language)
    if is_english_like(language) and relation_type == "属性" and not prop_info.mapped:
        reasons.append("english_property_unmapped")
    unit = str(fact.get("unit", "") or "").strip()
    condition = str(fact.get("condition", "") or "").strip()
    subject_type = _ENTITY_TYPE_MAP.get(str(fact.get("subject_type", "") or "").strip(), "其他实体")
    row: dict[str, Any] = {name: "" for name in SCHEMA59_COLUMNS}
    if base_row:
        for name in SCHEMA59_COLUMNS:
            row[name] = base_row.get(name, "")
    locator = source_locator or str(row.get("来源定位", "") or "")
    resolved_table_id = str(table_id or row.get("所属表格ID", "") or "").strip()
    resolved_table_title = str(table_title or row.get("所属表格标题", "") or "").strip()
    fact_id, graph_key = _fact_ids(document_id, book_title, subject, prop_info.canonical_name, value, condition, locator)
    row.update(
        {
            "fact_id": fact_id,
            "graph_fact_key": graph_key,
            "文档ID": document_id,
            "书名": book_title,
            "章节路径": heading_path,
            "来源定位": locator,
            "来源类型": source_type,
            "所属表格ID": resolved_table_id,
            "所属表格标题": resolved_table_title,
            "主体名称": subject,
            "主体类型": subject_type,
            "事实类型": fact_type,
            "predicate_raw": str(fact.get("property", "") or prop_info.canonical_name),
            "edge_verb": edge_verb,
            "fact_node_label": f"{prop_info.canonical_name}事实",
            "attribute_category": prop_info.category,
            "attribute_category_key": f"cat:{_slug(prop_info.category)}",
            "attribute_name": prop_info.canonical_name,
            "attribute_key": f"cat:{_slug(prop_info.category)}::attr:{_slug(prop_info.canonical_name)}",
            "尾实体/取值文本": value,
            "条件文本": condition,
            "structured_condition_json": json.dumps([{"name": "LLM条件", "value_text": condition}], ensure_ascii=False) if condition else "[]",
            "condition_attributes": json.dumps({"LLM条件": [condition]}, ensure_ascii=False) if condition else "{}",
            "置信度": f"{confidence:.4f}",
            "抽取来源": extraction_source,
            "证据文本": evidence,
        }
    )
    row.update(_numeric_projection(value, unit))
    if relation_type == "组成":
        row.update(
            {
                "component_name": value,
                "component_role": "配方组分",
                "component_amount_text": "",
            }
        )
    if relation_type == "方法":
        row["方法名称"] = value
    if relation_type == "步骤":
        row["step_action"] = str(fact.get("property", "") or "执行")
        row["step_object"] = value
        row["step_condition_text"] = condition
    return row, reasons


def semantic_key(row: Mapping[str, Any]) -> tuple[str, ...]:
    process_id = _compact(row.get("process_id", ""))
    step_id = _compact(row.get("step_id", ""))
    if process_id or step_id:
        # Process steps are graph nodes, not ordinary repeated attribute facts.
        # Preserve the same action when it occurs in different processes/positions.
        return ("process-step", _compact(row.get("书名", "")), process_id, step_id)
    return tuple(
        _compact(row.get(name, ""))
        for name in ("书名", "主体名称", "attribute_name", "尾实体/取值文本", "条件文本")
    )


def deduplicate(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    selected: dict[tuple[str, ...], dict[str, Any]] = {}
    for original in rows:
        row = dict(original)
        key = semantic_key(row)
        if not all(key[:4]):
            continue
        try:
            confidence = float(row.get("置信度", 0.0) or 0.0)
        except Exception:
            confidence = 0.0
        previous = selected.get(key)
        if previous is None:
            selected[key] = row
            continue
        try:
            previous_conf = float(previous.get("置信度", 0.0) or 0.0)
        except Exception:
            previous_conf = 0.0
        # Prefer explicit LLM corrected facts, then higher confidence, then fuller evidence.
        score = ("qwen3" in str(row.get("抽取来源", "")), confidence, len(str(row.get("证据文本", ""))))
        prev_score = ("qwen3" in str(previous.get("抽取来源", "")), previous_conf, len(str(previous.get("证据文本", ""))))
        if score > prev_score:
            selected[key] = row
    return list(selected.values())


def validate_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    fact_ids: set[str] = set()
    graph_keys: set[str] = set()
    for index, row in enumerate(rows, start=2):
        for field in ("fact_id", "graph_fact_key", "书名", "主体名称", "attribute_name", "尾实体/取值文本", "证据文本"):
            if not str(row.get(field, "") or "").strip():
                errors.append({"row": index, "reason": f"empty_{field}"})
        if str(row.get("来源类型", "") or "") == "llm_table_direct":
            if not str(row.get("所属表格ID", "") or "").strip():
                errors.append({"row": index, "reason": "empty_所属表格ID"})
        fid = str(row.get("fact_id", "") or "")
        gfk = str(row.get("graph_fact_key", "") or "")
        if fid in fact_ids:
            errors.append({"row": index, "reason": "duplicate_fact_id"})
        if gfk in graph_keys:
            errors.append({"row": index, "reason": "duplicate_graph_fact_key"})
        fact_ids.add(fid)
        graph_keys.add(gfk)
    return {
        "ok": not errors,
        "column_count": len(SCHEMA59_COLUMNS),
        "rows": len(rows),
        "unique_fact_ids": len(fact_ids),
        "unique_graph_fact_keys": len(graph_keys),
        "errors": errors[:100],
        "error_count": len(errors),
    }


__all__ = [
    "SCHEMA59_COLUMNS", "PropertyOntologyIndex", "read_tsv", "write_tsv", "span_supported",
    "subject_is_valid", "apply_llm_fact", "deduplicate", "validate_rows", "semantic_key",
]
