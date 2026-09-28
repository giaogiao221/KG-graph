from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Sequence, Tuple

from book_engine.text.evidence_self_containment import is_deictic_subject


_INDEPENDENT_VARIABLE_PROPERTIES = {
    "温度", "压力", "时间", "延迟时间", "含量", "用量", "浓度", "湿度",
    "粒度", "粒径", "直径", "药量", "埋深", "距离", "密度",
}
_NUMERIC_PROPERTIES = {
    "密度", "熔点", "沸点", "闪点", "自燃点", "爆发点", "分解温度", "峰顶温度",
    "玻璃化温度", "燃速", "爆速", "比冲", "爆压", "爆热", "生成热", "燃烧热",
    "熔化热", "熔融热", "汽化热", "粒度", "粒径", "感度", "冲击感度", "摩擦感度",
    "静电感度", "抗拉强度", "拉伸强度", "延伸率", "断裂伸长率", "模量", "官能度",
    "分散度", "压力指数", "燃速压力指数", "相对分子质量", "分子量", "含量", "质量分数",
    "体积分数", "纯度",
}
_PROPERTY_TERMS = tuple(sorted(_NUMERIC_PROPERTIES | _INDEPENDENT_VARIABLE_PROPERTIES, key=len, reverse=True))
_PROPERTY_ALT = "|".join(re.escape(x) for x in _PROPERTY_TERMS)

_CONDITION_COMPOSITION_HINT_RE = re.compile(
    r"(?:粘合剂|黏合剂|氧化剂|金属(?:燃料|粉)?|铝粉|AlH|燃料|组分|配比|配方|质量分数|含量).*?[=：:]",
    re.I,
)
_STRUCTURAL_FORMULA_RE = re.compile(
    r"(?:[A-Z][A-Za-z]?\d*){3,}|(?:CH|NH|NO|N3|Cl|Br|OH|COOH){3,}|[A-Za-z0-9()\-]{18,}",
    re.I,
)
_REPEATED_COMPARATOR_RE = re.compile(
    r"[<>≤≥]\s*[-+]?\d+(?:[.]\d+)?\s*[<>≤≥]\s*[-+]?\d+(?:[.]\d+)?|"
    r"^(?P<x>[-+]?\d+(?:[.]\d+)?)\s*(?:[<>≤≥|/;,，；])\s*(?P=x)$",
    re.I,
)
_MULTIPLE_DECIMAL_RE = re.compile(r"(?:^|(?<!\d))(?:\.\d+\.\d+|\d+\.\d+\.\d+)(?!\d)")
_SLASH_MULTI_VALUE_RE = re.compile(r"[-+]?\d+(?:[.]\d+)?\s*/\s*[-+]?\d+(?:[.]\d+)?")
_RANGE_SEP_RE = re.compile(r"(?:~|～|—|–|至|到)")

_TEMP_PROPERTIES = {"温度", "熔点", "沸点", "闪点", "自燃点", "爆发点", "分解温度", "峰顶温度", "玻璃化温度"}
_SPEED_PROPERTIES = {"燃速", "爆速"}
_LENGTH_PROPERTIES = {"粒度", "粒径", "直径", "距离", "埋深"}
_DENSITY_PROPERTIES = {"密度"}
_CONTENT_PROPERTIES = {"含量", "质量分数", "体积分数", "纯度"}
_TEMP_UNITS = {"℃", "°C", "K"}
_SPEED_UNITS = {"mm/s", "cm/s", "m/s", "km/s"}
_LENGTH_UNITS = {"μm", "um", "nm", "mm", "cm", "m", "目"}
_DENSITY_UNITS = {"g/cm3", "g/cm³", "kg/m3", "kg/m³"}
_CONTENT_UNITS = {"%", "‰", "ppm", "ppb", "vol%", "wt%"}

_SPECIFIC_VOLUME_PROPERTIES = {"比容", "比体积"}
_SPECIFIC_VOLUME_UNITS = {"m3/kg", "m³/kg", "l/kg", "L/kg", "cm3/g", "cm³/g"}
_IDENTITY_FORMULA_PROPERTIES = {"分子式", "化学式", "经验式", "假定分子式", "理论分子式"}
_NUMERIC_PLACEHOLDER_RE = re.compile(r"^(?:[一二三四五六七八九十百]+|[-—–~～/\\]|无|缺|空|未测|未检出|n/?a)$", re.I)
_ANONYMOUS_SUBJECT_RE = re.compile(r"^(?:某|一种|若干|不同)(?:.*(?:预聚物|聚合物|样品|试样|配方|材料|火药|推进剂|炸药))$")
_FORMULA_LIKE_SUBJECT_RE = re.compile(r"^[A-Z][A-Za-z0-9()\[\]·.]{2,18}$")
_PERIODIC_ELEMENTS = {
    "H","He","Li","Be","B","C","N","O","F","Ne","Na","Mg","Al","Si","P","S","Cl","Ar",
    "K","Ca","Sc","Ti","V","Cr","Mn","Fe","Co","Ni","Cu","Zn","Ga","Ge","As","Se","Br","Kr",
    "Rb","Sr","Y","Zr","Nb","Mo","Tc","Ru","Rh","Pd","Ag","Cd","In","Sn","Sb","Te","I","Xe",
    "Cs","Ba","La","Ce","Pr","Nd","Pm","Sm","Eu","Gd","Tb","Dy","Ho","Er","Tm","Yb","Lu",
    "Hf","Ta","W","Re","Os","Ir","Pt","Au","Hg","Tl","Pb","Bi","Po","At","Rn","Fr","Ra","Ac",
    "Th","Pa","U","Np","Pu","Am","Cm","Bk","Cf","Es","Fm","Md","No","Lr","Rf","Db","Sg","Bh",
    "Hs","Mt","Ds","Rg","Cn","Nh","Fl","Mc","Lv","Ts","Og",
}

_ATOMIC_WEIGHTS = {
    "H": 1.008, "He": 4.003, "Li": 6.94, "Be": 9.012, "B": 10.81, "C": 12.011,
    "N": 14.007, "O": 15.999, "F": 18.998, "Na": 22.990, "Mg": 24.305, "Al": 26.982,
    "Si": 28.085, "P": 30.974, "S": 32.06, "Cl": 35.45, "K": 39.098, "Ca": 40.078,
    "Ti": 47.867, "Cr": 51.996, "Mn": 54.938, "Fe": 55.845, "Co": 58.933, "Ni": 58.693,
    "Cu": 63.546, "Zn": 65.38, "Br": 79.904, "Ag": 107.868, "I": 126.904, "Ba": 137.327,
    "W": 183.84, "Pt": 195.084, "Au": 196.967, "Hg": 200.592, "Pb": 207.2, "Bi": 208.980,
}
_MOLAR_MASS_PROPERTIES = {"相对分子质量", "分子量", "摩尔质量"}
_IMPULSE_PROPERTIES = {"比冲", "冲量", "比冲量"}

_PERFORMANCE_PROPERTIES = {
    "燃速", "燃速压力指数", "压力指数", "比冲", "爆速", "爆压", "爆热", "卡片间隙",
    "冲击感度", "摩擦感度", "静电感度", "抗拉强度", "延伸率", "模量",
}
_GENERIC_UNRESOLVED_TEXT_SUBJECTS = {
    "材料", "液体材料", "聚合物", "预聚物", "共聚物", "样品", "试样", "体系", "配方",
}
_SENTENCE_OWNER_RISK_RE = re.compile(
    r"(?:并不能|不能保证|混合起来|开始研制|含有卤素|组成的粘合剂|一定有良好|"
    r"^\d{2,4}年代|^上的|^在.+(?:中|下)$|^如果|^当.+时|^压力(?:复合火药|推进剂)|"
    r"(?:进行|采用|利用|通过|为了|从而).{2,}(?:材料|火药|推进剂|聚合物)$)",
    re.I,
)
_FORMULATION_OWNER_END_RE = re.compile(r"(?:复合火药|推进剂|炸药|配方|体系|药剂)$")

_UNIVERSAL_PROPERTY_SUBJECTS = set(_NUMERIC_PROPERTIES) | set(_INDEPENDENT_VARIABLE_PROPERTIES) | {
    "外观", "颜色", "状态", "方法", "检验方法", "试验方法", "项目", "理化指标", "技术指标",
}
_UNIVERSAL_UNIT_SUBJECT_RE = re.compile(
    r"^(?:u?m|μm|nm|mm|cm|m|kbar|bar|mbar|Pa|kPa|MPa|GPa|℃|°C|K|s|ms|us|μs|ns|Hz|rpm|%|‰)$",
    re.I,
)
_ROLE_PREFIX_OWNER_RE = re.compile(
    r"^(?:如|例如|一些|若干|他使用的|其使用的|采用的|使用的|所用的|制得的|得到的|所得的|最后得到的|制得所需|于是|因此|从而)",
    re.I,
)
_VERBAL_FRAGMENT_OWNER_RE = re.compile(
    r"(?:得以开始|以开始|开始研制|开始反应|得到|制得|获得|生成|测得|表明|显示)$|"
    r"^(?:(?:先|再|然后|随后|将|把|对|经|用|采用))?"
    r"(?:搅拌|混合|研磨|粉碎|加热|冷却|过滤|洗涤|干燥|结晶|反应|处理|测定|检测|制备|合成)"
    r".{3,}(?:以使|从而|并使|之后|以后|至|得到|制得|均匀化|解聚集)",
    re.I,
)
_FORMULA_GROUP_OWNER_RE = re.compile(r"^(?:CH2|CH3|NH2|NO2|N3|COOH|OH|lnr|qC)$", re.I)

_EXPERIMENT_FACTOR_SUBJECTS = {
    "引发剂", "催化剂", "溶剂", "反应温度", "反应时间", "投料比", "单体", "共聚单体", "添加剂",
}
_TOC_PAGE_FACT_RE = re.compile(
    r"(?:^|\n)\s*(?:第?\d+章|\d+(?:[.．]\d+)+).{0,80}?(?:[.．·…]{2,}|[：:]\s*)?\d{1,4}\s*$"
)

_LOCAL_OWNER_TOKEN_RE = re.compile(
    r"(?P<name>(?:[A-Z][A-Z0-9+._/\-]{1,20}|"
    r"[\u4e00-\u9fffA-Za-z0-9+._/\-()]{2,36}(?:高氯酸铵|硝酸铵|火药|推进剂|炸药|"
    r"聚合物|预聚物|共聚物|均聚物|粘合剂|黏合剂|金属|粉|酸|盐|酯|醚|胺|醇|酮)))",
    re.I,
)


@dataclass(frozen=True)
class ReleaseConsistencyAudit:
    fact_id: str
    graph_fact_key: str
    source_type: str
    table_id: str
    subject: str
    property_name: str
    value_text: str
    action: str
    reasons: Tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SemanticReleaseGuardResult:
    releasable_rows: Sequence[Dict[str, object]]
    candidate_rows: Sequence[Dict[str, object]]
    rejected_rows: Sequence[Dict[str, object]]
    audits: Sequence[ReleaseConsistencyAudit]
    table_candidate_reasons: Mapping[str, Tuple[str, ...]]


def _compact(value: object) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def _unit(value: object) -> str:
    unit = _compact(value).replace("μ", "u").replace("⁻", "-").replace("−", "-")
    unit = re.sub(r"^g(?:·|\*)?cm\^?-?3$", "g/cm3", unit, flags=re.I)
    unit = re.sub(r"^kg(?:·|\*)?m\^?-?3$", "kg/m3", unit, flags=re.I)
    unit = re.sub(r"^(km|cm|mm|m)(?:·|\*)s\^?-?1$", r"\1/s", unit, flags=re.I)
    return unit.replace("^3", "3")


def _condition_names(row: Mapping[str, object]) -> List[str]:
    raw = str(row.get("structured_condition_json", "") or "")
    if raw:
        try:
            data = json.loads(raw)
            names = []
            for item in data if isinstance(data, list) else []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("normalized_name") or item.get("name") or "").strip()
                if name:
                    names.append(name)
            if names:
                return names
        except Exception:
            pass
    text = str(row.get("条件文本", "") or "")
    return [part.split("=", 1)[0].strip() for part in re.split(r"[；;]", text) if "=" in part]


def _row_number(row: Mapping[str, object]) -> str:
    match = re.search(r"(?:^|;)R(\d+)(?:;|$)", str(row.get("来源定位", "") or ""), re.I)
    return match.group(1) if match else ""



def _balanced_delimiters(value: str) -> bool:
    pairs = {")": "(", "]": "["}
    stack: List[str] = []
    for ch in value:
        if ch in "([":
            stack.append(ch)
        elif ch in ") ]".replace(" ", ""):
            if not stack or stack.pop() != pairs[ch]:
                return False
    return not stack


def _valid_formula_surface(value: str) -> bool:
    text = re.sub(r"\s+", "", value or "")
    text = text.replace("$", "").replace("{", "").replace("}", "")
    text = text.replace("\\mathrm", "").replace("\\mathbf", "")
    text = re.sub(r"[+\-−]\d*$", "", text)
    if not text or re.search(r"[\u4e00-\u9fff]", text) or not _balanced_delimiters(text):
        return False
    # Hydrates, dots and bracket/group punctuation are legal separators.
    index = 0
    element_count = 0
    while index < len(text):
        ch = text[index]
        if ch in "()[]·.+-":
            index += 1
            continue
        if ch.isdigit():
            while index < len(text) and (text[index].isdigit() or text[index] == "."):
                index += 1
            continue
        if not ch.isalpha() or not ch.isupper():
            return False
        token = ch
        if index + 1 < len(text) and text[index + 1].islower():
            token += text[index + 1]
            index += 1
        if token not in _PERIODIC_ELEMENTS:
            return False
        element_count += 1
        index += 1
    return element_count >= 1



def _normalized_formula_text(value: str) -> str:
    text = re.sub(r"\s+", "", value or "")
    text = text.replace("$", "").replace("{", "").replace("}", "")
    text = text.replace("\\mathrm", "").replace("\\mathbf", "")
    text = text.replace("[", "(").replace("]", ")")
    text = text.replace("·", ".")
    return re.sub(r"[+\-−]\d*$", "", text)


def _formula_molar_mass(value: str) -> float | None:
    text = _normalized_formula_text(value)
    if not text or not _valid_formula_surface(text):
        return None

    def parse_group(index: int, stop: str = "") -> tuple[float, int]:
        total = 0.0
        while index < len(text):
            ch = text[index]
            if stop and ch == stop:
                return total, index + 1
            if ch == ".":
                index += 1
                continue
            leading = 1
            if ch.isdigit():
                start = index
                while index < len(text) and text[index].isdigit():
                    index += 1
                leading = int(text[start:index])
                if index >= len(text):
                    return total, index
                ch = text[index]
            if ch == "(":
                subtotal, index = parse_group(index + 1, ")")
                start = index
                while index < len(text) and text[index].isdigit():
                    index += 1
                multiplier = int(text[start:index]) if index > start else 1
                total += leading * subtotal * multiplier
                continue
            if not ch.isupper():
                return 0.0, len(text)
            element = ch
            index += 1
            if index < len(text) and text[index].islower():
                element += text[index]
                index += 1
            if element not in _ATOMIC_WEIGHTS:
                return 0.0, len(text)
            start = index
            while index < len(text) and text[index].isdigit():
                index += 1
            count = int(text[start:index]) if index > start else 1
            total += leading * _ATOMIC_WEIGHTS[element] * count
        return total, index

    mass, end = parse_group(0)
    return mass if mass > 0 and end >= len(text) else None


def _numeric_scalar(row: Mapping[str, object]) -> float | None:
    for key in ("normalized_value_num", "数值", "范围下限"):
        raw = str(row.get(key, "") or "").strip()
        if raw:
            try:
                return float(raw)
            except Exception:
                pass
    match = re.search(r"[-+−]?\d+(?:[.]\d+)?", str(row.get("尾实体/取值文本", "") or ""))
    if match:
        try:
            return float(match.group(0).replace("−", "-"))
        except Exception:
            return None
    return None


def _raw_unit_semantics_lost(row: Mapping[str, object]) -> bool:
    if not str(row.get("来源类型", "") or "").startswith("text_"):
        return False
    prop = str(row.get("attribute_name") or row.get("predicate_raw") or "").strip()
    unit = str(row.get("单位") or row.get("normalized_unit") or "").strip()
    value = str(row.get("尾实体/取值文本", "") or "")
    evidence = str(row.get("证据文本", "") or "")
    if prop in _DENSITY_PROPERTIES and not unit:
        # Inspect the extracted value itself rather than the whole paragraph.
        # A later, unrelated mass unit in the evidence must not make a
        # volume-only density such as ``2.7 cm^3`` look valid.
        surface = re.sub(r"\\(?:mathrm|mathbf|mathbb|pmb|rm|bf)", "", value, flags=re.I)
        compact_value = re.sub(r"[^A-Za-z0-9/³^]", "", surface)
        has_volume = bool(re.search(r"(?:cm|m)(?:\^?3|³)", compact_value, re.I))
        has_mass_per_volume = bool(
            re.search(r"(?:g|kg)/(?:cm|m)(?:\^?3|³)", compact_value, re.I)
        )
        if has_volume and not has_mass_per_volume:
            return True
    if prop in _TEMP_PROPERTIES and not unit:
        surface = re.sub(r"\\(?:mathrm|mathbf|mathbb|pmb|rm|bf)", "", value, flags=re.I)
        if re.search(r"(?:\\circ|\\bar|°)\s*\{?\s*[CK]\s*\}?|(?:℃|°C)", surface, re.I):
            return True
    if prop in _IMPULSE_PROPERTIES and not unit:
        compact = re.sub(r"\s+", "", evidence)
        if re.search(r"N(?:[-–—·.]|\\cdot)?(?:s|8)/(?:kg|g)", compact, re.I):
            return True
    return False


def _comparator_or_unit_lost(row: Mapping[str, object]) -> bool:
    if not str(row.get("来源类型", "") or "").startswith("text_"):
        return False
    prop = str(row.get("attribute_name") or row.get("predicate_raw") or "").strip()
    if prop not in _NUMERIC_PROPERTIES | _IMPULSE_PROPERTIES:
        return False
    comparator = str(row.get("数值类型") or row.get("normalized_value_text") or "")
    value = str(row.get("尾实体/取值文本", "") or "")
    evidence = str(row.get("证据文本", "") or "")
    scalar = re.search(r"\d+(?:[.]\d+)?", value)
    if scalar and re.search(rf"(?:≥|≤|>|<|\\yen)\s*{re.escape(scalar.group(0))}", evidence) and not re.search(r"(?:≥|≤|>|<)", value + comparator):
        return True
    return False


def _table_row_group_reasons(rows: Sequence[Mapping[str, object]]) -> Dict[Tuple[str, str], Tuple[str, ...]]:
    grouped: Dict[Tuple[str, str], List[Mapping[str, object]]] = defaultdict(list)
    for row in rows:
        if str(row.get("来源类型", "") or "") != "table_conditional_record":
            continue
        table_id = str(row.get("所属表格ID", "") or "")
        row_num = _row_number(row)
        if table_id and row_num:
            grouped[(table_id, row_num)].append(row)
    result: Dict[Tuple[str, str], Tuple[str, ...]] = {}
    for key, group in grouped.items():
        reasons: List[str] = []
        formula_rows = [r for r in group if str(r.get("attribute_name") or r.get("predicate_raw") or "").strip() in _IDENTITY_FORMULA_PROPERTIES]
        mass_rows = [r for r in group if str(r.get("attribute_name") or r.get("predicate_raw") or "").strip() in _MOLAR_MASS_PROPERTIES]
        if any(_identity_formula_invalid(r) for r in formula_rows):
            reasons.append("row_identity_formula_ocr_failure")
        if formula_rows and mass_rows:
            formula_mass = _formula_molar_mass(str(formula_rows[0].get("尾实体/取值文本", "") or ""))
            stated_mass = _numeric_scalar(mass_rows[0])
            if formula_mass and stated_mass and abs(formula_mass - stated_mass) > max(2.0, stated_mass * 0.025):
                reasons.append("formula_molar_mass_inconsistent")
        # Severe identity OCR risk propagates to the whole row, because adjacent
        # numeric cells often share the same OCR alignment damage.
        if reasons:
            result[key] = tuple(sorted(set(reasons)))
    return result


def _identity_formula_invalid(row: Mapping[str, object]) -> bool:
    prop = str(row.get("attribute_name") or row.get("predicate_raw") or "").strip()
    if prop not in _IDENTITY_FORMULA_PROPERTIES:
        return False
    return not _valid_formula_surface(str(row.get("尾实体/取值文本", "") or ""))


def _formula_like_subject_invalid(row: Mapping[str, object]) -> bool:
    subject = _compact(row.get("主体名称"))
    if not _FORMULA_LIKE_SUBJECT_RE.fullmatch(subject):
        return False
    # Uppercase abbreviations without digits/lowercase are treated as material
    # codes rather than molecular formulae.
    if not re.search(r"[a-z0-9]", subject):
        return False
    return not _valid_formula_surface(subject)




_FORMULA_REQUIRED_ELEMENTS_BY_NAME_TOKEN = {
    "高氯酸": {"Cl", "O"},
    "氯酸": {"Cl", "O"},
    "氯化": {"Cl"},
    "溴化": {"Br"},
    "碘化": {"I"},
    "氟化": {"F"},
    "硝酸": {"N", "O"},
    "亚硝酸": {"N", "O"},
    "硫酸": {"S", "O"},
    "亚硫酸": {"S", "O"},
    "碳酸": {"C", "O"},
    "磷酸": {"P", "O"},
    "铬酸": {"Cr", "O"},
    "重铬酸": {"Cr", "O"},
    "高锰酸": {"Mn", "O"},
    "氢氧化": {"H", "O"},
    "氧化": {"O"},
    "硫化": {"S"},
    "铵": {"N", "H"},
}


def _formula_elements(value: str) -> set[str]:
    text = str(value or "")
    text = re.sub(r"\\(?:mathrm|mathbf|mathbb|mathit|mathrm|rm|bf)\s*", "", text, flags=re.I)
    text = re.sub(r"\\(?:cdot|times)", ".", text, flags=re.I)
    text = text.replace("$", "").replace("{", "").replace("}", "")
    text = re.sub(r"\s+", "", text)
    return set(re.findall(r"[A-Z][a-z]?", text))


def _chemical_name_formula_mismatch(row: Mapping[str, object]) -> bool:
    prop = str(row.get("attribute_name") or row.get("predicate_raw") or "").strip()
    if prop not in _IDENTITY_FORMULA_PROPERTIES:
        return False
    subject = re.sub(r"^\s*\d{6}\s*", "", str(row.get("主体名称", "") or "")).strip()
    if not subject:
        return False
    elements = _formula_elements(str(row.get("尾实体/取值文本", "") or ""))
    if not elements:
        return False
    required: set[str] = set()
    for token, token_elements in _FORMULA_REQUIRED_ELEMENTS_BY_NAME_TOKEN.items():
        if token in subject:
            required.update(token_elements)
    return bool(required and not required.issubset(elements))


def _numeric_placeholder(row: Mapping[str, object]) -> bool:
    prop = str(row.get("attribute_name") or row.get("predicate_raw") or "").strip()
    if prop not in _NUMERIC_PROPERTIES:
        return False
    value = _compact(row.get("尾实体/取值文本"))
    return bool(value and _NUMERIC_PLACEHOLDER_RE.fullmatch(value))


def _unresolved_deictic_subject(row: Mapping[str, object]) -> bool:
    return str(row.get("来源类型", "") or "").startswith("text_") and is_deictic_subject(str(row.get("主体名称", "") or ""))


def _unit_dimension_mismatch(row: Mapping[str, object]) -> bool:
    prop = str(row.get("attribute_name") or row.get("predicate_raw") or "").strip()
    unit = _unit(row.get("单位") or row.get("normalized_unit"))
    if not unit:
        return False
    if prop in _TEMP_PROPERTIES:
        return unit not in {_unit(x) for x in _TEMP_UNITS}
    if prop in _SPEED_PROPERTIES:
        return unit not in {_unit(x) for x in _SPEED_UNITS}
    if prop in _LENGTH_PROPERTIES:
        return unit not in {_unit(x) for x in _LENGTH_UNITS}
    if prop in _DENSITY_PROPERTIES:
        return unit not in {_unit(x) for x in _DENSITY_UNITS}
    if prop in _SPECIFIC_VOLUME_PROPERTIES:
        return unit not in {_unit(x) for x in _SPECIFIC_VOLUME_UNITS}
    if prop in _CONTENT_PROPERTIES:
        return unit not in {_unit(x) for x in _CONTENT_UNITS}
    return False


def _multi_property_leaf_collapse(row: Mapping[str, object]) -> bool:
    raw = str(row.get("predicate_raw", "") or "")
    prop = str(row.get("attribute_name", "") or "")
    terms = {term for term in _PROPERTY_TERMS if term in raw}
    if len(terms) >= 2 and ("/" in raw or "／" in raw or "|" in raw):
        return True
    value = str(row.get("尾实体/取值文本", "") or "")
    if _SLASH_MULTI_VALUE_RE.search(value) and prop not in {"比值", "配比", "分散度"}:
        return True
    return False


def _structural_formula_in_numeric_value(row: Mapping[str, object]) -> bool:
    if str(row.get("来源类型", "") or "") != "table_conditional_record":
        return False
    prop = str(row.get("attribute_name") or row.get("predicate_raw") or "").strip()
    if prop not in _NUMERIC_PROPERTIES:
        return False
    value = _compact(row.get("尾实体/取值文本"))
    if len(value) < 10:
        return False
    if _STRUCTURAL_FORMULA_RE.search(value) and not _RANGE_SEP_RE.search(value):
        letters = len(re.findall(r"[A-Za-z]", value))
        digits = len(re.findall(r"\d", value))
        return letters >= 5 and letters >= digits
    return False


def _glued_or_repeated_numeric(row: Mapping[str, object]) -> bool:
    if str(row.get("来源类型", "") or "") != "table_conditional_record":
        return False
    value = _compact(row.get("尾实体/取值文本"))
    if not value:
        return False
    if _REPEATED_COMPARATOR_RE.search(value) or _MULTIPLE_DECIMAL_RE.search(value):
        return True
    # A single cell that encodes several ranges and a ratio cannot be safely
    # represented as one scalar measurement.
    if value.count(":") + value.count("：") >= 2 and "=" in value:
        return True
    # Long, unseparated integer series is usually OCR collapse rather than one
    # measurement. Legitimate scalar integers such as 10000 remain valid.
    if re.fullmatch(r"\d{9,}", value):
        return not re.search(r"(?:CAS|编号|代号|分子式|结构式)", str(row.get("predicate_raw", "")), re.I)
    return False


def _malformed_owner_surface(row: Mapping[str, object]) -> bool:
    subject = _compact(row.get("主体名称"))
    if not subject:
        return True
    if re.match(r"^(?:事|制|研|用|作|测|从)(?:火药|推进剂|炸药|材料|物质|体系)$", subject):
        return True
    if re.search(r"(?:为|是|具有|可以|能够|用于|制成|得到|测得).{2,}", subject):
        return True
    return False


def _generic_polymorph_owner_without_local_evidence(row: Mapping[str, object]) -> bool:
    if not str(row.get("来源类型", "") or "").startswith("text_"):
        return False
    subject = _compact(row.get("主体名称"))
    if subject not in {"复合火药", "火药", "推进剂", "炸药", "材料", "体系"}:
        return False
    evidence = str(row.get("证据文本", "") or "")
    return bool(re.search(r"(?:晶型|晶形|变形体)", evidence))


def _text_owner_role_risks(row: Mapping[str, object]) -> List[str]:
    """Detect formally valid but semantically implausible text owners.

    These checks are deliberately generic. They do not repair evidence or use
    book/table identifiers; risky rows are preserved in the candidate layer.
    """
    if not str(row.get("来源类型", "") or "").startswith("text_"):
        return []
    subject = str(row.get("主体名称", "") or "").strip()
    compact_subject = _compact(subject)
    prop = str(row.get("attribute_name") or row.get("predicate_raw") or "").strip()
    value = str(row.get("尾实体/取值文本", "") or "")
    evidence = str(row.get("证据文本", "") or "")
    reasons: List[str] = []

    if compact_subject in _GENERIC_UNRESOLVED_TEXT_SUBJECTS:
        reasons.append("generic_unresolved_text_owner")
    if _SENTENCE_OWNER_RISK_RE.search(compact_subject) or len(compact_subject) > 42:
        reasons.append("sentence_fragment_or_role_phrase_owner")
    if re.match(r"^(?:该|此|上述|所得|得到的|合成的)", compact_subject):
        reasons.append("unresolved_reference_surface_owner")

    # A truncated chemical-class prefix must not override a longer named
    # compound in the same clause (for example an acid prefix before a salt).
    if re.search(r"(?:酸|盐|酯|醚|胺|醇|酮)$", compact_subject):
        if re.search(re.escape(compact_subject) + r"[\u4e00-\u9fff]{1,8}(?=是|为|的|，|,)", evidence):
            reasons.append("owner_is_prefix_of_more_specific_local_entity")

    # Formula/structural fragments can be useful evidence but are not released
    # as material owners when the source explicitly labels them as a structure.
    if _FORMULA_LIKE_SUBJECT_RE.fullmatch(compact_subject) and re.search(r"(?:结构|结构式|链节)", evidence):
        reasons.append("structural_formula_fragment_used_as_owner")

    # Small unitless numbers next to a grading/ratio statement are conditions,
    # not particle-size results.
    scalar = _numeric_scalar(row)
    unit = str(row.get("单位") or row.get("normalized_unit") or "").strip()
    if prop in {"粒度", "粒径"} and not unit and scalar is not None and abs(scalar) <= 10:
        if re.search(r"(?:级配比|粗细比|配比|质量比|比例)", evidence):
            reasons.append("ratio_or_grade_condition_projected_as_particle_size")

    # For an experimental metric, a component/additive named with a percentage
    # or dosage is normally a condition, while the formulation is the owner.
    if prop in _PERFORMANCE_PROPERTIES and not _FORMULATION_OWNER_END_RE.search(compact_subject):
        if (
            re.search(re.escape(subject) + r".{0,12}(?:含量|用量|质量分数|加入|添加)", evidence, re.I)
            or re.search(r"(?:加入|添加|掺入).{0,24}" + re.escape(subject), evidence, re.I)
        ):
            if re.search(r"(?:复合火药|推进剂|炸药|配方|体系)", evidence):
                reasons.append("component_or_additive_used_as_performance_owner")
        if re.search(r"(?:该配方|本配方|配方中).{0,100}(?:燃速|比冲|压力指数|感度|爆速|爆压)", evidence):
            reasons.append("component_from_formulation_list_used_as_result_owner")

    # When the immediately preceding source context names a different explicit
    # owner for the same property, keep the row for review instead of trusting a
    # more distant component mention.
    property_pos = evidence.rfind(prop) if prop else -1
    if property_pos >= 0:
        preceding = evidence[:property_pos]
        local_mentions = [m.group("name").strip() for m in _LOCAL_OWNER_TOKEN_RE.finditer(preceding[-240:])]
        if local_mentions:
            nearest = local_mentions[-1]
            if nearest != subject and prop in {"卡片间隙", "爆发点"}:
                reasons.append("nearest_local_owner_conflicts_with_selected_owner")

    return sorted(set(reasons))


def _condition_owner_consistency_risks(row: Mapping[str, object]) -> List[str]:
    if not str(row.get("来源类型", "") or "").startswith("text_"):
        return []
    subject = _compact(row.get("主体名称"))
    prop = str(row.get("attribute_name") or row.get("predicate_raw") or "").strip()
    evidence = str(row.get("证据文本", "") or "")
    condition_text = str(row.get("条件文本", "") or "")
    reasons: List[str] = []

    subject_pair = re.match(r"(?P<pair>[A-Z][A-Z0-9-]*(?:/[A-Z][A-Z0-9-]*)+)", subject, re.I)
    for pair in re.findall(r"([A-Z][A-Z0-9-]*(?:/[A-Z][A-Z0-9-]*)+)(?:摩尔比|质量比|配比)", condition_text, re.I):
        if subject_pair and pair.upper() != subject_pair.group("pair").upper():
            reasons.append("formulation_ratio_condition_conflicts_with_owner")

    if prop in _CONTENT_PROPERTIES | {"粒度", "粒径", "密度", "压力", "温度"}:
        if re.search(r"(?:当|在).{0,60}" + re.escape(prop) + r".{0,30}(?:时|下|条件下)", evidence):
            value = str(row.get("尾实体/取值文本", "") or "").strip()
            if value and not re.search(r"(?:为|达到|可达|约为|范围为).{0,20}" + re.escape(value), evidence):
                reasons.append("experimental_condition_projected_as_standalone_property")

    return sorted(set(reasons))



def _universal_owner_surface_risks(row: Mapping[str, object]) -> List[str]:
    subject = str(row.get("主体名称", "") or "").strip()
    compact = _compact(subject)
    evidence = str(row.get("证据文本", "") or "")
    reasons: List[str] = []
    if not compact:
        return ["empty_owner_surface"]
    if compact in _UNIVERSAL_PROPERTY_SUBJECTS:
        reasons.append("property_or_table_header_used_as_subject")
    if _UNIVERSAL_UNIT_SUBJECT_RE.fullmatch(compact):
        reasons.append("unit_or_measurement_token_used_as_subject")
    if _ROLE_PREFIX_OWNER_RE.search(compact) or _VERBAL_FRAGMENT_OWNER_RE.search(compact):
        reasons.append("role_phrase_or_verbal_fragment_used_as_subject")
    if _FORMULA_GROUP_OWNER_RE.fullmatch(compact):
        # Small structural groups and OCR variables are not stable material
        # subjects when the same evidence contains a longer named owner.
        named = re.findall(
            r"[\u4e00-\u9fffA-Za-z0-9+._/\-()]{3,40}(?:火药|推进剂|炸药|聚合物|共聚物|均聚物|预聚物|粘合剂|黏合剂|材料|产品|结晶)",
            evidence,
            re.I,
        )
        if named:
            reasons.append("structural_group_or_variable_used_as_subject")
    if len(compact) <= 8 and re.fullmatch(r"[a-z][a-z0-9._-]*", compact):
        reasons.append("lowercase_variable_or_ocr_fragment_used_as_subject")
    return sorted(set(reasons))


def _experimental_factor_subject(row: Mapping[str, object]) -> bool:
    subject = re.sub(r"\s+", "", str(row.get("主体名称", "") or ""))
    return subject in _EXPERIMENT_FACTOR_SUBJECTS


def _toc_or_page_index_evidence(row: Mapping[str, object]) -> bool:
    evidence = str(row.get("证据文本", "") or "").replace("\\n", "\n")
    return bool(_TOC_PAGE_FACT_RE.search(evidence))

def _row_risks(row: Mapping[str, object]) -> List[str]:
    reasons: List[str] = []
    if _unit_dimension_mismatch(row):
        reasons.append("property_unit_dimension_mismatch")
    if _multi_property_leaf_collapse(row):
        reasons.append("compound_header_leaf_collapse")
    if _structural_formula_in_numeric_value(row):
        reasons.append("structural_formula_in_numeric_value")
    if _glued_or_repeated_numeric(row):
        reasons.append("glued_or_repeated_numeric_value")
    if _identity_formula_invalid(row):
        reasons.append("invalid_chemical_formula_syntax")
    if _chemical_name_formula_mismatch(row):
        reasons.append("chemical_name_formula_element_mismatch")
    if _formula_like_subject_invalid(row):
        reasons.append("invalid_formula_like_subject")
    if _numeric_placeholder(row):
        reasons.append("numeric_placeholder_or_missing_value")
    if _unresolved_deictic_subject(row):
        reasons.append("unresolved_deictic_subject")
    if _raw_unit_semantics_lost(row):
        reasons.append("raw_unit_semantics_lost_or_incomplete")
    if _comparator_or_unit_lost(row):
        reasons.append("numeric_comparator_or_unit_lost")
    if _malformed_owner_surface(row):
        reasons.append("malformed_or_sentence_like_owner")
    if _generic_polymorph_owner_without_local_evidence(row):
        reasons.append("generic_owner_for_polymorph_measurement")
    if _experimental_factor_subject(row):
        reasons.append("experimental_factor_subject")
    if _toc_or_page_index_evidence(row):
        reasons.append("table_of_contents_or_page_index_evidence")
    reasons.extend(_text_owner_role_risks(row))
    reasons.extend(_condition_owner_consistency_risks(row))
    reasons.extend(_universal_owner_surface_risks(row))
    return sorted(set(reasons))


def _table_group_reasons(rows: Sequence[Mapping[str, object]]) -> Dict[str, Tuple[str, ...]]:
    grouped: Dict[str, List[Mapping[str, object]]] = defaultdict(list)
    for row in rows:
        if str(row.get("来源类型", "") or "") != "table_conditional_record":
            continue
        table_id = str(row.get("所属表格ID", "") or "")
        if table_id:
            grouped[table_id].append(row)

    result: Dict[str, Tuple[str, ...]] = {}
    for table_id, table_rows in grouped.items():
        reasons: List[str] = []
        properties = [str(r.get("attribute_name") or r.get("predicate_raw") or "").strip() for r in table_rows]
        condition_counts = [len(set(_condition_names(r))) for r in table_rows]
        # A table whose released measurement is almost always an independent
        # variable while each row carries many response-like condition columns
        # is an axis inversion, not a valid property table.
        independent_ratio = sum(p in _INDEPENDENT_VARIABLE_PROPERTIES for p in properties) / max(1, len(properties))
        if len(table_rows) >= 4 and independent_ratio >= 0.80 and sum(c >= 3 for c in condition_counts) >= max(3, len(table_rows) // 2):
            reasons.append("condition_response_axis_inversion")

        if any(_multi_property_leaf_collapse(r) for r in table_rows):
            reasons.append("compound_header_leaf_collapse")

        topology = Counter(str(r.get("table_semantic_type", "") or "") for r in table_rows).most_common(1)
        topology_name = topology[0][0] if topology else ""
        subjects = {str(r.get("主体名称", "") or "").strip() for r in table_rows}
        row_numbers = {_row_number(r) for r in table_rows if _row_number(r)}
        composition_rows = sum(bool(_CONDITION_COMPOSITION_HINT_RE.search(str(r.get("条件文本", "") or ""))) for r in table_rows)
        if (
            topology_name in {"composition_and_performance", "formulation_matrix"}
            and len(table_rows) >= 4
            and len(subjects) == 1
            and len(row_numbers) >= 3
            and composition_rows >= max(3, len(table_rows) // 2)
            and not next(iter(subjects), "").startswith("配方:")
        ):
            reasons.append("formulation_matrix_without_row_level_subject")

        risk_rows = 0
        risk_reason_counts: Counter[str] = Counter()
        values_by_subject_row: Dict[Tuple[str, str], List[Tuple[str, str]]] = defaultdict(list)
        for row in table_rows:
            risks = _row_risks(row)
            if risks:
                risk_rows += 1
                risk_reason_counts.update(risks)
            key = (str(row.get("主体名称", "") or ""), _row_number(row))
            values_by_subject_row[key].append((
                str(row.get("attribute_name") or row.get("predicate_raw") or ""),
                _compact(row.get("尾实体/取值文本")),
            ))
        duplicate_property_values = 0
        for pairs in values_by_subject_row.values():
            by_value: Dict[str, set[str]] = defaultdict(set)
            for prop, value in pairs:
                if value:
                    by_value[value].add(prop)
            duplicate_property_values += sum(1 for props in by_value.values() if len(props) >= 2)
        if duplicate_property_values >= 2:
            risk_rows += duplicate_property_values
            risk_reason_counts["same_value_projected_to_multiple_properties"] += duplicate_property_values

        if (
            (len(table_rows) >= 4 and risk_rows >= max(3, int(len(table_rows) * 0.25 + 0.999)))
            or (len(table_rows) >= 2 and risk_rows >= 2 and risk_rows / len(table_rows) >= 0.50)
        ):
            reasons.append("table_column_alignment_anomaly")
            reasons.extend(reason for reason, count in risk_reason_counts.items() if count >= 2)

        # Anonymous experimental subjects may be reused across rows only when
        # each conflicting value carries a distinguishing condition/sample key.
        grouped_measurements: Dict[Tuple[str, str], List[Mapping[str, object]]] = defaultdict(list)
        for row in table_rows:
            grouped_measurements[(
                str(row.get("主体名称", "") or "").strip(),
                str(row.get("attribute_name") or row.get("predicate_raw") or "").strip(),
            )].append(row)
        for (subject, _property), group_rows in grouped_measurements.items():
            if not _ANONYMOUS_SUBJECT_RE.fullmatch(subject) or len(group_rows) < 2:
                continue
            values = {_compact(row.get("尾实体/取值文本")) for row in group_rows if _compact(row.get("尾实体/取值文本"))}
            condition_signatures = {tuple(sorted(_condition_names(row))) + (str(row.get("条件文本", "") or ""),) for row in group_rows}
            if len(values) >= 2 and (len(condition_signatures) < len(group_rows) or any(not str(row.get("条件文本", "") or "").strip() for row in group_rows)):
                reasons.append("anonymous_sample_without_distinguishing_conditions")

        if reasons:
            result[table_id] = tuple(sorted(set(reasons)))
    return result


def apply_semantic_release_guard(rows: Sequence[Dict[str, object]]) -> SemanticReleaseGuardResult:
    table_reasons = _table_group_reasons(rows)
    table_row_reasons = _table_row_group_reasons(rows)
    releasable: List[Dict[str, object]] = []
    candidates: List[Dict[str, object]] = []
    rejected: List[Dict[str, object]] = []
    audits: List[ReleaseConsistencyAudit] = []

    for original in rows:
        row = dict(original)
        reasons = _row_risks(row)
        table_id = str(row.get("所属表格ID", "") or "")
        if table_id in table_reasons:
            reasons.extend(table_reasons[table_id])
        row_key = (table_id, _row_number(row))
        if row_key in table_row_reasons:
            reasons.extend(table_row_reasons[row_key])
        reasons = sorted(set(reasons))
        if reasons:
            row["_phase100_release_guard_reasons"] = "|".join(reasons)
            row["_production_action"] = "candidate"
            candidates.append(row)
            action = "candidate"
        else:
            releasable.append(row)
            action = "pass"
        audits.append(ReleaseConsistencyAudit(
            fact_id=str(row.get("fact_id", "") or ""),
            graph_fact_key=str(row.get("graph_fact_key", "") or ""),
            source_type=str(row.get("来源类型", "") or ""),
            table_id=table_id,
            subject=str(row.get("主体名称", "") or ""),
            property_name=str(row.get("attribute_name") or row.get("predicate_raw") or ""),
            value_text=str(row.get("尾实体/取值文本", "") or ""),
            action=action,
            reasons=tuple(reasons),
        ))

    return SemanticReleaseGuardResult(
        releasable_rows=releasable,
        candidate_rows=candidates,
        rejected_rows=rejected,
        audits=audits,
        table_candidate_reasons=table_reasons,
    )


__all__ = [
    "ReleaseConsistencyAudit",
    "SemanticReleaseGuardResult",
    "apply_semantic_release_guard",
]
