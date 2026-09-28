from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Sequence, Tuple

from book_engine.document.block_segmenter import TextBlock
from book_engine.routing.heading_subject_resolver import strip_heading_number
from book_engine.routing.subject_type_inferer import infer_subject_type
from book_engine.routing.text_subject_anchor_resolver import TextSubjectAnchor, is_plausible_subject_name
from book_engine.text.evidence_self_containment import iter_clauses, is_deictic_subject
from book_engine.text.text_fact_extractor import (
    TextFactRecord, _clean_owner_candidate, _is_explicit_entity, _looks_like_toc_page_line, _make_record,
)

# Phase 10.3 adds deterministic relation compilers only to narrative_monograph.
# All rules are clause-local, evidence-preserving and fail closed.  They never
# correct OCR or invent a missing subject/value.

_OWNER = r"[^。；;，,:：]{2,64}?"
_VALUE = r"[^。；;]{2,220}"

_CLASSIFY_RE = re.compile(rf"(?P<owner>{_OWNER})\s*(?:通常|一般|主要|大致|可)?(?:分为|分成|分作|可分为)\s*(?P<value>{_VALUE})")
_OWNERLESS_CLASSIFY_RE = re.compile(r"^(?:该材料|该物质|该体系|该装置|它|其)\s*(?:通常|一般|主要|大致|可)?(?:分为|分成|分作)\s*(?P<value>[^。；;]{2,220})")
_DEFINITION_RE = re.compile(rf"(?P<owner>{_OWNER})\s*(?:被)?(?:是指|指的是|定义为|可定义为|称为|叫做)\s*(?P<value>{_VALUE})")
_APPLICATION_RE = re.compile(rf"(?P<owner>{_OWNER})\s*(?:可|能|能够|常|主要|广泛|已经|已)?(?:用于|应用于|用作|适用于)\s*(?P<value>{_VALUE})")
_AS_ROLE_RE = re.compile(rf"(?P<owner>{_OWNER})\s*(?:可|能|能够|常|主要|广泛|已经|已)?作为\s*(?P<value>{_VALUE})")
_OWNERLESS_APPLICATION_RE = re.compile(
    r"^(?:(?:该材料|该物质|该体系|该装置|本品)\s*(?:可|能|能够|常|主要|广泛|已经|已)?(?:用于|应用于|用作|适用于)|"
    r"(?:可|能|能够|常|主要|广泛|通常|一般|已经|已)(?:用于|应用于|用作|适用于)|用作|适用于)"
    r"\s*(?P<value>[^。；;]{2,220})"
)
_IN_APPLICATION_RE = re.compile(rf"(?P<owner>{_OWNER})\s*(?:已|可|能|能够|常|主要|广泛)?在\s*(?P<value>[^。；;，,]{{2,120}})中(?:得到|获得)?(?:广泛)?应用")
_FUNCTION_RE = re.compile(rf"(?P<owner>{_OWNER})\s*(?:的)?(?:主要)?(?:功能|作用)(?:是|为|在于)\s*(?P<value>{_VALUE})")
_ACTION_FUNCTION_RE = re.compile(rf"(?P<owner>{_OWNER})\s*(?:可|能|能够)?(?:起到?|发挥|实现|完成)\s*(?P<value>[^。；;]{{2,160}}?(?:作用|功能|任务|目的|动作))")
_OWNERLESS_FUNCTION_RE = re.compile(r"^(?:该部件|该元件|该装置|该系统|它|其)\s*(?:可|能|能够)?(?:起到?|发挥|实现|完成)\s*(?P<value>[^。；;]{2,160}?(?:作用|功能|任务|目的|动作))")
_INCLUDE_RE = re.compile(rf"(?P<owner>{_OWNER})\s*(?:主要)?(?:包括|包含)\s*(?P<value>{_VALUE})")
_BY_COMPOSITION_RE = re.compile(rf"(?P<owner>{_OWNER})\s*(?:主要)?由\s*(?P<value>[^。；;]{{2,220}}?)\s*(?:组成|构成)")
_OWNERLESS_INCLUDE_RE = re.compile(r"^(?:(?:该装置|该系统|该元件|该组件)\s*)?(?:主要)?(?:包括|包含)\s*(?P<value>[^。；;]{2,220})")
_OWNERLESS_BY_COMPOSITION_RE = re.compile(r"^(?:(?:该装置|该系统|该元件|该组件)\s*)?(?:主要)?由\s*(?P<value>[^。；;]{2,220}?)\s*(?:组成|构成)")
_CONNECTION_RE = re.compile(rf"(?P<owner>{_OWNER})\s*(?:与|和)\s*(?P<target>{_OWNER})\s*(?P<verb>连接|相连|联接|相接|耦合|配合)")
_LOCATION_RE = re.compile(rf"(?P<owner>{_OWNER})\s*(?:被)?(?:安装|设置|装在|位于)\s*(?:于|在|到)?\s*(?P<target>[^。；;，,]{{2,120}})")
_METHOD_RE = re.compile(rf"(?P<owner>{_OWNER})\s*(?:可|通常|一般)?(?:采用|使用|利用|基于)\s*(?P<value>[^。；;，,]{{2,120}}?(?:法|方法|技术|模型|算法|装置|仪器))")
_OWNERLESS_METHOD_RE = re.compile(r"^(?:采用|使用|利用|基于)\s*(?P<value>[^。；;，,]{2,120}?(?:法|方法|技术|模型|算法|装置|仪器))")
_COMPARISON_RE = re.compile(rf"(?P<left>{_OWNER})\s*(?:的\s*(?P<metric>[\u4e00-\u9fffA-Za-z]{{1,20}}))?\s*(?:比|较)\s*(?P<right>{_OWNER})\s*(?:的)?\s*(?P<direction>高|低|大|小|快|慢|强|弱|好|差|优|劣|长|短)")
_COMPARATIVE_VERB_RE = re.compile(rf"(?P<left>{_OWNER})\s*(?P<verb>高于|低于|优于|劣于|快于|慢于|强于|弱于|大于|小于)\s*(?P<right>{_OWNER})")
_CAUSAL_RE = re.compile(r"(?P<cause>[^。；;，,]{3,100}?)\s*(?:会|可|能|能够)?(?P<verb>导致|引起|造成|促使|使得)\s*(?P<effect>[^。；;]{3,180})")
_EFFECT_RE = re.compile(rf"(?P<owner>{_OWNER})\s*(?:可|能|能够|会|可以)?(?P<action>提高|降低|促进|抑制|改善|增加|减小|减少|缩短|延长|增强|削弱|加快|减慢)\s*(?P<object>[^。；;，,]{{2,150}})")
_EXPERIMENT_RESULT_RE = re.compile(rf"(?P<owner>{_OWNER})\s*(?:在[^。；;，,]{{1,100}}(?:条件)?下)?\s*(?:表现出|出现|发生|观察到|测得)\s*(?P<value>[^。；;]{{2,180}})")

_SAFETY_SIGNAL_RE = re.compile(r"(?:必须|应当|应该|应能|应不|应保持|应避免|应防止|应符合|应满足|要求|严禁|不得|禁止|避免|防止|注意|小心|安全(?:性|要求|措施)?|危险|储存|贮存|运输|搬运|泄漏|消防|销毁|防护(?:措施|要求|方法))")
_STORAGE_RE = re.compile(r"(?:储存|贮存|保管)")
_TRANSPORT_RE = re.compile(r"(?:运输|搬运|装卸)")
_METHOD_HEADING_RE = re.compile(r"(?:计算方法|求解方法|分析方法|测试方法|试验方法|检验方法|算法|迭代方法|计算流程|求解流程|操作程序|工作流程|方法)$")
_METHOD_STEP_MARKER_RE = re.compile(r"^\s*(?P<label>(?:[（(]\s*(?P<n1>\d{1,3})\s*[）)]|(?P<n2>\d{1,3})[、.．)]|第\s*(?P<cn>[一二三四五六七八九十]+)\s*步))\s*")
_METHOD_ACTION_RE = re.compile(r"(?:建立|假定|列出|求解|计算|检验|判断|修正|比较|迭代|输入|输出|选择|确定|设置|更新|代入|展开|构造|调用|读取|保存|分析|评价|测定|测试)")

_GENERIC_ANCHORS = {"材料", "物质", "体系", "样品", "试样", "方法", "模型", "装置", "系统", "本章", "本文"}
_EXPERIMENT_FACTOR_OWNERS = {"引发剂", "催化剂", "溶剂", "反应温度", "反应时间", "投料比", "单体", "共聚单体", "添加剂"}
_INVALID_LOCAL_OWNER = {
    "密度", "熔点", "沸点", "爆速", "爆压", "爆热", "燃速", "性能", "性质", "结果", "条件", "温度", "压力", "时间", "方法", "模型",
    "无论", "特别", "所以", "因此", "此外", "同时", "进一步", "人们", "没有", "只有", "直接", "随后", "其中", "这", "它", "其", "使它", "都", "均", "可", "会",
}
_BAD_OWNER_PREFIX_RE = re.compile(r"^(?:无论|特别|所以|因此|此外|同时|进一步|有可能|且|而且|但是|然而|其中|人们|没有|只有|直接|随后|当|在|由于|这是因|图\s*\d+|表\s*\d+|这种|这类|该类|该|对应的|按|根据|有时|因为|改进为|使|用|故|还需|从而|如|最后|从总体|与)")
_BAD_OWNER_TOKEN_RE = re.compile(r"(?:建议|研究|发现|表明|显示|认为|采用|使用|利用|进行|得到|获得|制得|生成|形成|用于|应用于|作为|提高|降低|增加|减少|导致|造成|引起|可以|能够|需要|必须|应当|这种|这类|等人|会给)")
_PROPERTY_OWNER_RE = re.compile(r"(?:密度|温度|压力|时间|粒度|粒径|含量|能量水平|感度|爆速|爆压|爆热|燃速|得率|产率|粘度|黏度|分子量|熔点|沸点|性能|性质)$")
_COMPONENT_RE = re.compile(r"(?:桥丝|电极|壳体|管壳|药柱|药芯|隔板|隔离层|传爆管|导爆管|雷管|延期元件|点火元件|点火药|引火药|发火件|喷管|喷嘴|阀门|活塞|转子|定子|叶轮|腔体|导线|引线|接线柱|触点|开关|传感器|探头|部件|元件|组件)$")
_CONCEPT_RE = re.compile(r"(?:燃烧|反应|爆轰|爆炸|结晶|孪晶|模型|理论|方法|现象|过程|状态|结构|类型|类别|体系|机制|机理)$")
_CODE_RE = re.compile(r"[A-Za-z][A-Za-z0-9+._/\-]{1,24}")
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_TOC_ITEM_RE = re.compile(r"(?:^|\s)\d+(?:[.．]\d+){1,4}[^。；;\n]{1,60}?(?:[（(]\s*\d{1,4}\s*[）)]|[.．·…]{2,}\s*\d{1,4})(?=\s|$)")
_TOC_PLAIN_ITEM_RE = re.compile(r"(?:^|\s)\d+(?:[.．]\d+){1,4}\s*[^。；;\n]{1,60}?\s+\d{2,4}(?=\s|$)")
_SOURCE_INTRO_OWNER_RE = re.compile(r"^(?:(?:最近|已有|有关|一些)?(?:文献|资料)(?:还|曾|已经)?(?:介绍|报道|提出)(?:了)?(?:一种|一类)?|研究者(?:还|曾)?(?:介绍|报道|提出)(?:了)?(?:一种|一类)?|(?:实验|试验|研究)(?:证实|表明|发现))")
_SURFACE_USE_OWNER_RE = re.compile(r"^(?:内表面|外表面|表面)(?:用|采用)")
_GENERIC_LOCATIVE_OWNER_RE = re.compile(r"(?:中|内)(?:固体|液体|气体|某些)?(?:物质|成分|含量|产物)$")
_GENERIC_NONSPECIFIC_OWNER_RE = re.compile(r"^(?:一种|某种|某些|一些|有些)?(?:(?:化学)?物质|材料|化合物|组分|成分)$")


def _looks_like_toc_block(text: str) -> bool:
    surface = text or ""
    nonempty = [line for line in surface.splitlines() if line.strip()]
    if nonempty and sum(_looks_like_toc_page_line(line) for line in nonempty) / len(nonempty) >= 0.5:
        return True
    # OCR often collapses several TOC items into one physical line. Two or more
    # numbered-title-page tuples are enough to keep the whole block out of the
    # relation compiler.
    if len(_TOC_ITEM_RE.findall(surface)) >= 2:
        return True
    # Some OCR text preserves section numbers and trailing page numbers but
    # drops dot leaders. Three or more such tuples are still a TOC/index block.
    plain_lines = [
        line for line in nonempty
        if re.fullmatch(r"\s*\d+(?:[.．]\d+){1,4}\s*[^。！？!?]{1,80}?\s+\d{2,4}\s*", line)
    ]
    if len(plain_lines) >= 3 and len(plain_lines) / max(1, len(nonempty)) >= 0.5:
        return True
    if len(_TOC_PLAIN_ITEM_RE.findall(surface)) >= 3:
        return True
    if len(re.findall(r"[（(]\s*\d{2,4}\s*[）)]", surface)) >= 3 and len(re.findall(r"\d+(?:[.．]\d+){1,4}", surface)) >= 2:
        return True
    return False


@dataclass
class NarrativeRelationAudit:
    record_id: str
    block_id: str
    role: str
    relation_kind: str
    subject: str
    property_name: str
    value_text: str
    rule: str
    confidence: float
    evidence: str


def _balanced_delimiters(text: str) -> bool:
    pairs = (("(", ")"), ("（", "）"), ("[", "]"), ("【", "】"))
    return all(text.count(left) == text.count(right) for left, right in pairs)


def _safe_anchor(block: TextBlock, anchor: TextSubjectAnchor | None) -> bool:
    if anchor is None or anchor.status != "confirmed" or not anchor.subject:
        return False
    if anchor.subject in _GENERIC_ANCHORS or is_deictic_subject(anchor.subject):
        return False
    if not is_plausible_subject_name(anchor.subject):
        return False
    if not _balanced_delimiters(anchor.subject) or _BAD_OWNER_PREFIX_RE.search(anchor.subject):
        return False
    if _BAD_OWNER_TOKEN_RE.search(anchor.subject) or re.search(r"(?:是|为|得到|制得|开始|结束)", anchor.subject):
        return False
    return block.role in {"material_profile", "formulation", "process", "experiment", "comparison", "application_safety", "method_model", "device_system", "theory", "property"}


def _anchor_is_heading_owner(block: TextBlock, anchor: TextSubjectAnchor) -> bool:
    heading = strip_heading_number(block.heading_title or "").strip()
    return bool(heading and anchor.subject and (anchor.subject in heading or heading in anchor.subject))


def _clean_segment(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip(" ，,。；;：:")


def _relation_segments(clause: str) -> List[str]:
    # A relation owner must be in the same comma-local span as its predicate.
    # Colon is kept because definitions and field-like statements use it.
    return [s for s in (_clean_segment(x) for x in re.split(r"[，,]", clause or "")) if len(s) >= 4]


def _recover_local_owner(raw: str) -> str:
    text = _clean_segment(raw)
    # In “研究者建议将ADN作为…” only the span after the last disposal marker
    # can own the relation.
    pieces = re.split(r"(?:建议)?(?:将|把|以)", text)
    if len(pieces) > 1 and pieces[-1].strip():
        text = pieces[-1].strip()
    text = re.sub(r"^(?:也就是说|换句话说|但是|但|而|所谓|成熟的|是)", "", text).strip()
    text = _SOURCE_INTRO_OWNER_RE.sub("", text).strip()
    text = _SURFACE_USE_OWNER_RE.sub("", text).strip()
    text = re.sub(r"^(?:加入|添加)(?=[\u4e00-\u9fffA-Za-z])", "", text).strip()
    # In “催化剂A对材料B起作用”, A owns the function; the target after
    # “对” is not part of the owner surface.
    if "对" in text:
        left, right = text.split("对", 1)
        if 2 <= len(left.strip()) <= 24 and right.strip():
            text = left.strip()
    text = re.sub(r"被$", "", text).strip()
    return _clean_owner_candidate(text)


def _explicit_owner(raw: str, *, allow_component: bool = False, allow_concept: bool = False) -> str:
    owner = _recover_local_owner(raw)
    if not owner or is_deictic_subject(owner) or owner in _INVALID_LOCAL_OWNER or owner in _EXPERIMENT_FACTOR_OWNERS:
        return ""
    if len(owner) > 36 or not _balanced_delimiters(owner) or _BAD_OWNER_PREFIX_RE.search(owner) or _BAD_OWNER_TOKEN_RE.search(owner):
        return ""
    if re.search(r"(?:textcircled|\\circled|\\mathbf|\\mathrm)", owner) or re.search(r"(?:也|即|故|从而)$", owner):
        return ""
    if _PROPERTY_OWNER_RE.search(owner) or "的" in owner or re.search(r"(?:地|得)$", owner):
        return ""
    if _GENERIC_LOCATIVE_OWNER_RE.search(owner) or _GENERIC_NONSPECIFIC_OWNER_RE.fullmatch(owner):
        return ""
    if not is_plausible_subject_name(owner):
        return ""
    if _is_explicit_entity(owner):
        return owner
    if _CODE_RE.fullmatch(owner):
        return owner
    if allow_component and _COMPONENT_RE.search(owner) and len(owner) <= 18:
        return owner
    if allow_concept and _CONCEPT_RE.search(owner) and len(owner) <= 18 and not re.search(r"(?:这种|上述|以下|所谓|一种|另一种)$", owner):
        return owner
    return ""


def _trim_value(value: str) -> str:
    text = _clean_segment(value)
    text = re.sub(r"\[[0-9,，\-–—]+\]\s*$", "", text).strip()
    return text[:600]


def _strong_target(raw: str, *, allow_component: bool = False, allow_concept: bool = False) -> str:
    return _explicit_owner(raw, allow_component=allow_component, allow_concept=allow_concept)


def _valid_relation_value(relation_kind: str, value: str) -> bool:
    text = _trim_value(value)
    if len(text) < 2 or len(text) > 260:
        return False
    if _looks_like_toc_block(text):
        return False
    if re.search(r"^(?:所以|因此|此外|同时|进一步|人们|没有|只有|图\s*\d+|表\s*\d+)", text):
        return False
    if relation_kind == "application" and re.search(
        r"(?:必须|应当|需要|首先要|有可能|兴趣(?:上升|下降)|的重大进展|的函数|的方法|的优点|的缺点|有其缺点|是由|可观察到|不再|考虑|为[^。]{0,40}的是|的是|(?:制备|处理).+时$|(?:时|后|地)$)", text
    ):
        return False
    if relation_kind == "composition" and re.search(r"(?:提高|降低|导致|造成|引起|用于|应用于|作为|制备|获得|得到|反应|变化|传导|对流|原理|性能|一致)", text):
        return False
    if relation_kind == "composition" and re.fullmatch(r"(?:两个|三个|若干|多个|几个)(?:部分|部件|组分|单元)", text):
        return False
    if relation_kind == "definition" and (len(text) > 220 or re.search(r"(?:时|当|下|后)$", text)):
        return False
    if relation_kind == "classification" and (len(text) > 220 or re.search(r"(?:温度|压力|时间|小于|大于|低于|高于|\d)", text)):
        return False
    if relation_kind == "function" and text in {"作用", "功能", "重要作用", "重要功能", "任务"}:
        return False
    if relation_kind == "location" and re.search(r"(?:的固定|均为|材料来完成|性质有关)$", text):
        return False
    return True


def _split_as_role_value(raw: str) -> tuple[str, str]:
    text = _trim_value(raw)
    nested = re.search(r"(?:广泛|有效地|主要)?(?:用于|应用于|适用于)", text)
    target = ""
    if nested:
        target = _trim_value(text[nested.end():])
        text = _trim_value(text[:nested.start()])
    else:
        role_match = re.match(r"(?P<role>[^的]{0,16}(?:氧化剂|还原剂|催化剂|固化剂|交联剂|粘合剂|燃料|组分|传感器|量规))的(?P<target>.+)", text)
        if role_match:
            text = _trim_value(role_match.group("role"))
            target = _trim_value(role_match.group("target"))
    text = re.split(r"(?:使用时|使用的|合成|进行|即可|的溶剂法|的(?:方法|工艺)|并且|并)", text, maxsplit=1)[0]
    return _trim_value(text), target


def _add_record(
    output: List[TextFactRecord], audits: List[NarrativeRelationAudit], seen: set[tuple[str, str, str, str]],
    block: TextBlock, anchor: TextSubjectAnchor, *, subject: str, property_name: str, value_text: str,
    relation_kind: str, source_type: str, confidence: float, clause: str, rule: str, owner_source: str,
) -> None:
    value = _trim_value(value_text)
    if not subject or not value or len(value) < 2 or not _valid_relation_value(relation_kind, value):
        return
    key = (subject, property_name, value, clause)
    if key in seen:
        return
    record = _make_record(
        block, anchor, property_name, value, source_type=source_type, confidence=confidence,
        relation_kind=relation_kind, subject_override=subject, fact_clause=clause,
        owner_evidence=clause, owner_source=owner_source,
    )
    record.normalized_value_text = value
    seen.add(key)
    output.append(record)
    audits.append(NarrativeRelationAudit(
        record_id=record.record_id, block_id=block.block_id, role=block.role,
        relation_kind=relation_kind, subject=record.subject, property_name=property_name,
        value_text=value, rule=rule, confidence=record.confidence, evidence=clause,
    ))


def _compile_segment_relations(
    segment: str, block: TextBlock, anchor: TextSubjectAnchor,
    output: List[TextFactRecord], audits: List[NarrativeRelationAudit], seen: set[tuple[str, str, str, str]],
) -> None:
    anchor_ok = _safe_anchor(block, anchor)

    for match in _CLASSIFY_RE.finditer(segment):
        owner = _explicit_owner(match.group("owner"), allow_concept=True)
        if owner:
            _add_record(output, audits, seen, block, anchor, subject=owner, property_name="分类", value_text=match.group("value"), relation_kind="classification", source_type="text_narrative_classification", confidence=0.86, clause=segment, rule="explicit_classification", owner_source="explicit_narrative_owner")
    if anchor_ok:
        match = _OWNERLESS_CLASSIFY_RE.search(segment)
        if match:
            _add_record(output, audits, seen, block, anchor, subject=anchor.subject, property_name="分类", value_text=match.group("value"), relation_kind="classification", source_type="text_narrative_classification", confidence=0.82, clause=segment, rule="anchored_classification", owner_source="confirmed_block_anchor")

    for match in _DEFINITION_RE.finditer(segment):
        owner = _explicit_owner(match.group("owner"), allow_concept=True)
        if owner:
            _add_record(output, audits, seen, block, anchor, subject=owner, property_name="定义", value_text=match.group("value"), relation_kind="definition", source_type="text_narrative_definition", confidence=0.86, clause=segment, rule="explicit_definition", owner_source="explicit_narrative_owner")

    for match in _APPLICATION_RE.finditer(segment):
        owner = _explicit_owner(match.group("owner"))
        if owner:
            prop = "适用范围" if "适用于" in match.group(0) else "用途"
            _add_record(output, audits, seen, block, anchor, subject=owner, property_name=prop, value_text=match.group("value"), relation_kind="application", source_type="text_narrative_application", confidence=0.86, clause=segment, rule="explicit_application", owner_source="explicit_narrative_owner")
    for match in _AS_ROLE_RE.finditer(segment):
        owner = _explicit_owner(match.group("owner"))
        value, nested_target = _split_as_role_value(match.group("value"))
        category_like = bool(
            re.match(r"^(?:一种|一类|新型|新的|具有.+的)(?:化合物|材料|体系|类型|类别|炸药|推进剂)$", value)
            or re.search(r"的(?:一种|一类)$", value)
        )
        if owner and category_like:
            _add_record(output, audits, seen, block, anchor, subject=owner, property_name="分类", value_text=value, relation_kind="classification", source_type="text_narrative_classification", confidence=0.84, clause=segment, rule="explicit_category_role", owner_source="explicit_narrative_owner")
        elif owner:
            _add_record(output, audits, seen, block, anchor, subject=owner, property_name="用途", value_text=value, relation_kind="application", source_type="text_narrative_application", confidence=0.84, clause=segment, rule="explicit_functional_role", owner_source="explicit_narrative_owner")
        if owner and nested_target:
            _add_record(output, audits, seen, block, anchor, subject=owner, property_name="用途", value_text=nested_target, relation_kind="application", source_type="text_narrative_application", confidence=0.84, clause=segment, rule="nested_application_after_role", owner_source="explicit_narrative_owner")
    for match in _IN_APPLICATION_RE.finditer(segment):
        owner = _explicit_owner(match.group("owner"))
        if owner:
            _add_record(output, audits, seen, block, anchor, subject=owner, property_name="用途", value_text=match.group("value"), relation_kind="application", source_type="text_narrative_application", confidence=0.84, clause=segment, rule="application_context", owner_source="explicit_narrative_owner")
    if anchor_ok and _anchor_is_heading_owner(block, anchor):
        match = _OWNERLESS_APPLICATION_RE.search(segment)
        if match:
            prop = "适用范围" if "适用于" in match.group(0) else "用途"
            _add_record(output, audits, seen, block, anchor, subject=anchor.subject, property_name=prop, value_text=match.group("value"), relation_kind="application", source_type="text_narrative_application", confidence=0.82, clause=segment, rule="anchored_application", owner_source="confirmed_block_anchor")

    for pattern, rule in ((_FUNCTION_RE, "explicit_function"), (_ACTION_FUNCTION_RE, "explicit_action_function")):
        for match in pattern.finditer(segment):
            owner = _explicit_owner(match.group("owner"), allow_component=True)
            if owner:
                _add_record(output, audits, seen, block, anchor, subject=owner, property_name="功能", value_text=match.group("value"), relation_kind="function", source_type="text_narrative_function", confidence=0.86, clause=segment, rule=rule, owner_source="explicit_narrative_owner")
    if anchor_ok and block.role == "device_system":
        match = _OWNERLESS_FUNCTION_RE.search(segment)
        if match:
            _add_record(output, audits, seen, block, anchor, subject=anchor.subject, property_name="功能", value_text=match.group("value"), relation_kind="function", source_type="text_narrative_function", confidence=0.82, clause=segment, rule="anchored_device_function", owner_source="confirmed_block_anchor")

    if block.role in {"device_system", "formulation"}:
        for pattern, rule in ((_INCLUDE_RE, "explicit_include"), (_BY_COMPOSITION_RE, "explicit_by_composition")):
            for match in pattern.finditer(segment):
                owner = _explicit_owner(match.group("owner"), allow_component=True)
                if owner:
                    _add_record(output, audits, seen, block, anchor, subject=owner, property_name="组成描述", value_text=match.group("value"), relation_kind="composition", source_type="text_narrative_part_whole", confidence=0.86, clause=segment, rule=rule, owner_source="explicit_narrative_owner")
    if anchor_ok and block.role == "device_system" and re.match(r"^(?:该装置|该系统|该元件|该组件)", segment):
        for pattern, rule in ((_OWNERLESS_INCLUDE_RE, "anchored_include"), (_OWNERLESS_BY_COMPOSITION_RE, "anchored_by_composition")):
            match = pattern.search(segment)
            if match:
                _add_record(output, audits, seen, block, anchor, subject=anchor.subject, property_name="组成描述", value_text=match.group("value"), relation_kind="composition", source_type="text_narrative_part_whole", confidence=0.82, clause=segment, rule=rule, owner_source="confirmed_block_anchor")

    for match in _CONNECTION_RE.finditer(segment):
        owner = _explicit_owner(match.group("owner"), allow_component=True)
        target = _strong_target(match.group("target"), allow_component=True)
        if owner and target and not re.search(r"(?:的连接|的联接|的相连)", segment):
            _add_record(output, audits, seen, block, anchor, subject=owner, property_name="连接关系", value_text=f"与{target}{match.group('verb')}", relation_kind="connection", source_type="text_narrative_connection", confidence=0.86, clause=segment, rule="explicit_connection", owner_source="explicit_narrative_owner")
    if block.role == "device_system":
        for match in _LOCATION_RE.finditer(segment):
            owner = _explicit_owner(match.group("owner"), allow_component=True)
            target = _trim_value(match.group("target"))
            if owner and target and not _BAD_OWNER_TOKEN_RE.search(target):
                _add_record(output, audits, seen, block, anchor, subject=owner, property_name="安装位置", value_text=target, relation_kind="location", source_type="text_narrative_location", confidence=0.84, clause=segment, rule="explicit_location", owner_source="explicit_narrative_owner")

    for match in _METHOD_RE.finditer(segment):
        owner = _explicit_owner(match.group("owner"))
        if owner:
            _add_record(output, audits, seen, block, anchor, subject=owner, property_name="方法", value_text=match.group("value"), relation_kind="method", source_type="text_narrative_method", confidence=0.84, clause=segment, rule="explicit_method", owner_source="explicit_narrative_owner")
    if anchor_ok and _anchor_is_heading_owner(block, anchor) and block.role in {"method_model", "experiment", "process"}:
        match = _OWNERLESS_METHOD_RE.search(segment)
        if match:
            _add_record(output, audits, seen, block, anchor, subject=anchor.subject, property_name="方法", value_text=match.group("value"), relation_kind="method", source_type="text_narrative_method", confidence=0.80, clause=segment, rule="anchored_method", owner_source="confirmed_block_anchor")

    for match in _COMPARISON_RE.finditer(segment):
        owner = _explicit_owner(match.group("left"))
        right = _strong_target(match.group("right"))
        if owner and right:
            metric = _trim_value(match.group("metric"))
            value = f"{metric + ' ' if metric else ''}比{right}{match.group('direction')}"
            _add_record(output, audits, seen, block, anchor, subject=owner, property_name="比较结论", value_text=value, relation_kind="comparison", source_type="text_narrative_comparison", confidence=0.86, clause=segment, rule="explicit_comparison", owner_source="explicit_narrative_owner")
    for match in _COMPARATIVE_VERB_RE.finditer(segment):
        owner = _explicit_owner(match.group("left"))
        right = _strong_target(match.group("right"))
        if owner and right:
            _add_record(output, audits, seen, block, anchor, subject=owner, property_name="比较结论", value_text=f"{match.group('verb')}{right}", relation_kind="comparison", source_type="text_narrative_comparison", confidence=0.86, clause=segment, rule="comparative_verb", owner_source="explicit_narrative_owner")

    for match in _EFFECT_RE.finditer(segment):
        owner = _explicit_owner(match.group("owner"))
        if owner:
            _add_record(output, audits, seen, block, anchor, subject=owner, property_name="作用影响", value_text=f"{match.group('action')}{_trim_value(match.group('object'))}", relation_kind="effect", source_type="text_narrative_effect", confidence=0.84, clause=segment, rule="explicit_effect", owner_source="explicit_narrative_owner")

    if block.role == "experiment":
        for match in _EXPERIMENT_RESULT_RE.finditer(segment):
            owner = _explicit_owner(match.group("owner"))
            if owner:
                _add_record(output, audits, seen, block, anchor, subject=owner, property_name="试验结果", value_text=match.group("value"), relation_kind="experiment_result", source_type="text_narrative_experiment_result", confidence=0.84, clause=segment, rule="explicit_experiment_result", owner_source="explicit_narrative_owner")



def _compile_clause_relations(
    block: TextBlock, anchor: TextSubjectAnchor, output: List[TextFactRecord],
    audits: List[NarrativeRelationAudit], seen: set[tuple[str, str, str, str]],
) -> None:
    anchor_ok = _safe_anchor(block, anchor)
    for _, _, clause_raw in iter_clauses(block.text):
        clause = _trim_value(clause_raw)
        if len(clause) < 5 or len(clause) > 700:
            continue
        for segment in _relation_segments(clause):
            _compile_segment_relations(segment, block, anchor, output, audits, seen)
        safety_local = bool(
            anchor.subject in clause
            or re.match(r"^(?:储存|贮存|运输|搬运|严禁|不得|禁止|必须|应当|应该|要求|为此|危险|安全)", clause)
        )
        if anchor_ok and safety_local and block.role == "application_safety" and _SAFETY_SIGNAL_RE.search(clause) and len(clause) >= 10 and not re.search(r"(?:如果|当|若|感应|电)$", clause):
            prop = "储存要求" if _STORAGE_RE.search(clause) else ("运输要求" if _TRANSPORT_RE.search(clause) else "安全要求")
            _add_record(output, audits, seen, block, anchor, subject=anchor.subject, property_name=prop, value_text=clause, relation_kind="safety", source_type="text_narrative_safety", confidence=0.82, clause=clause, rule="anchored_safety_clause", owner_source="confirmed_block_anchor")
        # Causal statements may legitimately cross one comma.  They stay bound
        # to an already confirmed local subject rather than promoting the cause
        # phrase itself to an entity.
        if anchor_ok and block.role in {"theory", "comparison", "experiment"} and anchor.subject in clause:
            for match in _CAUSAL_RE.finditer(clause):
                cause = _trim_value(match.group("cause"))
                effect = _trim_value(match.group("effect"))
                if _BAD_OWNER_PREFIX_RE.search(cause) or len(cause) < 3 or len(effect) < 3:
                    continue
                value = f"{cause}{match.group('verb')}{effect}"
                _add_record(output, audits, seen, block, anchor, subject=anchor.subject, property_name="因果关系", value_text=value, relation_kind="causal", source_type="text_narrative_causal", confidence=0.80, clause=clause, rule="anchored_causal_statement", owner_source="confirmed_block_anchor")


def _method_step_index(match: re.Match[str], fallback: int) -> int:
    for group in ("n1", "n2"):
        value = match.groupdict().get(group)
        if value:
            return int(value)
    cn = match.groupdict().get("cn") or ""
    if cn in _CN_NUM:
        return _CN_NUM[cn]
    if cn == "十一":
        return 11
    if cn == "十二":
        return 12
    return fallback


def _compile_method_steps(
    blocks: Sequence[TextBlock], anchors_by_block: Mapping[str, TextSubjectAnchor],
    output: List[TextFactRecord], audits: List[NarrativeRelationAudit], seen: set[tuple[str, str, str, str]],
) -> None:
    groups: Dict[Tuple[str, ...], List[Tuple[TextBlock, re.Match[str]]]] = {}
    for block in blocks:
        heading = strip_heading_number(block.heading_title or "").strip()
        context = " > ".join(block.heading_path)
        if block.role != "method_model" and not _METHOD_HEADING_RE.search(heading):
            continue
        if not re.search(r"(?:方法|算法|流程|程序|计算|求解|迭代)", context):
            continue
        match = _METHOD_STEP_MARKER_RE.search(block.text or "")
        if not match:
            continue
        clause = _trim_value((block.text or "")[match.end():])
        if len(clause) < 4 or not _METHOD_ACTION_RE.search(clause):
            continue
        groups.setdefault(tuple(block.heading_path), []).append((block, match))

    for heading_path, items in groups.items():
        if len(items) < 2:
            continue
        items.sort(key=lambda item: item[0].line_start)
        heading_text = " > ".join(heading_path)
        strong_process_heading = bool(re.search(r"(?:算法|流程|程序|计算方法|求解方法|迭代方法|操作步骤|工作步骤)", heading_text))
        start_action_re = re.compile(r"^(?:建立|假定|列出|求解|计算|检验|判断|修正|比较|迭代|输入|输出|选择|确定|设置|更新|代入|展开|构造|调用|读取|保存|分析|评价|测定|测试|对|若|将|用|采用|根据)")
        action_starts = 0
        for block, marker in items:
            candidate_clause = _trim_value((block.text or "")[marker.end():])
            action_starts += int(bool(start_action_re.search(candidate_clause)))
        if not strong_process_heading and action_starts / max(1, len(items)) < 0.7:
            continue
        method_name = strip_heading_number(items[0][0].heading_title or "").strip() or (heading_path[-1] if heading_path else "计算方法")
        method_name = re.sub(r"^[（(]?[0-9一二三四五六七八九十]+[）)、.．]?\s*", "", method_name).strip()
        if len(method_name) > 80 or not re.search(r"(?:方法|算法|流程|程序|计算|求解|迭代)", method_name):
            continue
        process_id = "proc:method:" + hashlib.sha1((" > ".join(heading_path) + method_name).encode("utf-8")).hexdigest()[:18]
        temporary: List[TextFactRecord] = []
        for ordinal, (block, marker) in enumerate(items, start=1):
            clause = _trim_value((block.text or "")[marker.end():])
            step_index = _method_step_index(marker, ordinal)
            anchor = anchors_by_block.get(block.block_id)
            if anchor is None:
                continue
            subject_type, _, _ = infer_subject_type(method_name)
            digest = hashlib.sha1(f"{process_id}|{step_index}|{clause}".encode("utf-8")).hexdigest()[:20]
            action_match = _METHOD_ACTION_RE.search(clause)
            record = TextFactRecord(
                record_id=f"txtmethod:{digest}", block_id=block.block_id, subject=method_name,
                subject_type="方法/模型" if subject_type == "其他实体" else subject_type,
                property_name="方法步骤", value_text=clause, normalized_value_text=clause,
                relation_kind="method_step", source_type="text_method_step", heading_path=list(block.heading_path),
                line_start=block.line_start, line_end=block.line_end, evidence=block.text, confidence=0.90,
                record_status="ready", process_id=process_id, process_name=method_name,
                process_type="计算/方法步骤", step_id="step:method:" + hashlib.sha1(f"{process_id}|{step_index}".encode("utf-8")).hexdigest()[:18],
                step_index=step_index, step_label=marker.group("label"), step_action=(action_match.group(0) if action_match else "执行"),
                step_object=clause, anchor_source="structured_method_heading", fact_clause=clause,
                owner_evidence=method_name, owner_source="structured_method_heading",
            )
            key = (record.subject, record.property_name, record.value_text, record.block_id)
            if key in seen:
                continue
            seen.add(key)
            temporary.append(record)
            audits.append(NarrativeRelationAudit(
                record_id=record.record_id, block_id=block.block_id, role=block.role,
                relation_kind=record.relation_kind, subject=record.subject,
                property_name=record.property_name, value_text=record.value_text,
                rule="cross_block_numbered_method_step", confidence=record.confidence,
                evidence=block.text,
            ))
        temporary.sort(key=lambda r: (r.step_index or 0, r.line_start))
        for index, record in enumerate(temporary):
            record.previous_step_id = temporary[index - 1].step_id if index else ""
            record.next_step_id = temporary[index + 1].step_id if index + 1 < len(temporary) else ""
        output.extend(temporary)


def compile_narrative_relations(
    blocks: Sequence[TextBlock], anchors_by_block: Mapping[str, TextSubjectAnchor],
) -> Tuple[List[TextFactRecord], List[NarrativeRelationAudit]]:
    records: List[TextFactRecord] = []
    audits: List[NarrativeRelationAudit] = []
    seen: set[tuple[str, str, str, str]] = set()
    for block in blocks:
        anchor = anchors_by_block.get(block.block_id)
        if anchor is None or anchor.status == "unresolved":
            continue
        if _looks_like_toc_block(block.text or ""):
            continue
        _compile_clause_relations(block, anchor, records, audits, seen)
    _compile_method_steps(blocks, anchors_by_block, records, audits, seen)
    return records, audits


def write_narrative_relation_audit(output_dir: Path, audits: Sequence[NarrativeRelationAudit]) -> Dict[str, object]:
    audit_dir = output_dir / "step_narrative_relations"
    audit_dir.mkdir(parents=True, exist_ok=True)
    path = audit_dir / "narrative_relation_candidates.tsv"
    fields = ["record_id", "block_id", "role", "relation_kind", "subject", "property_name", "value_text", "rule", "confidence", "evidence"]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        for audit in audits:
            writer.writerow(audit.__dict__)
    counts: Dict[str, int] = {}
    for audit in audits:
        counts[audit.relation_kind] = counts.get(audit.relation_kind, 0) + 1
    return {
        "ok": True,
        "stage": "narrative_relation_compiler_v2_phase103",
        "candidate_relations": len(audits),
        "relation_kind_counts": counts,
        "audit_file": str(path),
        "policy": "comma-local explicit-owner relations; confirmed-anchor fallback; strict final gates remain active",
    }


__all__ = ["NarrativeRelationAudit", "compile_narrative_relations", "write_narrative_relation_audit"]
