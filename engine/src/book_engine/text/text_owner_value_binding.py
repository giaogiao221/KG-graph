from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, List

from book_engine.core.schemas import ConditionAtom
from book_engine.routing.heading_subject_resolver import is_entity_surface
from book_engine.routing.subject_name_normalizer import normalize_subject_name
from book_engine.tables.value_parser import parse_value
from book_engine.text.evidence_self_containment import compact_ocr_numeric_text

_PROPERTY_TERMS = (
    "相对分子质量", "分子量", "质量分数", "体积分数", "密度", "熔点", "沸点", "闪点", "自燃点",
    "爆发点", "分解温度", "峰顶温度", "玻璃化温度", "爆速", "爆压", "爆热", "燃速", "比冲",
    "氧平衡", "生成热", "生成焓", "燃烧热", "熔化热", "熔融热", "汽化热", "纯度",
    "有效成分含量", "氧化剂含量", "粘合剂含量", "黏合剂含量", "固体含量", "水分含量",
    "挥发分含量", "灰分含量", "氮含量", "氧含量", "氯含量", "铝含量", "硼含量", "金属含量", "含量",
    "粒径", "粒度", "黏度", "粘度", "压力指数", "燃速压力指数", "释能时间", "释能功率",
    "抗拉强度", "拉伸强度", "延伸率", "断裂伸长率", "官能度", "分散度", "毒性", "外观",
    "粗粒度", "细粒度", "卡片间隙", "临界直径", "相变温度",
)
_PROPERTY_PATTERN = "|".join(sorted((re.escape(x) for x in _PROPERTY_TERMS), key=len, reverse=True))
_PROPERTY_RE = re.compile(rf"(?P<property>{_PROPERTY_PATTERN})", re.I)

_UNIT_PATTERN = (
    r"N(?:·|\.|•)?s/kg|N(?:·|\.|•)?s/g|kJ/cm3|kJ/mol|kJ/kg|J/g|cal/g|mm/s|cm/s|km/s|m/s|"
    r"°C/min|℃/min|K/min|r/min|g/cm(?:3|³)|kg/m(?:3|³)|mol/L|mg/L|g/L|Pa·s|Pa\.s|"
    r"GPa|MPa|kPa|mbar|bar|Pa|kHz|MHz|Hz|rpm|ppm|ppb|vol%|wt%|μg|ug|mg|kg|g|μm|um|nm|mm|cm|"
    r"ns|μs|us|ms|min|℃|°C|‰|%|目|张|类|K|V|mV|kV|A|mA|m|s|h|d|P"
)
_NUMBER = r"[-+−]?(?:\d+(?:[.]\d+)?|\.\d+)"
_VALUE_TOKEN = rf"{_NUMBER}(?:\s*(?:~|～|—|–|至|到|±)\s*{_NUMBER})?\s*(?:{_UNIT_PATTERN})?"
_VALUE_RE = re.compile(
    rf"(?P<value>(?:≥|≤|>|<|约|不到|不小于|不大于)?\s*{_VALUE_TOKEN})",
    re.I,
)
_CONDITION_BEFORE_RESULT_RE = re.compile(
    rf"^\s*(?:在|于)?\s*(?P<condition_value>{_VALUE_TOKEN})\s*"
    r"(?P<condition_name>压力|温度|时间|延迟期|含量|用量|浓度|湿度|粒度|粒径|密度)"
    r"\s*(?:下|时|条件下)?\s*(?:为|是|达到|可达)?\s*"
    rf"(?P<result>{_VALUE_TOKEN})",
    re.I,
)
_POSTPOSED_OWNER_RE = re.compile(
    rf"(?P<property>{_PROPERTY_PATTERN})\s*(?:为|是|约为|达到|可达)?\s*"
    rf"(?P<value>{_VALUE_TOKEN})\s*的\s*"
    r"(?P<owner>[A-Za-z0-9+._/\-（）()\u4e00-\u9fff]{2,50}?)"
    r"(?=(?:的(?:性能|力学性能|热性能|物理性质|化学性质)|是|为|具有|呈|可|，|,|。|；|;|$))",
    re.I,
)

_CHINESE_ENTITY_END_RE = re.compile(
    r"(?:火药|推进剂|炸药|药剂|材料|物质|化合物|氧化物|聚合物|预聚物|共聚物|均聚物|"
    r"粘合剂|黏合剂|增塑剂|催化剂|交联剂|固化剂|稳定剂|燃料|氧化剂|金属|粉|橡胶|树脂|"
    r"系统|装置|设备|机构|组件|燃烧室|反应器|混合机|研磨室|容器|产品|产物|体系)$"
)
_FORMULA_RE = re.compile(r"^(?=.*[A-Za-z])(?=.*(?:\d|[A-Z].*[A-Z]))[A-Za-z][A-Za-z0-9+._/\-()]{1,40}$")
_ACRONYM_MATERIAL_RE = re.compile(
    r"^(?:AP|AN|HMX|RDX|TNT|PETN|CL-20|NTO|ADN|GAP|HTPB|BAMO|NIMMO|PCP|PEG|PECH|FOE)"
    r"(?:[/+\-][A-Za-z0-9]+)*(?:复合火药|推进剂|体系|基ETPE)?$",
    re.I,
)
_FORMULATION_EQ_RE = re.compile(r"(?P<name>(?:[A-Z][A-Z0-9-]*)(?:/(?:[A-Z][A-Z0-9-]*))+?)\s*=\s*\d+(?:[.]\d+)?\s*/\s*\d+(?:[.]\d+)?", re.I)
_FORMULATION_RATIO_CONDITION_RE = re.compile(
    r"(?P<name>(?:[A-Z][A-Z0-9-]*)(?:/(?:[A-Z][A-Z0-9-]*))+?)\s*=\s*"
    r"(?P<left>\d+(?:[.]\d+)?)\s*/\s*(?P<right>\d+(?:[.]\d+)?)",
    re.I,
)
_GENERIC_MATERIAL_OWNERS = {"复合火药", "火药", "推进剂", "炸药", "材料", "体系"}
_DISCOURSE_PREFIX_RE = re.compile(
    r"^(?:例如|其中|同时|此外|而|但|目前|还有|一般情况下|通常|资料报道|研究表明|结果表明)\s*"
)
_NON_OWNER_RE = re.compile(r"^(?:一般|通常|范围|性能|性质|结果|条件|参数|温度|压力|时间|含量|用量|浓度|粒度|粒径|粉)$")
_UNIT_OR_VARIABLE_OWNER_RE = re.compile(
    r"^(?:MPa|kPa|GPa|Pa|bar|mbar|kJ/mol|kJ/kg|J/g|cal/g|N[·.]?s/kg|K|℃|°C|mm/s|cm/s|km/s|m/s|mm|cm|m|s|ms|μs|ns|Hz|rpm|%|‰|[a-zA-Z]\d*)$", re.I
)
_OWNER_LEADING_CONDITION_RE = re.compile(
    rf"^(?:在|于|当)?\s*{_VALUE_TOKEN}\s*(?:压力|温度|时间|延迟期|含量|用量|浓度|湿度|粒度|粒径|密度)",
    re.I,
)
_OWNER_NOISE_RE = re.compile(
    r"(?:^图\s*\d|^表\s*\d|关系|变化规律|实验结果|测得|采用了|采用|我们在|两个波段|"
    r"可以看出|由图|从图|见图|如图|用于比较|进行比较|分别为|含量相同|总固体含量|"
    r"如表.{0,20}所示|由式|从式|textcircled|circled)",
    re.I,
)
_PREPROPERTY_DELAY_RE = re.compile(r"(?P<value>\d+(?:[.]\d+)?)\s*(?:s|秒|秒钟)(?:延迟期|延迟)?(?:的)?$", re.I)
_DELAY_EXPLOSION_RE = re.compile(
    rf"(?P<delay>{_NUMBER})\s*(?:s|秒|秒钟)(?:延迟期|延迟)?(?:的)?\s*"
    r"(?:爆发点)?\s*(?:为|是|达到|可达)?\s*"
    rf"(?P<result>{_NUMBER}\s*(?:℃|°C|K))",
    re.I,
)


_EXPERIMENT_CONTEXT_RE = re.compile(
    r"(?P<owner>[A-Za-z0-9+._/\-（）()\u4e00-\u9fff]{2,50}?)的"
    r"(?P<property>[A-Za-z0-9+._/\-（）()\u4e00-\u9fff]{2,30}?)"
    r"(?:实验|试验|测试)\s*[：:]",
    re.I,
)
_CONDITIONAL_RESULT_PAIR_RE = re.compile(
    rf"(?:当|在)?\s*(?:(?P<owner>[A-Za-z0-9+._/\-（）()\u4e00-\u9fff]{{1,40}}?)\s*)?"
    rf"(?P<condition_property>{_PROPERTY_PATTERN})\s*(?:为|是|达到)?\s*"
    rf"(?P<condition_value>{_VALUE_TOKEN})\s*(?:时|下|条件下)\s*[，,]?\s*"
    rf"(?:(?P<result_property>[A-Za-z\u4e00-\u9fff]{{2,30}}?)\s*(?:为|是|达到|可达))?\s*"
    rf"(?:为|是|达到|可达)?\s*(?P<result>{_VALUE_TOKEN})",
    re.I,
)
_POLYMORPH_PROPERTY_RE = re.compile(
    rf"(?P<form>[αβγδεζηθικλμνξοπρστυφχψωA-Za-z0-9]+)\s*[-–—]?\s*晶型"
    rf"(?P<property>{_PROPERTY_PATTERN})\s*(?:为|是|约为|达到|可达)?\s*(?P<value>{_VALUE_TOKEN})",
    re.I,
)
_PROPERTY_VALUE_PAIR_RE = re.compile(
    rf"(?P<property>{_PROPERTY_PATTERN})\s*(?:为|是|约为|达到|可达|范围为)?\s*(?P<value>{_VALUE_TOKEN})",
    re.I,
)
_DERIVED_FORMULATION_OWNER_RE = re.compile(
    r"(?P<material>[A-Za-z0-9+._/\-（）()\u4e00-\u9fff]{2,45})"
    r"(?:的)?(?:粘度|黏度|流动性|加工性|分子量|相对分子质量)"
    r"[^。；;]{0,45}?(?:可|能够|可以)?制(?:成|得|备成)",
    re.I,
)
_MOLECULAR_LEVEL_PROPERTIES = {"分子量", "相对分子质量", "官能度", "玻璃化温度", "熔点"}
_FORMULATION_SUFFIX_RE = re.compile(r"(?:复合火药|推进剂|炸药|配方|体系)$")
_MATERIAL_MENTION_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9+._/\-()]{1,40}|"
    r"[\u4e00-\u9fffA-Za-z0-9+._/\-()]{2,40}(?:高氯酸铵|硝酸铵|酸铵|酸盐|硝酸酯|"
    r"火药|推进剂|炸药|药剂|材料|物质|化合物|氧化物|聚合物|预聚物|共聚物|均聚物|"
    r"粘合剂|黏合剂|增塑剂|催化剂|交联剂|固化剂|金属|粉|橡胶|树脂|酸|盐|酯|醚|胺|醇|酮)"
)
_CHINESE_MATERIAL_MENTION_RE = re.compile(
    r"(?P<name>[\u4e00-\u9fffA-Za-z0-9+._/\-()]{0,32}?(?:高氯酸铵|硝酸铵|酸铵|酸盐|硝酸酯|"
    r"火药|推进剂|炸药|药剂|材料|物质|化合物|氧化物|聚合物|预聚物|共聚物|均聚物|"
    r"粘合剂|黏合剂|增塑剂|催化剂|交联剂|固化剂|金属|粉|橡胶|树脂|酸|盐|酯|醚|胺|醇|酮))"
)

_LATEX_COMMAND_RE = re.compile(
    r"\\(?:mathbf|mathrm|textbf|mathcal|pmb|textrm|mathsf|mathfrak|text|operatorname|"
    r"scriptstyle|displaystyle|bf|rm|overline|bar|underline)\s*"
)


@dataclass(frozen=True)
class BoundNumericFact:
    owner: str
    property_name: str
    value_text: str
    clause: str
    owner_source: str
    confidence: float
    conditions: List[ConditionAtom] = field(default_factory=list)


def normalize_scientific_text(text: str) -> str:
    """Normalize OCR/LaTeX surfaces while preserving clause semantics.

    The normalization is deliberately local: it repairs visually split numbers,
    units and chemical formulae, but never invents a missing value or unit.
    """
    value = text or ""
    # OCR/LaTeX comparison and impulse-unit variants are normalized only when
    # their local surface is unambiguous.
    value = re.sub(r"\\yen(?=\s*[-+−]?\d)", "≥", value, flags=re.I)
    # List markers such as \textcircled{2} are not entity surfaces.
    value = re.sub(r"\\textcircled\s*\{[^{}]*\}", " ", value, flags=re.I)
    value = value.replace("\\sim", "～").replace("\\cdot", "·")
    value = value.replace("\\times", "×").replace("\\ast", "*")
    value = value.replace("\\textdegree", "°").replace("\\circ", "°")
    # Repair micro-unit commands before the generic LaTeX command stripper.
    # The previous order removed ``\mu`` entirely and turned ``30 \mu m``
    # into ``30 m``, which then failed the length-dimension gate.
    value = value.replace("\\textmu", "μ")
    value = re.sub(r"\\(?:mu|micro)\b", "μ", value, flags=re.I)
    value = _LATEX_COMMAND_RE.sub("", value)
    value = re.sub(r"\\[A-Za-z]+", " ", value)
    value = value.replace("$", "")
    value = re.sub(r"_\s*\{\s*([^{}]+?)\s*\}", lambda m: re.sub(r"\s+", "", m.group(1)), value)
    value = re.sub(r"\^\s*\{\s*([^{}]+?)\s*\}", lambda m: re.sub(r"\s+", "", m.group(1)), value)
    value = value.replace("{", "").replace("}", "")
    # Any remaining slash is a stripped LaTeX command delimiter, not content.
    value = value.replace("\\", " ")
    # Canonicalize superscript-minus glyphs and OCR multiplication marks before
    # value matching.  These replacements are surface-preserving and do not
    # invent a missing exponent.
    value = value.replace("⁻", "-").replace("−", "-")
    value = value.replace("×", "*").replace("•", "·")
    # Compact the right-hand digits before repairing OCR decimal separators.
    value = compact_ocr_numeric_text(value)
    # LaTeX wrappers often leave individual Latin unit letters separated
    # (``c m``, ``k J``).  The legacy normalizer already compacts such runs
    # later; doing it here lets the composite-unit repair see the full unit.
    value = re.sub(r"(?<=[A-Za-z0-9])\s+(?=[A-Za-z0-9])", "", value)
    value = re.sub(r"μ\s+(?=m\b)", "μ", value, flags=re.I)
    # Scientific units emitted by PDF-to-Markdown commonly arrive as
    # ``g · cm ^ -3`` / ``cm * s ^ -1`` after LaTeX cleanup.  Normalize only
    # complete, explicit exponent surfaces; incomplete strings such as
    # ``g·cm-`` remain unresolved.
    value = re.sub(r"kg\s*(?:·|\*)?\s*m\s*\^?\s*-\s*3\b", "kg/m3", value, flags=re.I)
    value = re.sub(r"g\s*(?:·|\*)?\s*cm\s*\^?\s*-\s*3\b", "g/cm3", value, flags=re.I)
    value = re.sub(r"kJ\s*(?:·|\*)?\s*cm\s*\^?\s*-\s*3\b", "kJ/cm3", value, flags=re.I)
    value = re.sub(r"kJ\s*(?:·|\*)?\s*mol\s*\^?\s*-\s*1\b", "kJ/mol", value, flags=re.I)
    value = re.sub(r"J\s*(?:·|\*)?\s*g\s*\^?\s*-\s*1\b", "J/g", value, flags=re.I)
    value = re.sub(r"(?P<u>km|cm|mm|m)\s*(?:·|\*)\s*s\s*\^?\s*-\s*1\b", r"\g<u>/s", value, flags=re.I)
    value = re.sub(r"N\s*(?:·|\*)\s*s\s*\^?\s*kg\s*\^?\s*-\s*1\b", "N·s/kg", value, flags=re.I)
    value = re.sub(r"μ\s*m\b", "μm", value, flags=re.I)
    value = re.sub(r"u\s*m\b", "um", value, flags=re.I)
    value = re.sub(r"(?<=\d)\s*_\s*(?=(?:μm|um)\b)", " ", value, flags=re.I)
    # Chinese OCR commonly uses a comma or centred dot for the decimal point.
    value = re.sub(r"(?<=\d),(?=\d{1,2}(?:\D|$))", ".", value)
    value = re.sub(
        rf"(?P<a>\d)\s*·\s*(?P<b>\d{{1,3}})(?=\s*(?:{_UNIT_PATTERN})(?:\b|\s|$))",
        r"\g<a>.\g<b>",
        value,
        flags=re.I,
    )
    value = re.sub(r"(?<=\b[A-Za-z])\s+(?=\d)", "", value)
    value = re.sub(r"(?<=\d)\s+(?=[A-Za-z]\b)", "", value)
    value = re.sub(r"(?<=[A-Za-z0-9])\s+(?=[A-Za-z0-9])", "", value)
    value = re.sub(r"\s*/\s*", "/", value)
    value = re.sub(rf"(?<=\d)\s*~\s*(?=(?:{_UNIT_PATTERN})(?:\b|\s|$))", "", value, flags=re.I)
    value = re.sub(r"\s*(?:·|•)\s*(?=[A-Za-z])", "·", value)
    value = re.sub(r"(?:\^\s*)?°\s*C", "℃", value, flags=re.I)
    value = re.sub(r"(?<=\d)\s*[°^]?\s*C\b", "℃", value, flags=re.I)
    value = re.sub(r"\bdeg\s*C\b", "℃", value, flags=re.I)
    value = re.sub(r"N\s*[.]\s*s/kg", "N·s/kg", value, flags=re.I)
    value = re.sub(r"N\s*[-–—·.]?\s*(?:s|8)\s*/\s*kg", "N·s/kg", value, flags=re.I)
    value = re.sub(r"N\s*[-–—·.]?\s*(?:s|8)\s*/\s*g", "N·s/g", value, flags=re.I)
    # Normalize ranges where OCR repeats the unit on both bounds:
    # ``85 ℃ ～ 90 ℃`` -> ``85～90 ℃``.  This preserves both bounds for
    # parse_value instead of silently publishing the lower bound only.
    repeated_unit_range = re.compile(
        rf"(?P<a>{_NUMBER})\s*(?P<u>{_UNIT_PATTERN})\s*(?P<sep>~|～|—|–|至|到)\s*"
        rf"(?P<b>{_NUMBER})\s*(?P=u)",
        re.I,
    )
    value = repeated_unit_range.sub(lambda m: f"{m.group('a')}～{m.group('b')} {m.group('u')}", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _clean_owner(value: str) -> str:
    text = normalize_subject_name(value or "").canonical_name
    text = re.sub(r"^\s*(?:第?\d+(?:[.．]\d+)*[章节]?)\s*", "", text)
    text = _DISCOURSE_PREFIX_RE.sub("", text).strip(" ，,。；;：:的")
    text = re.sub(r"^(?:大多数|多数|各种|几种|要求)", "", text).strip()
    text = re.sub(
        r"^(?:我们在|我们|两个波段测得的|测得的|采用了|采用|使用了|使用|加入了|加入|"
        r"以|对于|对|在|当|由|从|于)",
        "",
        text,
    ).strip()
    text = re.sub(r"^(?:氧化物|化合物|材料)\s+(?=[A-Za-z])", "", text)
    text = re.sub(r"(?:作|作为|用作|做为)(?:一种)?(?:粘合剂|黏合剂|增塑剂|催化剂|交联剂|固化剂|燃料|氧化剂).*$", "", text)
    text = re.sub(r"(?:一般|通常|主要|基本|则|也)$", "", text).strip(" 的，,。；;：:")
    return text


def _owner_like(value: str) -> bool:
    owner = _clean_owner(value)
    if not owner or len(owner) > 48 or _NON_OWNER_RE.fullmatch(owner):
        return False
    if owner.count("(") != owner.count(")") or owner.count("（") != owner.count("）"):
        return False
    compact = re.sub(r"\s+", "", owner)
    if _UNIT_OR_VARIABLE_OWNER_RE.fullmatch(compact) or _OWNER_NOISE_RE.search(compact):
        return False
    if _OWNER_LEADING_CONDITION_RE.search(compact):
        return False
    if re.search(r"(?:为|是|具有|达到|可达|提高|降低|增加|减少|影响|导致|使得|使|令|表明|显示|说明|报道|测定|制造|制备|合成|从事)", compact):
        return False
    # OCR/chapter-prefix trimming can leave a verbal residue before a generic
    # material class (e.g. “从事火药” -> “事火药”). Such surfaces are not
    # entities and must never outrank a local material mention.
    if re.match(r"^(?:事|制|研|用|作|测|从)(?:火药|推进剂|炸药|材料|物质|体系)$", compact):
        return False
    if re.match(r"^(?:\d|[-+−.]|MPa|kPa|GPa|Pa|℃|°C)", compact, re.I):
        return False
    if re.search(r"(?:单|二|三|多)官能度.*(?:和|及|、).*(?:预聚物|聚合物)", compact):
        return False
    if _ACRONYM_MATERIAL_RE.fullmatch(compact) or _FORMULA_RE.fullmatch(compact):
        return True
    if _CHINESE_ENTITY_END_RE.search(compact):
        return True
    return is_entity_surface(compact)




def _last_specific_material_mention(text: str) -> str:
    candidates: List[str] = []
    for match in _CHINESE_MATERIAL_MENTION_RE.finditer(text or ""):
        value = _clean_owner(match.group("name"))
        value = re.sub(r"^(?:制造|制备|生产)?(?:复合火药|推进剂|炸药)?(?:所)?用的", "", value).strip()
        chemical_surface = bool(re.search(r"(?:高氯酸铵|硝酸铵|酸铵|酸盐|硝酸酯|酸|盐|酯|醚|胺|醇|酮)$", value))
        if not value or value in _GENERIC_MATERIAL_OWNERS or not (_owner_like(value) or chemical_surface):
            continue
        candidates.append(value)
    for match in re.finditer(
        r"(?P<name>[\u4e00-\u9fff]{0,10}聚[\u4e00-\u9fff]{2,18}?)(?=粘度|黏度|流动性|加工性|，|,|。|；|;|可|$)",
        text or "",
    ):
        value = _clean_owner(match.group("name"))
        if value and value not in _GENERIC_MATERIAL_OWNERS:
            candidates.append(value)
    for match in re.finditer(r"[A-Za-z][A-Za-z0-9+._/\-()]{1,40}", text or ""):
        value = _clean_owner(match.group(0))
        if _UNIT_OR_VARIABLE_OWNER_RE.fullmatch(value) or re.fullmatch(
            r"(?:g/cm(?:3|³)|kg/m(?:3|³)|kJ/mol|kJ/kg|J/g|cal/g|N[·.]?s/kg|g|kg|mg|cm|mm|m|s|Pa|kPa|MPa)(?:[/^0-9A-Za-z·.]+)?", value, re.I
        ):
            continue
        if not value or value in _GENERIC_MATERIAL_OWNERS or not _owner_like(value):
            continue
        candidates.append(value)
    return candidates[-1] if candidates else ""


def _best_derived_formulation_base(text: str) -> str:
    """Choose the most specific binder/polymer that yields a formulation.

    The selection is semantic rather than book-specific: terminal-functional
    polymers and explicit acronyms outrank state descriptors such as
    “未固化的…”.
    """
    candidates: List[str] = []
    for match in _CHINESE_MATERIAL_MENTION_RE.finditer(text or ""):
        value = _clean_owner(match.group("name"))
        value = re.sub(r"^(?:未固化的|已固化的|固化后的|液态的|低黏度的|低粘度的)", "", value).strip()
        if value and value not in _GENERIC_MATERIAL_OWNERS and _owner_like(value):
            candidates.append(value)
    for match in re.finditer(
        r"(?P<name>(?:(?:端羟基|端羧基|二羟基|端氨基|羟基|羧基))?聚[\u4e00-\u9fff]{2,18}?)"
        r"(?=(?:是|的(?:粘度|黏度|流动性|加工性)|粘度|黏度|流动性|加工性|，|,|。|；|;|可|$))",
        text or "",
    ):
        value = _clean_owner(match.group("name"))
        value = re.sub(r"(?:是近代|是现代|的粘度|的黏度).*$", "", value).strip()
        if value and value not in _GENERIC_MATERIAL_OWNERS and _owner_like(value):
            candidates.append(value)
    for match in re.finditer(r"[A-Za-z][A-Za-z0-9+._/\-()]{1,40}", text or ""):
        value = _clean_owner(match.group(0))
        if value and value not in _GENERIC_MATERIAL_OWNERS and _owner_like(value):
            candidates.append(value)
    if not candidates:
        return ""

    def score(value: str) -> tuple[int, int]:
        specificity = 0
        if re.search(r"(?:端羟基|端羧基|二羟基|端氨基|端叠氮基)", value):
            specificity += 8
        if _ACRONYM_MATERIAL_RE.fullmatch(value) or re.search(r"[A-Z]{2,}", value):
            specificity += 5
        if re.search(r"(?:聚合物|预聚物|共聚物|均聚物|橡胶|树脂|粘合剂|黏合剂)$", value):
            specificity += 3
        specificity += min(len(value), 30) // 6
        return specificity, len(value)

    return max(candidates, key=score)


def _role_adjusted_owner(owner: str, property_name: str, clause: str) -> str:
    value = _clean_owner(owner)
    if not value:
        return value
    # Molecular-level properties normally belong to the polymer/binder named
    # immediately before a formulation suffix, not to the derived formulation.
    if property_name in _MOLECULAR_LEVEL_PROPERTIES and _FORMULATION_SUFFIX_RE.search(value):
        stem = _FORMULATION_SUFFIX_RE.sub("", value).strip(" -/的")
        if stem and stem not in _GENERIC_MATERIAL_OWNERS and (
            re.search(r"(?:聚合物|预聚物|共聚物|均聚物|橡胶|树脂|粘合剂|黏合剂)$", stem)
            or re.search(r"(?:GAP|HTPB|BAMO|AMMO|NIMMO|PCP|PEG|PECH|FOE)$", stem, re.I)
        ):
            return stem
    return value


def _extract_owner_from_prefix(prefix: str, carry_owner: str) -> tuple[str, str, float]:
    value = prefix.strip(" ，,。；;：:")
    if not value:
        return (carry_owner, "clause_carry_owner", 0.89) if carry_owner else ("", "", 0.0)
    if re.search(r"(?:它|其|该材料|该物质|该化合物|该聚合物|上述材料|上述物质)\s*的?$", value):
        return (carry_owner, "clause_pronoun_owner", 0.91) if carry_owner else ("", "", 0.0)
    value = re.split(r"[，,；;。]|(?:而|但是|同时|其中)", value)[-1]
    value = re.sub(r"^(?:例如|如|其中|而|但|同时|对于|对|在)", "", value).strip()
    value = re.sub(r"的$", "", value).strip()
    if len(value) > 48:
        value = value[-48:]

    candidates = [value]
    # In “含催化剂的AP燃速…”, AP is the owner; the preceding phrase is a
    # modifier/condition and must not become part of the entity name.
    if "的" in value:
        candidates.insert(0, value.rsplit("的", 1)[-1])
    candidates.append(re.split(r"(?:与|和|及).{0,20}?相比", value)[-1])
    if _clean_owner(value) in _GENERIC_MATERIAL_OWNERS and carry_owner and _owner_like(carry_owner):
        return carry_owner, "clause_carry_owner", 0.91
    for candidate in candidates:
        candidate = _clean_owner(candidate)
        if _owner_like(candidate):
            if carry_owner and carry_owner not in _GENERIC_MATERIAL_OWNERS and re.fullmatch(r"(?:共聚物|聚合物|预聚物|粘合剂|黏合剂|复合火药|推进剂|体系)(?:粘合剂|黏合剂)?", candidate):
                return carry_owner, "clause_carry_owner", 0.93
            return candidate, "explicit_clause_owner", 0.95

    embedded = _last_specific_material_mention(value)
    if embedded:
        return embedded, "embedded_specific_owner", 0.94

    matches = re.findall(
        r"[A-Za-z][A-Za-z0-9+._/\-()]{1,40}|[\u4e00-\u9fffA-Za-z0-9+._/\-()]{2,40}"
        r"(?:火药|推进剂|炸药|药剂|材料|物质|化合物|氧化物|聚合物|预聚物|共聚物|均聚物|粉|橡胶|树脂|体系|系统|装置)",
        value,
    )
    for candidate in reversed(matches):
        if _owner_like(candidate):
            return _clean_owner(candidate), "explicit_clause_owner", 0.93
    return (carry_owner, "clause_carry_owner", 0.86) if carry_owner else ("", "", 0.0)


def _seed_owner(segment: str) -> str:
    value = segment.strip(" ，,。；;：:")
    formulation = _FORMULATION_EQ_RE.search(value)
    if formulation:
        base = formulation.group("name").upper()
        nearby = value[formulation.end(): formulation.end() + 80]
        if re.search(r"(?:共聚物|聚合物|粘合剂|黏合剂)", nearby):
            candidate = base + "共聚物"
        elif re.search(r"(?:复合火药|推进剂|炸药)", nearby):
            candidate = base + ("推进剂" if "推进剂" in nearby else "复合火药")
        else:
            candidate = base + "体系"
        if _owner_like(candidate):
            return candidate
    head = re.split(r"[，,：:]", value, maxsplit=1)[0]
    head = re.sub(r"^(?:例如|如|其中|目前)", "", head).strip()
    # Specific material owner in ``X的燃速催化剂/性能/组成`` seeds the
    # following local clauses.  The topic tail is not part of the entity.
    topic_owner = re.match(
        r"(?P<owner>[A-Za-z0-9+._/\-()\u4e00-\u9fff]{2,45}?(?:火药|推进剂|炸药|药剂|材料|"
        r"化合物|氧化物|聚合物|预聚物|共聚物|均聚物|体系))的"
        r"(?:燃速催化剂|催化剂|性能|性质|组成|配方|制备|合成|应用|用途|研究)",
        head,
        re.I,
    )
    if topic_owner and _owner_like(topic_owner.group("owner")):
        return _clean_owner(topic_owner.group("owner"))
    if _owner_like(head):
        return _clean_owner(head)
    match = re.match(
        r"(?P<owner>[A-Za-z][A-Za-z0-9+._/\-()]{1,40}|[\u4e00-\u9fffA-Za-z0-9+._/\-()]{2,40}"
        r"(?:火药|推进剂|炸药|药剂|材料|物质|化合物|氧化物|聚合物|预聚物|共聚物|均聚物|粉|橡胶|树脂|体系|系统|装置))",
        value,
    )
    if match and _owner_like(match.group("owner")):
        return _clean_owner(match.group("owner"))
    embedded = _last_specific_material_mention(value)
    return embedded if embedded else ""


def _condition_atom(name: str, value_text: str, clause: str) -> ConditionAtom:
    parsed = parse_value(value_text)
    return ConditionAtom(
        name=name,
        normalized_name=name,
        condition_type="text_condition",
        value_text=value_text,
        unit=parsed.unit,
        value_num=parsed.value_num,
        lower_bound=parsed.lower_bound,
        upper_bound=parsed.upper_bound,
        comparator=parsed.comparator,
        scope="clause",
        priority=90,
        confidence=0.92,
        source_kind="clause_condition",
        source_text=clause,
    )


def _infer_condition_name(value_text: str) -> str:
    compact = re.sub(r"\s+", "", value_text)
    if re.search(r"(?:MPa|kPa|GPa|Pa|bar|mbar)$", compact, re.I):
        return "压力"
    if re.search(r"(?:℃|°C|K)$", compact, re.I):
        return "温度"
    if re.search(r"(?:ns|μs|us|ms|s|min|h|d)$", compact, re.I):
        return "时间"
    return "实验条件"


def _extract_prefix_conditions(prefix: str, clause: str) -> tuple[str, List[ConditionAtom]]:
    value = prefix.strip()
    atoms: List[ConditionAtom] = []

    # “HMX含量80%时GAP复合火药比冲…”
    entity_condition = re.match(
        rf"^\s*(?P<entity>[A-Za-z0-9+._/\-()\u4e00-\u9fff]{{1,30}}?)"
        r"(?P<name>含量|用量|浓度|粒度|粒径|压力|温度|时间)\s*(?:为|是)?\s*"
        rf"(?P<value>{_VALUE_TOKEN})\s*(?:时|下|条件下)\s*",
        value,
        re.I,
    )
    if entity_condition:
        name = f"{_clean_owner(entity_condition.group('entity'))}{entity_condition.group('name')}"
        atoms.append(_condition_atom(name, entity_condition.group("value").strip(), clause))
        value = value[entity_condition.end():]

    # “在4.9kPa压力复合火药燃速…”
    leading = re.match(
        rf"^\s*(?:在|于|当)?\s*(?P<value>{_VALUE_TOKEN})\s*"
        r"(?P<name>压力|温度|时间|延迟期|含量|用量|浓度|湿度|粒度|粒径|密度)"
        r"\s*(?:下|时|条件下)?\s*",
        value,
        re.I,
    )
    if leading:
        atoms.append(_condition_atom(leading.group("name"), leading.group("value").strip(), clause))
        value = value[leading.end():]

    # “在17～20MPa平台区压力指数…” — infer the condition from the unit.
    inferred = re.match(
        rf"^\s*(?:在|于)\s*(?P<value>{_VALUE_TOKEN})\s*(?:的)?(?:平台区|范围内|区间内)?\s*",
        value,
        re.I,
    )
    if inferred:
        condition_value = inferred.group("value").strip()
        atoms.append(_condition_atom(_infer_condition_name(condition_value), condition_value, clause))
        value = value[inferred.end():]

    return value.strip(), atoms


def _deduplicate_atoms(atoms: Iterable[ConditionAtom]) -> List[ConditionAtom]:
    unique = {}
    for atom in atoms:
        key = (atom.normalized_name or atom.name, re.sub(r"\s+", "", atom.value_text or ""), atom.unit or "")
        previous = unique.get(key)
        if previous is None or atom.confidence > previous.confidence:
            unique[key] = atom
    return list(unique.values())


def _extract_standalone_conditions(segment: str) -> List[ConditionAtom]:
    """Extract conditions that occur in a preceding soft clause.

    These patterns require an explicit condition marker (时/下/加入/以...为),
    so result measurements are not reclassified merely because they contain a
    number.  The returned atoms are carried only within the current sentence.
    """
    atoms: List[ConditionAtom] = []
    text = segment.strip()

    for match in _FORMULATION_RATIO_CONDITION_RE.finditer(text):
        ratio_name = match.group("name").upper() + "摩尔比"
        ratio_value = f"{match.group('left')}/{match.group('right')}"
        atoms.append(_condition_atom(ratio_name, ratio_value, segment))

    for match in re.finditer(
        r"以(?P<value>[^，,。；;]{1,35}?)为(?P<name>催化剂|添加剂|交联剂|固化剂|增塑剂|稳定剂|引发剂|溶剂)(?:时|条件下)?",
        text,
        re.I,
    ):
        atoms.append(_condition_atom(match.group("name"), match.group("value").strip(), segment))

    for match in re.finditer(
        rf"加入(?:了)?(?P<entity>[A-Za-z0-9+._/\-()\u4e00-\u9fff]{{1,35}}?)\s*"
        rf"(?P<value>{_VALUE_TOKEN})(?=\s*(?:，|,|时|后|条件下|$))",
        text,
        re.I,
    ):
        entity = _clean_owner(match.group("entity"))
        if entity and not re.search(r"(?:燃速|爆速|压力指数|温度|压力)$", entity):
            atoms.append(_condition_atom(f"{entity}用量", match.group("value").strip(), segment))

    for match in re.finditer(
        rf"(?:(?P<entity>[A-Za-z0-9+._/\-()\u4e00-\u9fff]{{1,28}}?))?"
        r"(?P<name>用量|含量|浓度|粒度|粒径|压力|温度|时间)\s*(?:为|是)?\s*"
        rf"(?P<value>{_VALUE_TOKEN})\s*(?:时|下|条件下)",
        text,
        re.I,
    ):
        entity = _clean_owner(match.group("entity") or "")
        entity = re.sub(r"^(?:如|例如|一般|通常|其中)$", "", entity).strip()
        name = f"{entity}{match.group('name')}" if entity else match.group("name")
        atoms.append(_condition_atom(name, match.group("value").strip(), segment))

    for match in re.finditer(
        rf"(?:在|于)\s*(?P<value>{_VALUE_TOKEN})\s*(?:的)?"
        r"(?P<name>压力|温度|时间|延迟期|含量|用量|浓度|湿度|粒度|粒径)?"
        r"(?:范围内|区间内|平台区|之间|下|时|条件下)",
        text,
        re.I,
    ):
        value_text = match.group("value").strip()
        name = match.group("name") or _infer_condition_name(value_text)
        atoms.append(_condition_atom(name, value_text, segment))

    return _deduplicate_atoms(atoms)


def _sentences(text: str) -> Iterable[str]:
    for sentence in re.split(r"[。！？!?；;\n]", text or ""):
        sentence = sentence.strip(" ，,。；;：:")
        if sentence:
            yield sentence


def _soft_segments(sentence: str) -> Iterable[str]:
    # Decimal commas are normalized before this stage, so commas are safe
    # discourse boundaries rather than numeric punctuation.
    for segment in re.split(r"(?<=，)|(?<=,)|\s+(?=而|但是|同时|其中)", sentence):
        segment = segment.strip(" ，,。；;：:")
        if segment:
            yield segment


def _append_fact(
    facts: List[BoundNumericFact],
    seen: set,
    *,
    owner: str,
    property_name: str,
    value_text: str,
    clause: str,
    owner_source: str,
    confidence: float,
    conditions: List[ConditionAtom] | None = None,
) -> None:
    key = (owner, property_name, value_text, clause, tuple((c.normalized_name, c.value_text) for c in (conditions or [])))
    if not owner or key in seen:
        return
    seen.add(key)
    facts.append(BoundNumericFact(owner, property_name, value_text, clause, owner_source, confidence, conditions or []))


def bind_numeric_facts(text: str, fallback_owner: str = "") -> List[BoundNumericFact]:
    normalized = normalize_scientific_text(text)
    facts: List[BoundNumericFact] = []
    seen: set = set()

    carry_owner = ""
    carry_experiment_owner = ""
    carry_experiment_property = ""
    history = ""
    for sentence in _sentences(normalized):
        pending_conditions: List[ConditionAtom] = []
        context_match = _EXPERIMENT_CONTEXT_RE.search(sentence)
        if context_match:
            candidate_owner = _clean_owner(context_match.group("owner"))
            if _owner_like(candidate_owner):
                carry_experiment_owner = candidate_owner
                carry_owner = candidate_owner
            carry_experiment_property = context_match.group("property").strip()
        sentence_pairs = list(_CONDITIONAL_RESULT_PAIR_RE.finditer(sentence))
        if sentence_pairs and carry_experiment_property:
            for pair in sentence_pairs:
                explicit_owner = _clean_owner(pair.group("owner") or "")
                owner = explicit_owner if _owner_like(explicit_owner) else (carry_experiment_owner or carry_owner)
                if not owner:
                    continue
                result_property = (pair.group("result_property") or carry_experiment_property).strip(" ：:")
                result_property = re.sub(r"(?:实验|试验|测试)$", "", result_property)
                _append_fact(
                    facts, seen, owner=owner, property_name=result_property,
                    value_text=pair.group("result").strip(), clause=sentence,
                    owner_source="conditional_experiment_result_phase100", confidence=0.96,
                    conditions=[_condition_atom(pair.group("condition_property"), pair.group("condition_value").strip(), sentence)],
                )
            history = (history + "。" + sentence)[-800:]
            continue
        for segment in _soft_segments(sentence):
            pending_conditions = _deduplicate_atoms(pending_conditions + _extract_standalone_conditions(segment))
            seeded = _seed_owner(segment)
            formulation_match = _FORMULATION_EQ_RE.search(segment)
            if formulation_match:
                base = formulation_match.group("name").upper()
                discourse_window = (history[-320:] + " " + segment)
                if re.search(r"(?:共聚物|共聚粘合剂|共聚黏合剂)", discourse_window):
                    seeded = base + "共聚物"
                elif re.search(r"(?:粘合剂|黏合剂)", discourse_window):
                    seeded = base + "粘合剂"
            if seeded:
                # A generic mention such as ``复合火药燃速`` must not erase a
                # more specific owner seeded earlier in the same paragraph.
                # A molecular formula is an identity value, not a discourse
                # owner, when a named material is already active.
                formula_seed = bool(_FORMULA_RE.fullmatch(_clean_owner(seeded)))
                preserve_named_owner = (
                    formula_seed
                    and carry_owner
                    and not _FORMULA_RE.fullmatch(_clean_owner(carry_owner))
                    and carry_owner not in _GENERIC_MATERIAL_OWNERS
                )
                if not preserve_named_owner and not (
                    seeded in _GENERIC_MATERIAL_OWNERS
                    and carry_owner
                    and carry_owner not in _GENERIC_MATERIAL_OWNERS
                ):
                    carry_owner = seeded

            # Generic experiment result binding: ``X的Y实验：当条件A时，为B``.
            paired_results = list(_CONDITIONAL_RESULT_PAIR_RE.finditer(segment))
            if paired_results and carry_experiment_property:
                for pair in paired_results:
                    explicit_owner = _clean_owner(pair.group("owner") or "")
                    owner = explicit_owner if _owner_like(explicit_owner) else (carry_experiment_owner or carry_owner)
                    if not owner:
                        continue
                    result_property = (pair.group("result_property") or carry_experiment_property).strip(" ：:")
                    if result_property.endswith(("实验", "试验", "测试")):
                        result_property = re.sub(r"(?:实验|试验|测试)$", "", result_property)
                    conditions = _deduplicate_atoms(list(pending_conditions) + [
                        _condition_atom(pair.group("condition_property"), pair.group("condition_value").strip(), segment)
                    ])
                    _append_fact(
                        facts, seen, owner=owner, property_name=result_property,
                        value_text=pair.group("result").strip(), clause=segment,
                        owner_source="conditional_experiment_result_phase100", confidence=0.96,
                        conditions=conditions,
                    )
                # The property values before ``时`` are conditions, not standalone
                # material properties for this experimental sentence.
                history = (history + "。" + sentence)[-800:]
                continue

            # Polymorph-specific values remain properties of the current material
            # with the crystal form represented as a condition.
            polymorphs = list(_POLYMORPH_PROPERTY_RE.finditer(segment))
            if polymorphs:
                owner = carry_owner if carry_owner and carry_owner not in _GENERIC_MATERIAL_OWNERS else _last_specific_material_mention(history + " " + segment)
                if (
                    not owner
                    and fallback_owner
                    and _owner_like(fallback_owner)
                    and _clean_owner(fallback_owner) not in _GENERIC_MATERIAL_OWNERS
                ):
                    owner = _clean_owner(fallback_owner)
                for item in polymorphs:
                    if not owner:
                        continue
                    _append_fact(
                        facts, seen, owner=owner, property_name=item.group("property"),
                        value_text=item.group("value").strip(), clause=segment,
                        owner_source="polymorph_local_owner_phase100", confidence=0.93,
                        conditions=[_condition_atom("晶型", item.group("form") + "晶型", segment)],
                    )
                history = (history + "。" + sentence)[-800:]
                continue

            # Dedicated delayed-explosion-point binding. Only temperature-valued
            # pairs are released; OCR-corrupted percent/per-mille values remain
            # visible through the labeled/candidate layer instead of becoming
            # false temperatures.
            if "爆发点" in segment:
                owner_prefix = segment[:segment.find("爆发点")]
                owner_prefix, prefix_conditions = _extract_prefix_conditions(owner_prefix, segment)
                owner, owner_source, confidence = _extract_owner_from_prefix(owner_prefix, carry_owner)
                if not owner and fallback_owner and _owner_like(fallback_owner):
                    owner, owner_source, confidence = _clean_owner(fallback_owner), "safe_heading_fallback", 0.80
                if owner:
                    for delay in _DELAY_EXPLOSION_RE.finditer(segment):
                        conditions = _deduplicate_atoms(list(pending_conditions) + list(prefix_conditions))
                        conditions.append(_condition_atom("延迟时间", delay.group("delay") + "s", segment))
                        _append_fact(
                            facts, seen, owner=owner, property_name="爆发点",
                            value_text=delay.group("result").strip(), clause=segment,
                            owner_source=owner_source, confidence=confidence, conditions=conditions,
                        )
                    if _DELAY_EXPLOSION_RE.search(segment):
                        carry_owner = owner
                        continue

            # Postposed owner: “相对分子质量为6000的PCP”.
            postposed_spans = []
            for match in _POSTPOSED_OWNER_RE.finditer(segment):
                postposed_spans.append(match.span())
                owner = _role_adjusted_owner(match.group("owner"), match.group("property"), segment)
                derived_match = _DERIVED_FORMULATION_OWNER_RE.search(segment[:match.start()])
                if derived_match and match.group("property") in {"固体含量", "氧化剂含量", "粘合剂含量", "黏合剂含量"}:
                    base_material = _best_derived_formulation_base(segment[:match.start()]) or _clean_owner(derived_match.group("material"))
                    if base_material and (
                        _owner_like(base_material) or re.search(r"(?:^|[^可])聚[A-Za-z0-9\u4e00-\u9fff]+$", base_material)
                    ):
                        owner = re.sub(r"(?:复合火药|推进剂|炸药)$", "", base_material).strip() + "复合火药"
                elif (
                    owner in _GENERIC_MATERIAL_OWNERS
                    and carry_owner
                    and carry_owner not in _GENERIC_MATERIAL_OWNERS
                    and match.group("property") in {"固体含量", "氧化剂含量", "粘合剂含量", "黏合剂含量"}
                ):
                    owner = carry_owner + "复合火药"
                elif owner in _GENERIC_MATERIAL_OWNERS and match.group("property") in {"固体含量", "氧化剂含量", "粘合剂含量", "黏合剂含量"}:
                    prop_pos = sentence.find(match.group("property"))
                    context_material = _last_specific_material_mention(sentence[:prop_pos] if prop_pos >= 0 else sentence)
                    if context_material and context_material not in _GENERIC_MATERIAL_OWNERS:
                        owner = context_material + "复合火药"
                # A postposed generic formulation may inherit a state-qualified
                # binder surface (for example ``未固化的羟基聚丁二烯复合火药``).
                # Prefer the most specific named binder in the short discourse
                # history and strip transient physical-state modifiers.
                if match.group("property") in {"固体含量", "氧化剂含量", "粘合剂含量", "黏合剂含量"}:
                    base_material = _best_derived_formulation_base(history[-360:] + " " + sentence)
                    if base_material:
                        formulation_kind = "推进剂" if "推进剂" in owner else ("炸药" if "炸药" in owner and "复合火药" not in owner else "复合火药")
                        owner = re.sub(r"(?:复合火药|推进剂|炸药)$", "", base_material).strip() + formulation_kind
                if not _owner_like(owner):
                    continue
                value_text = match.group("value").strip()
                _append_fact(
                    facts, seen, owner=owner, property_name=match.group("property"), value_text=value_text,
                    clause=segment, owner_source="postposed_property_owner", confidence=0.95,
                    conditions=list(pending_conditions),
                )
                pending_conditions = _deduplicate_atoms(list(pending_conditions) + [
                    _condition_atom(match.group("property"), value_text, segment)
                ])
                carry_owner = owner

            property_matches = list(_PROPERTY_RE.finditer(segment))
            for index, prop_match in enumerate(property_matches):
                if any(span_start <= prop_match.start() < span_end for span_start, span_end in postposed_spans):
                    continue
                before_property = segment[:prop_match.start()]
                if before_property.rfind("(") > before_property.rfind(")") or before_property.rfind("（") > before_property.rfind("）"):
                    continue
                property_name = prop_match.group("property")
                prefix = segment[:prop_match.start()]
                # Preserve a semantically meaningful content modifier instead
                # of collapsing every metric to the generic ``含量``.
                if property_name == "含量":
                    modifier_match = re.search(
                        r"(?P<modifier>氮|氧|氯|铝|硼|水分|灰分|挥发分|固体|有效成分|金属|氧化剂|粘合剂|黏合剂)\s*$",
                        prefix,
                    )
                    if modifier_match:
                        property_name = modifier_match.group("modifier") + "含量"
                        prefix = prefix[:modifier_match.start()].rstrip()
                prefix, conditions = _extract_prefix_conditions(prefix, segment)
                conditions = _deduplicate_atoms(list(pending_conditions) + list(conditions))
                # A preceding explicit property/value pair can act as a condition
                # for the later result in the same clause (e.g. molecular mass ->
                # melting point).
                for prior in _PROPERTY_VALUE_PAIR_RE.finditer(prefix):
                    prior_property = prior.group("property")
                    if prior_property != property_name:
                        conditions.append(_condition_atom(prior_property, prior.group("value").strip(), segment))
                conditions = _deduplicate_atoms(conditions)

                delay_match = _PREPROPERTY_DELAY_RE.search(prefix)
                owner_prefix = prefix
                if delay_match and property_name == "爆发点":
                    owner_prefix = prefix[:delay_match.start()]
                    conditions.append(_condition_atom("延迟时间", delay_match.group("value") + "s", segment))

                owner, owner_source, confidence = _extract_owner_from_prefix(owner_prefix, carry_owner)
                derived_match = _DERIVED_FORMULATION_OWNER_RE.search(owner_prefix)
                if derived_match and property_name in {"固体含量", "氧化剂含量", "粘合剂含量", "黏合剂含量"}:
                    base_material = _best_derived_formulation_base(owner_prefix) or _clean_owner(derived_match.group("material"))
                    if base_material and (
                        _owner_like(base_material) or re.search(r"(?:^|[^可])聚[A-Za-z0-9\u4e00-\u9fff]+$", base_material)
                    ):
                        owner = re.sub(r"(?:复合火药|推进剂|炸药)$", "", base_material).strip() + "复合火药"
                        owner_source = "derived_formulation_owner_phase100"
                        confidence = 0.93
                        conditions.append(_condition_atom("粘合剂", base_material, segment))
                owner = _role_adjusted_owner(owner, property_name, segment)
                after = segment[prop_match.end():]
                # “X has low viscosity and can therefore make a formulation
                # with solid content Y” assigns Y to the derived formulation,
                # not to the uncured binder itself.  Use the short discourse
                # history to recover the most specific named binder.
                if (
                    property_name in {"固体含量", "氧化剂含量", "粘合剂含量", "黏合剂含量"}
                    and re.search(r"(?:制成|制得|制备成).{0,30}?(?:复合火药|推进剂|炸药)", history[-360:] + " " + segment)
                ):
                    base_material = _best_derived_formulation_base(history[-360:] + " " + segment)
                    if not base_material and carry_owner and carry_owner not in _GENERIC_MATERIAL_OWNERS:
                        base_material = re.sub(r"^(?:未固化的|已固化的|固化后的|液态的)", "", carry_owner).strip()
                    if base_material:
                        formulation_kind = "推进剂" if "推进剂" in segment else ("炸药" if "炸药" in segment else "复合火药")
                        owner = re.sub(r"(?:复合火药|推进剂|炸药)$", "", base_material).strip() + formulation_kind
                        owner_source = "derived_formulation_owner_phase100"
                        confidence = max(confidence, 0.93)
                after = segment[prop_match.end():]
                parenthetical_result = re.match(
                    rf"^\s*[（(](?P<condition>[^）)]{{1,80}})[）)]\s*(?:为|是|约为|达到|可达)\s*(?P<result>{_VALUE_TOKEN})",
                    after,
                    re.I,
                )
                if parenthetical_result:
                    condition_text = parenthetical_result.group("condition").strip()
                    condition_match_inner = re.search(
                        rf"(?P<value>{_VALUE_TOKEN})\s*(?P<name>真密度|相对密度|密度|压力|温度|含量|用量|浓度|粒度|粒径)",
                        condition_text,
                        re.I,
                    )
                    parent_conditions = list(conditions)
                    if condition_match_inner:
                        parent_conditions.append(
                            _condition_atom(condition_match_inner.group("name"), condition_match_inner.group("value").strip(), segment)
                        )
                    if not owner and fallback_owner and _owner_like(fallback_owner):
                        owner, owner_source, confidence = _clean_owner(fallback_owner), "safe_heading_fallback", 0.80
                    if owner:
                        _append_fact(
                            facts, seen, owner=owner, property_name=property_name,
                            value_text=parenthetical_result.group("result").strip(), clause=segment,
                            owner_source=owner_source, confidence=confidence, conditions=parent_conditions,
                        )
                        carry_owner = owner
                    continue
                # A property may not steal a value belonging to a later property.
                next_relative_start = (
                    property_matches[index + 1].start() - prop_match.end()
                    if index + 1 < len(property_matches) else len(after)
                )
                local_after = after[:max(0, next_relative_start)]

                condition_match = _CONDITION_BEFORE_RESULT_RE.match(local_after)
                if condition_match:
                    value_text = condition_match.group("result").strip()
                    conditions.append(
                        _condition_atom(
                            condition_match.group("condition_name"),
                            condition_match.group("condition_value").strip(),
                            segment,
                        )
                    )
                else:
                    copula = re.search(
                        r"(?:为|是|约为|达到|可达|提高到|降低到|范围为|通常为|一般为|不到)\s*",
                        local_after,
                    )
                    if copula:
                        search_text = local_after[copula.end():]
                        comparator_prefix = "<" if copula.group(0).strip() == "不到" else ""
                    else:
                        search_text = local_after.lstrip()
                        comparator_prefix = ""
                        if not re.match(rf"^(?:≥|≤|>|<|约|不小于|不大于)?\s*(?:{_NUMBER})", search_text):
                            continue
                    value_match = _VALUE_RE.match(search_text)
                    if not value_match:
                        continue
                    value_text = comparator_prefix + value_match.group("value").strip()

                if not owner:
                    competing = _seed_owner(segment)
                    if not competing and fallback_owner and _owner_like(fallback_owner):
                        owner = _clean_owner(fallback_owner)
                        owner_source = "safe_heading_fallback"
                        confidence = 0.80
                    else:
                        continue

                _append_fact(
                    facts, seen, owner=owner, property_name=property_name, value_text=value_text,
                    clause=segment, owner_source=owner_source, confidence=confidence, conditions=conditions,
                )
                carry_owner = owner
        history = (history + "。" + sentence)[-800:]

    return facts


__all__ = ["BoundNumericFact", "bind_numeric_facts", "normalize_scientific_text"]
