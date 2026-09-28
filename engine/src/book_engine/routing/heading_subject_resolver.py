from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, List, Sequence

from book_engine.routing.subject_name_normalizer import normalize_subject_name
from book_engine.routing.subject_type_inferer import infer_subject_type

_CHAPTER_PREFIX_RE = re.compile(
    r"^\s*第\s*[一二三四五六七八九十百零〇0-9]+\s*[章节篇部]\s*"
)
_LIST_MARKER_RE = re.compile(
    r"^\s*(?:[（(]?[一二三四五六七八九十0-9]+[）)）、.．]|[（(]?[A-Za-z][）)）、.．])\s*"
)
_DOTTED_SECTION_SPACE_RE = re.compile(r"^\s*\d+(?:\s*[.．]\s*\d+){1,8}\s+")
_SIMPLE_SECTION_SPACE_RE = re.compile(r"^\s*\d+\s+")
_TRAILING_CITATION_RE = re.compile(
    r"(?:(?:\s*[\[【]\s*\d+(?:\s*[-,，、]\s*\d+)*\s*[\]】])|"
    r"(?:\s+[（(]\s*\d+(?:\s*[-,，、]\s*\d+)*\s*[）)]))+\s*$"
)
_LATEX_MARKUP_RE = re.compile(r"\\(?:mathrm|text|operatorname|bullet|cdot|times|prime)\b|[$^{}]")

# Semantic descriptors are deliberately domain-generic.  They describe what a
# section discusses, not which entity owns the facts in that section.
_TOPIC_TERMS = (
    "概述", "导言", "绪论", "引论", "前言", "定义", "概念", "分类", "特点", "要求", "一般特征", "基本条件",
    "原理", "机理", "理论", "理论基础", "技术", "概论", "手册", "研究进展", "发展史", "计算标准",
    "性能", "性质", "物理性质", "化学性质", "理化性质", "力学性能", "燃烧性能", "爆轰性能",
    "热性能", "热分解性能", "工艺性能", "安全性能", "发火特性", "结构", "构造", "组成", "配方",
    "用途", "应用", "作用", "作用过程", "位置", "方法", "制备", "合成", "制备方法", "合成方法", "测试方法", "检验方法",
    "分析方法", "工艺", "工艺流程", "制备工艺", "生产工艺", "加工工艺", "装配", "装配方法", "试验",
    "实验", "测试", "测定", "检验", "表征", "分析", "结果", "讨论", "比较", "影响", "影响因素", "因素",
    "关系", "变化", "变化规律", "传播", "反应速度", "释能方式", "安全", "毒性", "防护", "储存", "运输",
    "储存运输", "原材料", "材料", "设备", "仪器", "参数", "条件", "模型", "算法", "数据库", "知识库",
    "理论结构单元", "结构单元", "构造举例", "结构举例", "举例", "示例", "方面",
    "本章小结", "小结", "结论", "参考文献", "附录", "目录",
)
_TOPIC_SET = {re.sub(r"\s+", "", item) for item in _TOPIC_TERMS}
_TOPIC_ALT = "|".join(sorted((re.escape(item) for item in _TOPIC_TERMS), key=len, reverse=True))
_TOPIC_SUFFIX_RE = re.compile(
    rf"(?:的)?(?:{_TOPIC_ALT})(?:及其(?:{_TOPIC_ALT}))?$",
    re.IGNORECASE,
)
_TOPIC_ONLY_RE = re.compile(
    rf"^(?:(?:第?[一二三四五六七八九十百零〇0-9]+[章节篇部])?\s*)?(?:{_TOPIC_ALT})$",
    re.IGNORECASE,
)
_BOOK_TOPIC_RE = re.compile(
    r"(?:理论基础|设计原理|技术基础|制造技术|基础理论|基本原理|概论|手册|教程|导论|技术)$"
)
_METHOD_TITLE_RE = re.compile(
    r"^(?:[A-Za-z][A-Za-z0-9._/+\-]{0,18}|[A-Za-z][A-Za-z0-9._/+\-]{0,18}[-–—][A-Za-z0-9._/+\-]{1,18})"
    r"(?:方法|模型|公式|定律|算法|准则|法)$",
    re.IGNORECASE,
)
_SENTENCE_LIKE_RE = re.compile(
    r"[。；;：:!?！？]|(?:可以|能够|采用|通过|导致|提高|降低|增加|减少|研究表明|结果表明|"
    r"从.+看|用于|取决于|主要是|分别为|由.+构成|必须|通常|一般|可使|使得)"
)
_TOPIC_CONNECTOR_RE = re.compile(
    r"(?:与|及|和|对|在|中|基于|关于|影响|效应|因素|关系|分类|比较|计算|确定|选择|研究|分析|讨论|"
    r"变化|传播|反应|释能|构造|结构|检验|作用|举例|示例)"
)
_EFFECT_TOPIC_RE = re.compile(r"(?:效应|因素).{0,12}对.{1,30}(?:聚合|反应|性能|性质|影响)$")
_BASED_TOPIC_RE = re.compile(r"^(?:基于|根据|按照).+(?:定律|模型|方法|标准|计算|分析|研究)$", re.I)
_TOPIC_PREFIX_CLASS_RE = re.compile(
    r"^.+(?:分类|性能|性质|影响|关系|研究|分析|计算|比较|组成|结构|生成焓|反应|释能|用途|作用)的"
    r"(?:单质|混合|复合|典型|常用|不同)?(?:材料|炸药|推进剂|火药|火工品|药剂|聚合物|弹性体|雷管|火帽|底火)$",
    re.IGNORECASE,
)
_COORDINATE_RE = re.compile(r"^(?P<left>.+?)(?:和|与|及|、)(?P<right>.+)$")
_AGGREGATE_ENTITY_RE = re.compile(r"(?:混合物|复合物|共聚物|配方|体系|组合物)$")


_EQUIPMENT_ENTITY_TERMINAL_RE = re.compile(r"(?:机|器|室|炉|泵|罐|釜|磨|筒|容器|箱|槽|塔|床|喷管)$")

_DEICTIC_ONLY_ENTITY_RE = re.compile(
    r"^(?:它|其|该|此|上述|前述|本|这些|那些)(?:系统|装置|材料|物质|产品|产物|聚合物|"
    r"推进剂|火药|炸药|组件|设备|结构|方法|体系|样品|试样)?$"
)

_GENERIC_ENTITY_TERMS = {
    "材料", "原材料", "含能材料", "炸药", "推进剂", "固体推进剂", "火药", "火工品", "药剂",
    "聚合物", "预聚物", "弹性体", "样品", "试样", "配方", "体系", "组分", "产品", "产物",
}
_BROAD_CLASS_TERMS = {
    "含能聚合物", "含能材料", "高能材料", "火工品", "炸药", "推进剂", "固体推进剂", "火药",
    "氧化剂", "粘结剂", "黏结剂", "增塑剂", "催化剂", "安定剂", "燃速调节剂",
}
_MATERIAL_CODE_RE = re.compile(
    r"^(?:[A-Za-z][A-Za-z0-9+._/()（）-]{1,40}|(?:[A-Z][a-z]?\d*){2,}|[A-Z][A-Z0-9]*(?:[-/][A-Z0-9]+)+)$"
)
_ENTITY_TERMINAL_RE = re.compile(
    r"(?:聚合物|预聚物|共聚物|均聚物|嵌段物|弹性体|树脂|橡胶|纤维素|推进剂|发射药|炸药|火药|"
    r"药剂|延期药|点火药|击发药|针刺药|传爆药|烟火药|导电药|配方|体系|混合物|复合物|火工品|火帽|底火|雷管|点火具|点火器|起爆器|导爆索|导火索|"
    r"传爆元件|延期元件|装置|组件|系统|分系统|机构|器件|元件|化合物|单体|低聚物|酸|盐|酯|醚|胺|酚|醇|酮|醛|酐|腈|肼|脲|"
    r"烷|烯|炔|唑|烃|苯|吡啶|咪唑|氧化物|氢化物|卤化物|金属|合金|铋|铅|铝|镁|铜|铁|镍|钴|银|金|锌|锡|钨|钼|钛|锆|硼|硅)(?:[A-Za-z0-9+._/()（）-]{0,18})?$",
    re.IGNORECASE,
)
_ABSTRACT_SUFFIX_RE = re.compile(
    r"(?:学|论|法|性|度|率|量|值|参数|条件|因素|关系|规律|过程|标准|方式|特征|特性|性能|性质|结构|构造|"
    r"方法|模型|理论|原理|机理|结果|比较|分析|研究|讨论|影响|效应|分类|用途|作用|方面|举例|示例)$"
)

# Generic descriptive-heading parsers.  The result is an entity span; the
# remaining text is a topic.  These patterns are intentionally based on syntax,
# not on any particular book title or material list.
_INFLUENCE_FACTORS_RE = re.compile(
    r"^影响(?P<entity>.+?)(?:的)?(?:性能|性质|特性|反应|过程)?的因素$",
    re.IGNORECASE,
)
_TYPICAL_STRUCTURE_RE = re.compile(
    r"^(?:典型)?(?P<entity>.+?)(?:典型)?(?:结构|构造)(?:的)?(?:举例|示例)(?:及其.+)?$",
    re.IGNORECASE,
)
_MIDDLE_TOPIC_RE = re.compile(
    r"^(?P<entity>.+?)中\s*.+?(?:的)?(?:传播|反应|变化|分解|燃烧|爆轰|聚合|迁移|扩散)$",
    re.IGNORECASE,
)
_ENTITY_STATEMENT_RE = re.compile(
    r"^(?P<entity>.+?)(?:是|为|属于|可视为|可作为)(?:一种|一类)?(?P<description>.+)$",
    re.IGNORECASE,
)

# Property-owner syntax is intentionally generic.  It is used only to decide
# which entity owns a fact; reagent/comparison mentions are filtered below.
_PROPERTY_TERMS = (
    "密度", "熔点", "沸点", "闪点", "分解温度", "峰顶温度", "爆速", "爆压", "爆热", "燃速",
    "比冲", "感度", "氧平衡", "生成热", "生成焓", "分子量", "相对分子质量", "纯度", "含量",
    "粒径", "粒度", "黏度", "粘度", "溶解性", "官能度", "取代度", "模量", "应力", "应变",
    "强度", "伸长率", "热容", "热导率", "电导率", "产率", "收率", "得率", "组成", "组分",
    "配方", "用途", "应用", "性能", "性质", "结构", "制备方法", "合成方法", "工艺",
)
_PROPERTY_ALT = "|".join(sorted((re.escape(item) for item in _PROPERTY_TERMS), key=len, reverse=True))
_PROPERTY_TOPIC_SUFFIX_RE = re.compile(rf"(?:的)?(?:{_PROPERTY_ALT})(?:性能|特性|参数|规律|变化|影响)?$", re.IGNORECASE)
_OWNER_PATTERNS = (
    re.compile(rf"(?P<subject>[A-Za-z0-9+._/()（）\-\u4e00-\u9fff]{{1,80}}?)的(?:结构|构造|组成|外观)\s*(?:如图|如表|见图|见表|如下)"),
    re.compile(rf"(?P<subject>[A-Za-z0-9+._/()（）\-\u4e00-\u9fff]{{1,80}}?)的(?:{_PROPERTY_ALT})(?=为|是|约|可|达到|[，,。；;：:]|$)"),
    re.compile(rf"(?P<subject>[A-Za-z0-9+._/()（）\-\u4e00-\u9fff]{{1,80}}?)(?:的)?(?:{_PROPERTY_ALT})\s*(?:为|是|约为|达到|可达)"),
    re.compile(r"(?:^|[。；;，,])\s*(?P<subject>[A-Za-z0-9+._/()（）\-\u4e00-\u9fff]{1,80}?)\s*(?:具有|呈现|表现为|属于|是一种|为一种)"),
)
_COMPOSITION_OWNER_PATTERNS = (
    re.compile(
        r"(?:^|[。；;，,])\s*(?P<subject>[A-Za-z0-9+._/()（）\-\u4e00-\u9fff]{1,60}?)"
        r"\s*(?:一般|通常|主要|基本)?\s*由[^。；;]{1,220}?(?:组成|构成)"
    ),
    re.compile(
        r"(?:^|[。；;，,])\s*(?P<subject>[A-Za-z0-9+._/()（）\-\u4e00-\u9fff]{1,60}?)"
        r"\s*(?:包括|包含|含有|分为)"
    ),
)
_EXPLICIT_OWNER_SURFACE_RE = re.compile(
    r"(?:^|[。；;，,])\s*(?P<subject>[A-Za-z0-9+._/()（）\-\u4e00-\u9fff]{1,60}?)"
    r"\s*(?:(?:一般|通常|主要|基本)?\s*由|包括|包含|含有|分为)"
)
_LEADING_CONNECTOR_RE = re.compile(
    r"^(?:与|和|及|或|对|对于|关于|其中|而|但|同时|此外|相比之下|相反|研究表明|结果表明|"
    r"在不同条件下|在|于|将|把|采用|使用|利用|通过|以|由|向|加入|添加|用)"
)
_REFERENCE_PREFIX_RE = re.compile(r"(?:与|和|相对于|相较于|相比于?|对比|参照)\s*$")
_REAGENT_PREFIX_RE = re.compile(r"(?:以|采用|使用|利用|加入|添加|用|由|向)\s*$")
_REAGENT_SUFFIX_RE = re.compile(r"^\s*(?:为原料|作(?:为)?|作为|为引发剂|为催化剂|为溶剂|为添加剂|参与|混合|反应)")
_CONDITION_TAIL_RE = re.compile(
    r"(?:质量分数|摩尔分数|体积分数|含量|比例|配比|浓度|密度|温度|压力|时间|编号|批次|样品号|"
    r"为\s*[-+]?\d|=\s*[-+]?\d).*$",
    re.IGNORECASE,
)
_ENTITY_PREFIX_RE = re.compile(
    r"^(?P<entity>.+?(?:聚合物|预聚物|共聚物|均聚物|弹性体|树脂|橡胶|推进剂|发射药|炸药|火药|药剂|"
    r"体系|火帽|底火|雷管|点火具|点火器|起爆器|导爆索|导火索|化合物|酸|盐|酯|醚|胺|酚|醇|酮))"
)


@dataclass(frozen=True)
class HeadingSubjectCandidate:
    raw_title: str
    normalized_title: str
    entity: str
    kind: str
    confidence: float
    subject_type: str = ""
    broad_class: bool = False
    depth: int = 0
    reasons: List[str] = field(default_factory=list)
    entity_candidates: List[str] = field(default_factory=list)

    @property
    def is_entity(self) -> bool:
        return bool(self.entity) and self.kind in {"pure_entity", "entity_with_topic", "material_class"}


@dataclass(frozen=True)
class LocalSubjectEvidence:
    subject: str
    role: str
    confidence: float
    evidence_span: str
    reasons: List[str] = field(default_factory=list)

    @property
    def is_owner(self) -> bool:
        return self.role in {"property_owner", "identity_owner", "entity_statement", "process_subject", "composition_owner"}


def _normalize_heading_surface(title: str) -> str:
    value = normalize_subject_name(title or "", trim_descriptive_topic=False).canonical_name
    value = _TRAILING_CITATION_RE.sub("", value)
    value = _LATEX_MARKUP_RE.sub("", value)
    value = value.replace("\\", "")
    value = re.sub(r"\s*([/+·._-])\s*", r"\1", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _strip_concatenated_section_before_locant(value: str) -> str:
    """Split ``3.3.33,3-X`` into section ``3.3.3`` + locant ``3,3-X``.

    OCR/Markdown often removes the whitespace between a dotted section number
    and a chemical locant.  Enumerate every possible split and keep the longest
    valid dotted section prefix, which preserves the shortest plausible locant.
    """
    candidates: list[tuple[int, str]] = []
    for index in range(1, len(value)):
        left, right = value[:index], value[index:]
        if not re.fullmatch(r"\d+(?:[.．]\d+)+", left):
            continue
        if re.match(r"^\d{1,2}(?:,\d{1,2})+-", right):
            candidates.append((len(left), right))
    return max(candidates, default=(0, value), key=lambda item: item[0])[1]


def strip_heading_number(title: str) -> str:
    value = _normalize_heading_surface(title)
    if not value:
        return ""
    value = _CHAPTER_PREFIX_RE.sub("", value)
    locant_preserved = _strip_concatenated_section_before_locant(value)
    if locant_preserved != value:
        value = locant_preserved
    else:
        value = _LIST_MARKER_RE.sub("", value)
        value = _DOTTED_SECTION_SPACE_RE.sub("", value)
        # Dotted section numbers may be directly attached to a Chinese/Latin
        # title.  Do this only when the remainder does not start with comma or
        # hyphen, which are chemical-locant punctuation.
        match = re.match(r"^\s*(\d+(?:[.．]\d+){1,8})(?=[A-Za-z\u4e00-\u9fff])", value)
        if match:
            value = value[match.end():]
        value = _SIMPLE_SECTION_SPACE_RE.sub("", value)
        # OCR may drop the delimiter after a one- or two-digit list/section
        # number (``4传爆药``).  Chemical locants normally retain comma or
        # hyphen, so a digit directly followed by a CJK entity token is safe to
        # treat as a structural prefix in heading context.
        value = re.sub(r"^\d{1,2}(?=[\u4e00-\u9fff])", "", value)
    return value.strip(" ：:、.．")


def _registry_names(entries: Sequence[object]) -> List[str]:
    # Hot path: callers may pass an already-normalized tuple of names.  Avoid
    # repeatedly walking and normalizing the full registry for every block and
    # every heading level.
    if entries and all(isinstance(item, str) for item in entries):
        return list(entries)
    names: List[str] = []
    seen: set[str] = set()
    for entry in entries or ():
        if getattr(entry, "status", "confirmed") != "confirmed":
            continue
        for name in [getattr(entry, "canonical_name", "")] + list(getattr(entry, "aliases", []) or []):
            normalized = strip_heading_number(str(name or ""))
            if normalized and normalized not in seen:
                seen.add(normalized)
                names.append(normalized)
    return sorted(names, key=len, reverse=True)


def registry_subject_names(entries: Sequence[object]) -> tuple[str, ...]:
    return tuple(_registry_names(entries))


def _has_entity_terminal(value: str) -> bool:
    return bool(_ENTITY_TERMINAL_RE.search(re.sub(r"\s+", "", value or "")))


def _is_generic_relation_topic(value: str) -> bool:
    compact = re.sub(r"\s+", "", value or "")
    for generic in sorted(_GENERIC_ENTITY_TERMS | _BROAD_CLASS_TERMS, key=len, reverse=True):
        if compact.startswith(generic) and compact != generic:
            tail = compact[len(generic):]
            if _TOPIC_CONNECTOR_RE.search(tail) or _ABSTRACT_SUFFIX_RE.search(tail):
                return True
    return False


def is_topic_only_title(title: str) -> bool:
    value = strip_heading_number(title)
    compact = re.sub(r"\s+", "", value)
    if not compact:
        return True
    if compact in _TOPIC_SET or _TOPIC_ONLY_RE.fullmatch(compact):
        return True
    if _METHOD_TITLE_RE.fullmatch(compact):
        return True
    if compact in _GENERIC_ENTITY_TERMS:
        return True
    if _EFFECT_TOPIC_RE.search(compact) or _BASED_TOPIC_RE.search(compact) or _TOPIC_PREFIX_CLASS_RE.search(compact):
        return True
    if _is_generic_relation_topic(compact):
        return True
    if _BOOK_TOPIC_RE.search(compact) and not _MATERIAL_CODE_RE.fullmatch(compact) and not _has_entity_terminal(compact):
        return True
    if len(compact) > 16 and (_SENTENCE_LIKE_RE.search(compact) or (_TOPIC_CONNECTOR_RE.search(compact) and _ABSTRACT_SUFFIX_RE.search(compact))):
        return True
    return False


def is_entity_surface(value: str, registry_names: Sequence[str] = ()) -> bool:
    candidate = strip_heading_number(value or "")
    if not candidate or len(candidate) > 60:
        return False
    compact = re.sub(r"\s+", "", candidate)
    if not compact or compact in _GENERIC_ENTITY_TERMS or _DEICTIC_ONLY_ENTITY_RE.fullmatch(compact) or is_topic_only_title(compact):
        return False
    if _SENTENCE_LIKE_RE.search(compact):
        return False
    if re.fullmatch(r"[0-9一二三四五六七八九十百.．()（）]+", compact):
        return False
    if _MATERIAL_CODE_RE.fullmatch(compact):
        return True
    if _has_entity_terminal(compact) or _EQUIPMENT_ENTITY_TERMINAL_RE.search(compact):
        return True
    if (
        re.fullmatch(r"[A-Za-z0-9+._/\-一-鿿]{2,60}", compact)
        and re.search(r"[A-Za-z]{2,}", compact)
        and re.search(r"(?:基|共聚|均聚|嵌段|聚合物|预聚物|弹性体|推进剂|炸药|药剂|体系)", compact)
    ):
        return True
    # A confirmed ontology/registry name is useful corroboration, but never a
    # bypass around the semantic topic filters above.  This prevents an earlier
    # bad run from certifying a descriptive heading as a material.
    if any(compact.casefold() == re.sub(r"\s+", "", name).casefold() for name in registry_names):
        return not _ABSTRACT_SUFFIX_RE.search(compact)
    # Unknown names without ontology/registry support stay ambiguous.  Guessing
    # from “a short Chinese phrase” is unsafe because topic titles such as
    # “反应结晶” or “压力与燃速” have the same surface shape.
    return False


def _candidate_from_entity(
    raw_title: str,
    normalized: str,
    entity: str,
    kind: str,
    confidence: float,
    depth: int,
    reason: str,
) -> HeadingSubjectCandidate:
    subject_type, _, type_reasons = infer_subject_type(entity)
    broad = entity in _BROAD_CLASS_TERMS or subject_type == "材料类别"
    if broad:
        confidence = min(confidence, 0.82)
        if depth == 0:
            confidence = min(confidence, 0.72)
    return HeadingSubjectCandidate(
        raw_title, normalized, entity, "material_class" if kind == "pure_entity" and broad else kind,
        confidence, subject_type, broad, depth, [reason] + type_reasons,
    )


def _parse_descriptive_entity(normalized: str, names: Sequence[str]) -> tuple[str, str, float, str] | None:
    compact = re.sub(r"\s+", "", normalized)
    if compact in _TOPIC_SET or _TOPIC_ONLY_RE.fullmatch(compact):
        return None
    match = _INFLUENCE_FACTORS_RE.fullmatch(normalized)
    if match:
        entity = match.group("entity").strip(" 的：:、-—")
        if is_entity_surface(entity, names):
            return entity, "entity_with_topic", 0.92, "influence_factors_heading"

    match = _TYPICAL_STRUCTURE_RE.fullmatch(normalized)
    if match:
        entity = re.sub(r"^(?:典型|不同|各种)", "", match.group("entity")).strip(" 的：:、-—")
        if is_entity_surface(entity, names):
            return entity, "entity_with_topic", 0.91, "structure_example_heading"

    match = _MIDDLE_TOPIC_RE.fullmatch(normalized)
    if match:
        entity = match.group("entity").strip(" 的：:、-—")
        if is_entity_surface(entity, names):
            return entity, "entity_with_topic", 0.90, "entity_scope_middle_topic_heading"

    match = _ENTITY_STATEMENT_RE.fullmatch(normalized)
    if match:
        entity = match.group("entity").strip(" 的：:、-—")
        if is_entity_surface(entity, names):
            return entity, "entity_with_topic", 0.86, "entity_definition_heading"

    # Prefer an exact known entity span when the remaining title is a semantic
    # descriptor.  Registry evidence corroborates the span but does not certify
    # the entire heading.
    for name in names:
        if normalized.startswith(name):
            remainder = normalized[len(name):].strip(" 的：:、-—")
            if remainder and (_TOPIC_SUFFIX_RE.fullmatch(remainder) or is_topic_only_title(remainder)):
                return name, "entity_with_topic", 0.96, "confirmed_registry_entity_plus_topic"

    suffix_candidates: list[tuple[int, str]] = []
    for pattern, label in ((_PROPERTY_TOPIC_SUFFIX_RE, "property_topic"), (_TOPIC_SUFFIX_RE, "topic")):
        match = pattern.search(normalized)
        if match and match.start() > 0:
            suffix_candidates.append((match.start(), label))
    if suffix_candidates:
        suffix_start, suffix_kind = min(suffix_candidates, key=lambda item: item[0])
        entity = normalized[:suffix_start].strip(" 的：:、-—")
        if entity not in _GENERIC_ENTITY_TERMS and is_entity_surface(entity, names):
            return entity, "entity_with_topic", 0.92 if suffix_kind == "property_topic" else 0.90, f"generic_entity_plus_{suffix_kind}"
    return None


def classify_heading_subject(
    title: str,
    registry_entries: Sequence[object] = (),
    *,
    depth: int = 0,
) -> HeadingSubjectCandidate:
    normalized = strip_heading_number(title)
    names = _registry_names(registry_entries)
    if not normalized:
        return HeadingSubjectCandidate(title, normalized, "", "empty", 0.0, depth=depth, reasons=["empty_heading"])

    if _METHOD_TITLE_RE.fullmatch(re.sub(r"\s+", "", normalized)):
        return HeadingSubjectCandidate(title, normalized, "", "topic_only", 0.99, depth=depth, reasons=["method_or_model_heading"])

    coordinate = _COORDINATE_RE.fullmatch(normalized)
    if coordinate and not _AGGREGATE_ENTITY_RE.search(normalized):
        left = coordinate.group("left").strip(" 的：:、-—")
        right = coordinate.group("right").strip(" 的：:、-—")
        if is_entity_surface(left, names) and is_entity_surface(right, names):
            return HeadingSubjectCandidate(
                title, normalized, "", "multi_entity", 0.84, depth=depth,
                reasons=[f"coordinated_entity_heading:{left}|{right}", "requires_local_owner_or_closed_set_adjudication"],
                entity_candidates=[left, right],
            )

    parsed = _parse_descriptive_entity(normalized, names)
    if parsed:
        entity, kind, confidence, reason = parsed
        return _candidate_from_entity(title, normalized, entity, kind, confidence, depth, reason)

    if is_topic_only_title(normalized):
        return HeadingSubjectCandidate(title, normalized, "", "topic_only", 0.98, depth=depth, reasons=["topic_only_heading"])

    # Exact registry matches are considered only after semantic parsing and
    # topic rejection, so a polluted registry cannot make a chapter phrase a
    # legal entity.
    for name in names:
        if normalized.casefold() == name.casefold() and is_entity_surface(name, names):
            return _candidate_from_entity(title, normalized, name, "pure_entity", 0.97, depth, "exact_confirmed_registry_heading")

    if is_entity_surface(normalized, names):
        confidence = 0.90 if (_MATERIAL_CODE_RE.fullmatch(normalized) or _has_entity_terminal(normalized)) else 0.76
        return _candidate_from_entity(title, normalized, normalized, "pure_entity", confidence, depth, "entity_surface_heading")

    return HeadingSubjectCandidate(title, normalized, "", "ambiguous", 0.35, depth=depth, reasons=["heading_not_entity_or_topic"])


def heading_path_candidates(
    heading_path: Sequence[str],
    registry_entries: Sequence[object] = (),
) -> List[HeadingSubjectCandidate]:
    return [
        classify_heading_subject(title, registry_entries, depth=index)
        for index, title in enumerate(heading_path or ())
    ]


def nearest_heading_subject(
    heading_path: Sequence[str],
    registry_entries: Sequence[object] = (),
) -> HeadingSubjectCandidate | None:
    candidates = heading_path_candidates(heading_path, registry_entries)
    for candidate in reversed(candidates):
        if candidate.is_entity:
            return candidate
    return None


def build_subject_lexicon(
    heading_paths: Iterable[Sequence[str]],
    registry_entries: Sequence[object] = (),
) -> List[str]:
    names = _registry_names(registry_entries)
    for path in heading_paths:
        for candidate in heading_path_candidates(path, registry_entries):
            if candidate.is_entity and candidate.entity not in names:
                names.append(candidate.entity)
    return sorted(dict.fromkeys(names), key=len, reverse=True)


def nearest_scope_from_candidates(candidates: Sequence[HeadingSubjectCandidate]) -> List[str]:
    for candidate in reversed(candidates):
        if candidate.is_entity:
            return [candidate.entity]
        if candidate.entity_candidates:
            return list(candidate.entity_candidates)
    return []


def nearest_heading_scope_candidates(
    heading_path: Sequence[str],
    registry_entries: Sequence[object] = (),
) -> List[str]:
    """Return the nearest heading scope without collapsing a multi-entity title."""
    return nearest_scope_from_candidates(heading_path_candidates(heading_path, registry_entries))


def _clean_local_candidate(value: str) -> str:
    candidate = normalize_subject_name(value or "").canonical_name
    candidate = _LEADING_CONNECTOR_RE.sub("", candidate).strip(" 的，,。；;：:")
    candidate = re.split(r"[。；;，,]", candidate)[-1].strip()
    candidate = re.sub(r"(?:一般|通常|主要|基本)?(?:是|为)$", "", candidate).strip(" 的，,。；;：:")
    # Property regexes can capture a condition tail before the actual predicate,
    # e.g. “起爆混合炸药质量分数A/B为90/10，密度为…”.  Keep the nearest
    # material/device noun and treat the remainder as condition evidence.
    condition_match = _CONDITION_TAIL_RE.search(candidate)
    if condition_match:
        prefix = candidate[:condition_match.start()].strip(" 的，,。；;：:")
        entity_match = _ENTITY_PREFIX_RE.match(prefix)
        if entity_match:
            candidate = entity_match.group("entity")
        elif prefix:
            candidate = prefix
    return strip_heading_number(candidate)


def _mention_role(text: str, start: int, end: int) -> str:
    before = text[max(0, start - 16):start]
    after = text[end:end + 20]
    if _REFERENCE_PREFIX_RE.search(before):
        return "comparison_reference"
    if _REAGENT_PREFIX_RE.search(before) and _REAGENT_SUFFIX_RE.search(after):
        return "condition_or_reagent"
    if re.match(rf"^\s*(?:的)?(?:{_PROPERTY_ALT})", after):
        return "property_owner"
    if re.match(r"^\s*(?:具有|呈现|表现为|属于|是一种|为一种|为|是)", after):
        return "entity_statement"
    return "simple_mention"


def detect_local_subject_evidence(
    text: str,
    lexicon: Sequence[str] = (),
    *,
    heading_subject: str = "",
) -> List[LocalSubjectEvidence]:
    value = text or ""
    evidence: List[LocalSubjectEvidence] = []

    for pattern in _OWNER_PATTERNS:
        for match in pattern.finditer(value):
            candidate = _clean_local_candidate(match.group("subject"))
            if not is_entity_surface(candidate, lexicon):
                continue
            role = "property_owner" if "的" in match.group(0) or re.search(_PROPERTY_ALT, match.group(0)) else "entity_statement"
            evidence.append(LocalSubjectEvidence(candidate, role, 0.94, match.group(0), ["explicit_local_owner_syntax"]))

    composition_spans: set[tuple[int, int]] = set()
    for pattern in _COMPOSITION_OWNER_PATTERNS:
        for match in pattern.finditer(value):
            candidate = _clean_local_candidate(match.group("subject"))
            composition_spans.add(match.span())
            if is_entity_surface(candidate, lexicon):
                evidence.append(LocalSubjectEvidence(
                    candidate, "composition_owner", 0.96, match.group(0),
                    ["explicit_composition_owner_syntax"],
                ))
            elif candidate and not _DEICTIC_ONLY_ENTITY_RE.fullmatch(candidate):
                # Preserve the fact that the sentence has an explicit owner even
                # when that owner is not a legal graph subject (e.g. a property
                # quantity).  The resolver can then fail closed instead of
                # assigning the fact to a chapter heading.
                evidence.append(LocalSubjectEvidence(
                    candidate, "explicit_owner_conflict", 0.90, match.group(0),
                    ["explicit_non_entity_owner_blocks_heading_fallback"],
                ))

    for name in sorted(dict.fromkeys(lexicon), key=len, reverse=True):
        if not name:
            continue
        if re.fullmatch(r"[A-Za-z0-9+._/-]+", name):
            matches = re.finditer(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", value, re.I)
        else:
            matches = re.finditer(re.escape(name), value)
        for match in matches:
            role = _mention_role(value, match.start(), match.end())
            confidence = {
                "property_owner": 0.95,
                "entity_statement": 0.91,
                "comparison_reference": 0.35,
                "condition_or_reagent": 0.30,
                "simple_mention": 0.66,
            }[role]
            evidence.append(LocalSubjectEvidence(name, role, confidence, match.group(0), ["document_subject_lexicon_mention"]))

    by_key: dict[tuple[str, str], LocalSubjectEvidence] = {}
    for item in evidence:
        key = (normalize_subject_name(item.subject).normalized_key, item.role)
        previous = by_key.get(key)
        if previous is None or item.confidence > previous.confidence:
            by_key[key] = item
    return sorted(by_key.values(), key=lambda item: (not item.is_owner, -item.confidence, item.subject))


def select_local_owner(evidence: Sequence[LocalSubjectEvidence]) -> LocalSubjectEvidence | None:
    owners: dict[str, LocalSubjectEvidence] = {}
    for item in evidence:
        if not item.is_owner or item.confidence < 0.82:
            continue
        key = normalize_subject_name(item.subject).normalized_key
        previous = owners.get(key)
        if previous is None or item.confidence > previous.confidence:
            owners[key] = item
    if len(owners) == 1:
        return next(iter(owners.values()))
    return None


__all__ = [
    "HeadingSubjectCandidate", "LocalSubjectEvidence", "strip_heading_number",
    "classify_heading_subject", "heading_path_candidates", "nearest_heading_subject",
    "build_subject_lexicon", "registry_subject_names", "nearest_scope_from_candidates", "nearest_heading_scope_candidates", "detect_local_subject_evidence", "select_local_owner",
    "is_topic_only_title", "is_entity_surface",
]
