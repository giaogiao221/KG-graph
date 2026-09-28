from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import List, Tuple

from book_engine.core.schemas import TableBlock
from book_engine.tables.condition_rules import INSTRUMENT_WORDS, METHOD_TOKENS
from book_engine.routing.heading_subject_resolver import classify_heading_subject, nearest_heading_subject, is_entity_surface, strip_heading_number


@dataclass
class ContextMetadata:
    subject: str = ""
    subject_source: str = ""
    subject_confidence: float = 0.0
    method: str = ""
    instrument: str = ""
    reasons: List[str] = None

    def __post_init__(self) -> None:
        if self.reasons is None:
            self.reasons = []


_GENERIC_SUBJECTS = {
    "材料", "含能材料", "炸药", "推进剂", "固体推进剂", "高能推进剂", "火药", "烟火药",
    "性能", "试验", "实验", "结果", "参数", "条件", "样品", "试样", "配方", "体系",
    "不同材料", "不同样品", "不同配方", "几种材料", "几种炸药", "几种推进剂",
}

_PROPERTY_TERMS = (
    "理化指标和检验方法", "理化指标", "性能参数", "性能", "性质", "特性", "参数", "组成", "配方",
    "测试结果", "试验结果", "实验结果", "检验结果", "密度", "爆速", "爆压", "爆热", "燃速",
    "比冲", "感度", "熔点", "沸点", "峰顶温度", "分解温度", "生成热", "生成焓", "氧平衡",
    "粒径", "粒度", "黏度", "粘度", "力学性能", "燃烧性能", "工艺性能", "相容性", "安定性",
    "得率", "产率", "收率", "相对分子质量", "分子量", "取代度", "官能度", "模量", "应力", "应变",
    "热分析", "测试", "试验", "实验",
)



_HEADING_NUMBER_RE = re.compile(
    # Match dotted section numbers before the short single-number alternative;
    # otherwise ``4.6.2`` is consumed as ``4.`` and leaves ``6.2`` in the name.
    r"^\s*(?:(?:第?[一二三四五六七八九十百\d]+[章节篇部分])|(?:\d+(?:[.．]\d+){1,4})|(?:[（(]?[一二三四五六七八九十百\d]+[）)]?[、.．)]?))[\s、.．)]*"
)
_CONDITION_TAIL_RE = re.compile(r"(?:在|于).{1,28}?(?:条件)?下$")
_MATERIAL_CODE_RE = re.compile(
    r"(?:HMX|RDX|TNT|PETN|CL-?20|NTO|ADN|AP|AN|GAP|HTPB|BAMO|AMMO|NIMMO|FOE|ECH|PECH|"
    r"PBAMO|ETPE|TSE[-–—－]?[A-Za-z0-9-]*|[A-Z][A-Z0-9]*(?:[-/][A-Z0-9]+)+)",
    re.IGNORECASE,
)
_HEADING_MATERIAL_HINT_RE = re.compile(
    r"(?:聚合物|预聚物|共聚物|均聚物|弹性体|发射药|推进剂|炸药|药剂|改性[A-Za-z0-9-]+|"
    r"硝基|叠氮|高氯酸|环氧|含能)",
    re.IGNORECASE,
)
_BROAD_CONTEXT_SUBJECT_RE = re.compile(
    r"^(?:(?:嵌段|无规|接枝|交替|支化|线型|结晶性|非晶)?(?:共聚物|均聚物|聚合物|预聚物|弹性体)|"
    r"材料|含能材料|炸药|推进剂|火药|火工品|药剂|体系|配方|样品|试样)$",
    re.IGNORECASE,
)
_CAUSAL_CAPTION_ENTITY_RE = re.compile(
    r"^(?P<subject>.+?)(?:的)?(?:成分|组成|配比|细度|粒度|含水量|水分含量|药量|直径|位置|埋深|"
    r"温度|压力|时间|浓度|密度|投料比).{0,36}?(?:对|与).{1,40}?(?:影响|关系)$",
    re.IGNORECASE,
)
_MIDDLE_CAUSAL_CAPTION_ENTITY_RE = re.compile(
    r"^(?P<subject>.+?)中.{1,40}?(?:对|与).{1,40}?(?:影响|关系)$", re.IGNORECASE
)


def _strip_condition_tail(value: str) -> str:
    result = _CONDITION_TAIL_RE.sub("", value or "").strip(" ，,、;；:：")
    return result


def _heading_subject(text: str) -> Tuple[str, float, str]:
    candidate = classify_heading_subject(text)
    if candidate.is_entity:
        return candidate.entity, candidate.confidence, f"heading_{candidate.kind}"
    return "", 0.0, ""


def _influence_subject(text: str) -> Tuple[str, float, str]:
    compact = _normalize_latex_surface(text)
    if not compact or "影响" not in compact:
        return "", 0.0, ""
    match = re.search(
        r"(?:表\s*\d+(?:[-－—.]\d+)*\s*)?(?:[^，。；;：:]{0,30}?对)?"
        r"(?P<subject>[A-Za-z0-9\-_/·\u4e00-\u9fff]{1,45}?)"
        r"(?:开环)?(?:聚合(?:反应)?|叠氮化反应|反应)?(?:的)?影响",
        compact,
        re.IGNORECASE,
    )
    if not match:
        return "", 0.0, ""
    candidate = _clean_candidate(match.group("subject"))
    candidate = re.sub(r"^(?:不同|各种|反应条件|引发剂和催化剂|引发剂|催化剂|溶剂|投料比)+对?", "", candidate)
    candidate = re.sub(r"(?:开环)?聚合(?:反应)?$|叠氮化反应$|反应$", "", candidate).strip()
    if _is_specific_subject(candidate) and (_MATERIAL_CODE_RE.search(candidate) or _HEADING_MATERIAL_HINT_RE.search(candidate)):
        return candidate, 0.86, "influence_object_subject"
    return "", 0.0, ""


def _explicit_product_subject(text: str) -> Tuple[str, float, str]:
    compact = _normalize_latex_surface(text)
    if not compact:
        return "", 0.0, ""
    match = re.search(
        r"(?:产物|所得(?:产物)?|制得(?:产物)?)\s*"
        r"(?P<subject>(?:\([A-Za-z]{1,8}\)-?)?[A-Za-z][A-Za-z0-9\-_/]{1,45})"
        r"(?:的)?(?:得率|产率|收率|相对分子质量|分子量|性质|性能)",
        compact,
        re.IGNORECASE,
    )
    if match:
        candidate = _clean_candidate(match.group("subject"))
        if _is_specific_subject(candidate):
            return candidate, 0.98, "explicit_product_subject"
    return "", 0.0, ""


def _normalize_latex_surface(text: str) -> str:
    value = unicodedata.normalize("NFKC", text or "")
    value = value.replace("\\prime", "′").replace("\\mathrm", "")
    value = value.replace("$", "").replace("{", "").replace("}", "").replace("^", "")
    value = value.replace("\\", "")
    value = re.sub(r"\s*([,，.·′/+\-])\s*", r"\1", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _clean_candidate(text: str) -> str:
    value = _normalize_latex_surface(text)
    value = re.sub(r"^\s*表\s*\d+(?:[-－—.]\d+)*\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^[：:、，,。\s]+|[：:、，,。\s]+$", "", value)
    # Captions such as “GAP的理化性质” are parsed against the suffix
    # “性质”; trim the residual descriptor so it cannot become part of
    # the material name (for example, “GAP的理化”).
    value = re.sub(
        r"(?:的)?(?:理化|物理化学)(?:性质|性能|指标)?$",
        "",
        value,
        flags=re.IGNORECASE,
    ).strip(" 的")
    # When a caption contains two coordinated attributes, a suffix parser may
    # consume only the final leaf and leave a dangling connector such as
    # “材料的组成及”.  Remove that incomplete topic tail, but preserve the
    # material/entity span itself.
    value = re.sub(
        r"(?:的)?(?:组成|配方|性能|性质|结构|参数|指标|结果)(?:和|及|与)$",
        "",
        value,
        flags=re.IGNORECASE,
    ).strip(" 的")
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _is_specific_subject(value: str) -> bool:
    if not value or value in _GENERIC_SUBJECTS:
        return False
    if len(value) > 60:
        return False
    if any(value.endswith(term) for term in ("影响", "关系", "规律", "方法", "模型", "比较", "结果")):
        return False
    if re.fullmatch(r"第?\d+(?:\.\d+)*章?", value):
        return False
    return bool(re.search(r"[A-Za-z0-9\u4e00-\u9fff]", value))


def _is_broad_context_subject(value: str) -> bool:
    compact = re.sub(r"\s+", "", value or "")
    return bool(_BROAD_CONTEXT_SUBJECT_RE.fullmatch(compact) or compact in _GENERIC_SUBJECTS)


def _caption_subject(text: str) -> Tuple[str, float, str]:
    compact = _normalize_latex_surface(text)
    if not compact or not re.match(r"^\s*表\s*\d", compact, re.IGNORECASE):
        return "", 0.0, ""
    caption = re.sub(r"^\s*表\s*\d+(?:[-－—.]\d+)*\s*", "", compact, flags=re.IGNORECASE).strip()
    semantic = classify_heading_subject(caption)
    semantic_entity = _clean_candidate(semantic.entity) if semantic.is_entity else ""
    if semantic_entity and _is_specific_subject(semantic_entity):
        confidence = 0.90 if _is_broad_context_subject(semantic_entity) else 0.95
        return semantic_entity, confidence, "semantic_table_caption_subject"
    causal_candidates = []
    for pattern in (_CAUSAL_CAPTION_ENTITY_RE, _MIDDLE_CAUSAL_CAPTION_ENTITY_RE):
        causal = pattern.fullmatch(caption)
        if not causal:
            continue
        candidate = _clean_candidate(causal.group("subject"))
        if is_entity_surface(candidate) and not _is_broad_context_subject(candidate):
            causal_candidates.append(candidate)
    if causal_candidates:
        candidate = min(causal_candidates, key=len)
        return candidate, 0.95, "causal_caption_explicit_entity"
    # Fall back to the older suffix parser for terse captions.
    suffix = "|".join(sorted((re.escape(term) for term in _PROPERTY_TERMS), key=len, reverse=True))
    match = re.search(
        rf"(?P<subject>.{{1,60}}?)(?:的)?(?:{suffix})(?:\b|和|及|$)", caption, re.IGNORECASE,
    )
    if match:
        candidate = _strip_condition_tail(_clean_candidate(match.group("subject")))
        if _is_specific_subject(candidate):
            return candidate, 0.88 if _is_broad_context_subject(candidate) else 0.93, "table_caption_subject"
    return "", 0.0, ""


def _extract_subject_from_text(text: str) -> Tuple[str, float, str]:
    if not text:
        return "", 0.0, ""
    # A sentence may itself be used as a table caption. Prefer an explicit
    # product mention over the generic caption pattern so the subject becomes
    # ``(RS)-GAP`` rather than the whole synthesis sentence.
    product = _explicit_product_subject(text)
    if product[0]:
        return product
    caption = _caption_subject(text)
    if caption[0]:
        return caption
    influence = _influence_subject(text)
    if influence[0]:
        return influence
    compact = _normalize_latex_surface(text)
    patterns = [
        r"(?:表\s*\d+(?:[-－—.]\d+)*\s*)?(?P<subject>[^，。；;：:]{1,45}?)的(?:" + "|".join(map(re.escape, _PROPERTY_TERMS)) + r")",
        r"(?:表\s*\d+(?:[-－—.]\d+)*\s*)?(?P<subject>[^，。；;：:]{1,45}?)(?:试验结果|实验结果|测试结果|性能参数)",
        r"(?P<subject>[A-Za-z][A-Za-z0-9\-_/]{1,24})\s*(?:体系|配方)?(?:的)?(?:" + "|".join(map(re.escape, _PROPERTY_TERMS)) + r")",
    ]
    for pattern in patterns:
        match = re.search(pattern, compact, re.IGNORECASE)
        if not match:
            continue
        candidate = _clean_candidate(match.group("subject"))
        if _is_specific_subject(candidate):
            return candidate, 0.78, "context_pattern"
    return "", 0.0, ""


def _extract_method(text: str) -> str:
    if not text:
        return ""
    found = []
    for token in METHOD_TOKENS:
        if token.lower() in text.lower():
            found.append(token)
    method_pattern = re.search(r"(?:采用|使用|利用|按照|通过)\s*([^，。；;]{1,35}?(?:法|方法|测试|测定|分析))", text)
    if method_pattern:
        found.append(method_pattern.group(1).strip())
    unique = []
    for value in found:
        if value and value not in unique:
            unique.append(value)
    return "；".join(unique[:4])


def _extract_instrument(text: str) -> str:
    if not text:
        return ""
    pattern = re.search(r"(?:采用|使用|利用)?\s*([^，。；;]{1,35}?(?:仪|仪器|装置|设备|传感器|量规|光谱仪|显微镜))", text)
    if pattern:
        return pattern.group(1).strip()
    return ""


def extract_context_metadata(block: TableBlock) -> ContextMetadata:
    """Resolve table fallback subject with local evidence over heading defaults.

    The nearest entity heading defines a default scope.  Explicit product or
    table-caption ownership may override it; an influence-object mention only
    overrides when the heading is absent or broad.  This prevents a specific
    heading such as PECH from being replaced by a reagent/reference ECH while
    allowing a broad parent chapter to yield to FOE or black powder in the
    local caption.
    """
    heading_path = list(getattr(block, "heading_path", []) or [])
    synthetic_heading_path = False
    if not heading_path and block.heading:
        heading_path = [block.heading]
        synthetic_heading_path = True

    heading_candidate = nearest_heading_subject(heading_path)
    heading_subject = heading_candidate.entity if heading_candidate else ""
    heading_confidence = heading_candidate.confidence if heading_candidate else 0.0
    heading_broad = bool(
        heading_candidate and (heading_candidate.broad_class or _is_broad_context_subject(heading_subject))
    )
    heading_source = ""
    if heading_candidate:
        # Find the actual nearest entity level for auditable source provenance.
        entity_index = 0
        for index in range(len(heading_path) - 1, -1, -1):
            candidate = classify_heading_subject(heading_path[index], depth=index)
            if candidate.is_entity:
                entity_index = index + 1
                break
        heading_source = "heading" if synthetic_heading_path and len(heading_path) == 1 else f"heading_path:{entity_index}"

    local_candidates: List[Tuple[float, int, str, str, str]] = []
    for source_name, text, priority in (
        ("preceding_text", block.preceding_text, 5),
        ("following_text", block.following_text, 4),
    ):
        product = _explicit_product_subject(text)
        if product[0]:
            local_candidates.append((product[1], priority + 3, product[0], source_name, product[2]))
        caption = _caption_subject(text)
        if caption[0]:
            local_candidates.append((caption[1], priority + 2, caption[0], source_name, caption[2]))
        influence = _influence_subject(text)
        if influence[0]:
            local_candidates.append((influence[1], priority + 1, influence[0], source_name, influence[2]))
        fallback = _extract_subject_from_text(text)
        if fallback[0] and fallback[2] not in {product[2], caption[2], influence[2]}:
            local_candidates.append((fallback[1], priority, fallback[0], source_name, fallback[2]))

    chosen: Tuple[float, str, str, str] | None = None
    # Explicit products are always stronger than a chapter default.
    products = [item for item in local_candidates if item[4] == "explicit_product_subject"]
    if products:
        item = max(products, key=lambda x: (x[0], x[1]))
        chosen = (item[0], item[2], item[3], item[4])
    else:
        captions = [item for item in local_candidates if "caption" in item[4]]
        specific_captions = [item for item in captions if not _is_broad_context_subject(item[2])]
        if specific_captions and (not heading_subject or heading_broad or any(item[2] != heading_subject for item in specific_captions)):
            item = max(specific_captions, key=lambda x: (x[0], x[1], len(x[2])))
            chosen = (item[0], item[2], item[3], item[4])
        elif not heading_subject:
            non_broad = [item for item in local_candidates if not _is_broad_context_subject(item[2])]
            if non_broad:
                item = max(non_broad, key=lambda x: (x[0], x[1]))
                chosen = (item[0], item[2], item[3], item[4])
        elif heading_broad:
            non_broad = [item for item in local_candidates if not _is_broad_context_subject(item[2])]
            if non_broad:
                item = max(non_broad, key=lambda x: (x[0], x[1]))
                chosen = (item[0], item[2], item[3], item[4])

    if chosen is None and heading_subject:
        chosen = (heading_confidence, heading_subject, heading_source, heading_candidate.kind if heading_candidate else "heading")
    if chosen is None and local_candidates:
        item = max(local_candidates, key=lambda x: (x[0], x[1]))
        chosen = (item[0], item[2], item[3], item[4])

    subject = chosen[1] if chosen else ""
    subject_source = chosen[2] if chosen else ""
    subject_confidence = chosen[0] if chosen else 0.0
    reasons: List[str] = []
    if chosen:
        reasons.extend([chosen[3], f"source:{subject_source}"])
        if heading_subject and subject != heading_subject:
            reasons.append("explicit_local_subject_overrode_heading_default")

    sources = [block.heading, block.preceding_text, block.following_text] + heading_path
    combined = " ".join(text for text in sources if text)
    method = _extract_method(combined)
    instrument = _extract_instrument(combined)
    if method:
        reasons.append("context_method_detected")
    if instrument:
        reasons.append("context_instrument_detected")
    return ContextMetadata(
        subject=subject,
        subject_source=subject_source,
        subject_confidence=subject_confidence,
        method=method,
        instrument=instrument,
        reasons=reasons,
    )


__all__ = ["ContextMetadata", "extract_context_metadata"]
