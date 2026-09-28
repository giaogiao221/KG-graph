from __future__ import annotations

"""High-recall narrative relation candidates for phase 105.

The strict phase-103 compilers remain unchanged.  This module emits additional,
lower-confidence, evidence-preserving candidates for the generalized release
layer.  Every rule is clause-local; no OCR correction or unsupported entity is
invented.  The normal text gate and the phase-105 generalized gate still decide
whether a candidate is released.
"""

import csv
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Mapping, Sequence

from book_engine.document.block_segmenter import TextBlock
from book_engine.routing.text_subject_anchor_resolver import TextSubjectAnchor, is_plausible_subject_name
from book_engine.text.evidence_self_containment import iter_clauses, is_deictic_subject
from book_engine.text.text_fact_extractor import (
    TextFactRecord,
    _clean_owner_candidate,
    _is_explicit_entity,
    _looks_like_toc_page_line,
    _make_record,
)

_OWNER = r"[^。；;，,:：]{2,72}"
_VALUE = r"[^。；;]{2,240}"

_BELONGS_RE = re.compile(rf"(?P<owner>{_OWNER})\s*(?:均|都|也)?属于\s*(?P<value>{_VALUE})")
_FEATURE_RE = re.compile(rf"(?P<owner>{_OWNER})\s*(?:通常|一般|主要|还|均|都|也)?(?:具有|具备|表现出|呈现出)\s*(?P<value>{_VALUE})")
_TRAIT_RE = re.compile(rf"(?P<owner>{_OWNER})\s*(?:的)?(?P<label>优点|缺点|特点|特征|优势|不足|主要性能|主要性质)\s*(?:是|为|在于|包括|表现为|体现在)\s*(?P<value>{_VALUE})")
_DEPENDS_RE = re.compile(rf"(?P<owner>{_OWNER})\s*(?:主要|很大程度上|基本)?(?:取决于|决定于|依赖于)\s*(?P<value>{_VALUE})")
_AFFECTED_BY_RE = re.compile(rf"(?P<owner>{_OWNER})\s*(?:主要|显著|明显|很大程度上)?(?:受|受到)\s*(?P<value>[^。；;]{{2,160}}?)\s*(?:的)?影响")
_RELATED_RE = re.compile(rf"(?P<owner>{_OWNER})\s*(?:与|和)\s*(?P<target>{_OWNER})\s*(?:密切|直接|显著|明显)?(?:有关|相关|存在(?:着)?(?:密切|直接|一定)?关系|成正比|成反比)")
_CONTAINS_RE = re.compile(rf"(?P<owner>{_OWNER})\s*(?:中)?(?:主要|通常|一般)?(?:含有|包含(?!于)|所含|(?<!包)含(?!能|量|于))\s*(?P<value>{_VALUE})")
_NOUN_DEFINITION_RE = re.compile(rf"(?P<owner>{_OWNER})\s*(?:是|为)\s*(?:一种|一类|一个|由)?(?P<value>[^。；;]{{2,120}})")
_RESULT_SIGNAL_RE = re.compile(r"(?:研究|试验|实验|结果|计算结果|分析结果|数据)(?:还|也|均|都)?(?:表明|说明|证明|显示|发现)\s*[，,:：]?\s*(?P<value>[^。；;]{4,240})")
_REASON_SIGNAL_RE = re.compile(r"(?P<value>[^。；;]{3,260}?(?:原因(?:是|在于)|这是由于|主要由于|其原因在于)[^。；;]{3,180})")
_CHANGE_RE = re.compile(r"(?P<value>(?:随着|随)[^。；;，,]{2,100}(?:增加|增大|升高|提高|降低|减小|下降|变化)[，,][^。；;]{3,180})")

_NUMERIC_PROPERTY = (
    "密度|熔点|沸点|闪点|自燃点|爆发点|分解温度|峰顶温度|玻璃化温度|爆速|爆压|爆热|燃速|比冲|"
    "氧平衡|生成热|生成焓|分子量|相对分子质量|纯度|含量|粒径|粒度|黏度|粘度|感度|压力指数|"
    "释能时间|释能功率|温度|压力|时间|得率|产率"
)
_PARALLEL_NUMERIC_RE = re.compile(
    rf"(?P<owners>[A-Za-z0-9+._/()（）\-\u4e00-\u9fff、，,和与及]{{2,180}}?)\s*(?:的)?\s*"
    rf"(?P<property>{_NUMERIC_PROPERTY})\s*(?:分别为|依次为|分别是|依次是)\s*(?P<values>[^。；;]{{3,220}})",
    re.I,
)

_CODE_RE = re.compile(r"^[A-Z][A-Z0-9+._/\-()]{1,28}$", re.I)
_MATERIAL_END_RE = re.compile(
    r"(?:材料|物质|化合物|金属|粉|酸|盐|酯|醚|胺|醇|酮|聚合物|预聚物|共聚物|均聚物|"
    r"橡胶|树脂|粘合剂|黏合剂|火药|推进剂|炸药|药剂|体系|配方|晶体|结晶|装置|系统|元件|部件)$"
)
_DEFINITION_END_RE = re.compile(
    r"(?:材料|物质|化合物|聚合物|共聚物|预聚物|火药|推进剂|炸药|药剂|体系|配方|方法|模型|"
    r"理论|过程|现象|结构|装置|系统|组分|氧化剂|还原剂|粘合剂|黏合剂|增塑剂|催化剂|稳定剂|"
    r"性能|性质|能力|状态|形式|类型|类别)$"
)
_BAD_OWNER_RE = re.compile(
    r"^(?:因此|所以|此外|同时|其中|由于|因为|如果|当|在|随着|研究|结果|试验|实验|文献|资料|"
    r"这种|这类|该|其|它|上述|所得|最终|可以|能够|必须|需要|通过|采用|使用|利用|为了|从而)"
)
_BAD_VALUE_RE = re.compile(r"^(?:图|表)\s*\d|(?:见|参见)(?:图|表)|^如下$|^如图|^如表")
_RELATION_OWNER_BAD_START_RE = re.compile(
    r"^(?:并|且|而|则|也|又|还|仅|不|因而|从而|实际上|实际不仅|这是|这可|可见|由此|"
    r"只要|为了|例如|如在|对于|对|将|使|添加|采用|利用|使用|评估|研究|分析|测定|人们|下文|特别|许多|一个|"
    r"表征|说明|表明|发现|强调|要求|需要|必须|能够|可以|有助于|可用于|可看到|所列|所用)"
)
_RELATION_OWNER_VERBAL_RE = re.compile(
    r"(?:能使|使其|可使|用于|用来|要求|需要|必须|应该|应当|可以|能够|有助于|"
    r"表明|说明|发现|认为|强调|测定|评价|评估|研究了|分析了|采用|利用|使用|添加|"
    r"生产|制备|合成|得到|获得|形成|发生|进行|表现|指出|列出|包含于|归因于|处理|讨论|提高|组成了|可认为|可认|可分|可设计|称之|给出|拟叙述)"
)
_RELATION_OWNER_BAD_END_RE = re.compile(
    r"(?:的|地|得|为|是|在|中|上|下|时|后|前|因|由|将|与|和|及|或|也|则|能|可|需|"
    r"要求|采用|利用|使用|添加|研究|分析|测定|评价|评估|形成|得到|获得|进行|表现|说明|表明|处理|讨论|提高|目的|的目)$"
)
_RELATION_CLASS_END_RE = re.compile(
    r"(?:材料|物质|化合物|炸药|火药|推进剂|药剂|组分|氧化剂|还原剂|燃料|粘结剂|黏结剂|"
    r"增塑剂|催化剂|稳定剂|方法|模型|过程|现象|结构|装置|系统|类型|类别|晶型|聚合物|"
    r"共聚物|预聚物|硝胺类|硝酸酯类)$"
)
_LIST_SPLIT_RE = re.compile(r"\s*(?:、|，|,|;|；|和|与|及)\s*")
_VALUE_SPLIT_RE = re.compile(r"\s*(?:、|，|,|;|；|和|及)\s*")
_UNIT_TAIL_RE = re.compile(
    r"(?P<unit>%|‰|ppm|ppb|vol%|wt%|℃|°C|K|Pa|kPa|MPa|GPa|bar|g(?:·|\*)?cm(?:\^?[-−]?3|³)|"
    r"kg(?:·|\*)?m(?:\^?[-−]?3|³)|nm|μm|um|mm|cm|m|ns|μs|us|ms|s|min|h|d|Hz|rpm|"
    r"mm(?:·|\*)?s(?:\^?[-−]?1)|cm(?:·|\*)?s(?:\^?[-−]?1)|m(?:·|\*)?s(?:\^?[-−]?1)|"
    r"km(?:·|\*)?s(?:\^?[-−]?1)|J/g|kJ/kg|kJ/mol|N(?:·|\*)?s(?:·|/)?kg(?:\^?[-−]?1)?)\s*$",
    re.I,
)
_NUMERIC_VALUE_RE = re.compile(
    r"^(?:约|近|大约|不小于|不大于|不少于|不超过|≥|≤|>|<)?\s*[-+−]?\d+(?:[.,]\d+)?"
    r"(?:\s*(?:~|～|—|–|至|到|±)\s*[-+−]?\d+(?:[.,]\d+)?)?\s*[^\u4e00-\u9fff]{0,28}$"
)

_ALLOWED_ROLES = {
    "material_profile", "formulation", "process", "experiment", "comparison", "application_safety",
    "method_model", "device_system", "theory", "property", "unknown",
}


@dataclass(frozen=True)
class GeneralizedRelationAudit:
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


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip(" \t，,。；;：:")


def _plausible_owner(raw: str, *, strong: bool = False) -> str:
    owner = _clean_owner_candidate(raw)
    owner = re.sub(
        r"^(?:所谓|成熟的|典型的|常用的|一般的|通常的|主要的|众所周知，?|研究表明，?|结果表明，?|"
        r"实验表明，?|试验表明，?|由此可知，?|可以看出，?|这是因为|这说明|这表明)",
        "", owner,
    ).strip()
    # When a discourse segment remains, keep the nearest noun phrase before the
    # relation trigger rather than the whole sentence prefix.
    owner = re.split(r"(?:因为|所以|因此|此外|同时|但是|然而|而且|进而|从而|其中|即)", owner)[-1].strip()
    owner = re.sub(r"(?:的)?(?:一个|一种|一类|独特|最大|主要|重要|显著|明显|完全|高度|基本)$", "", owner).strip()
    if not owner or is_deictic_subject(owner) or _BAD_OWNER_RE.search(owner):
        return ""
    if len(owner) > 48 or re.search(r"[。；;：:?？]", owner):
        return ""
    if _RELATION_OWNER_BAD_START_RE.search(owner) or _RELATION_OWNER_BAD_END_RE.search(owner):
        return ""
    if _RELATION_OWNER_VERBAL_RE.search(owner):
        return ""
    if re.search(r"(?:图|表)\s*\d", owner) or re.search(r"\\(?:mathrm|text|frac|alpha|beta)", owner):
        return ""
    explicit = bool(_is_explicit_entity(owner) or _CODE_RE.fullmatch(owner) or _MATERIAL_END_RE.search(owner))
    if strong and not explicit:
        if len(owner) > 20 or not is_plausible_subject_name(owner):
            return ""
        # A strong generalized subject should be noun-like and not a bare
        # connective/adjective fragment.
        if re.fullmatch(r"(?:最初|首先|其次|最后|重要|主要|一般|通常|某些|一些|其他|前者|后者)", owner):
            return ""
    if explicit:
        return owner
    return owner if is_plausible_subject_name(owner) and len(owner) <= 24 else ""


def _safe_anchor(block: TextBlock, anchor: TextSubjectAnchor | None) -> bool:
    return bool(
        anchor is not None
        and anchor.status == "confirmed"
        and anchor.subject
        and not is_deictic_subject(anchor.subject)
        and is_plausible_subject_name(anchor.subject)
        and block.role in _ALLOWED_ROLES
    )


def _split_owners(text: str) -> List[str]:
    surface = re.sub(r"^(?:对于|其中|例如|如)", "", _clean(text))
    parts = [item for item in _LIST_SPLIT_RE.split(surface) if item]
    output: List[str] = []
    for item in parts:
        owner = _plausible_owner(item)
        if not owner:
            return []
        output.append(owner)
    return output if 2 <= len(output) <= 12 and len(set(output)) == len(output) else []


def _split_values(text: str, expected: int) -> List[str]:
    surface = _clean(text)
    # Remove a trailing explanatory parenthesis only when it follows all values.
    surface = re.sub(r"\s*[（(](?:实验值|理论值|计算值|文献值|约值)[）)]\s*$", "", surface)
    parts = [item.strip() for item in _VALUE_SPLIT_RE.split(surface) if item.strip()]
    if len(parts) != expected or not all(_NUMERIC_VALUE_RE.fullmatch(item) for item in parts):
        return []
    final_unit = ""
    match = _UNIT_TAIL_RE.search(parts[-1])
    if match:
        final_unit = match.group("unit")
    if final_unit:
        for index, item in enumerate(parts[:-1]):
            if not _UNIT_TAIL_RE.search(item):
                parts[index] = f"{item} {final_unit}".strip()
    return parts


def _append(
    records: List[TextFactRecord], audits: List[GeneralizedRelationAudit], seen: set[tuple[str, str, str, str]],
    block: TextBlock, anchor: TextSubjectAnchor, *, subject: str, property_name: str, value_text: str,
    relation_kind: str, source_type: str, confidence: float, clause: str, rule: str,
) -> None:
    subject = _clean(subject)
    value_text = _clean(value_text)
    if not subject or not value_text or _BAD_VALUE_RE.search(value_text):
        return
    key = (subject, property_name, value_text, block.block_id)
    if key in seen:
        return
    record = _make_record(
        block,
        anchor,
        property_name,
        value_text,
        source_type=source_type,
        confidence=confidence,
        relation_kind=relation_kind,
        subject_override=subject,
        fact_clause=clause,
        owner_evidence=clause,
        owner_source="explicit_generalized_relation_owner" if subject != anchor.subject else "confirmed_block_anchor",
    )
    # Keep phase-105 additions out of the strict phase-103 release stream.  They
    # are deliberately assessed by the generalized gate instead.
    if record.record_status == "ready":
        record.record_status = "candidate"
        record.unresolved_reasons = sorted(set(record.unresolved_reasons + ["phase105_generalized_candidate"]))
    seen.add(key)
    records.append(record)
    audits.append(
        GeneralizedRelationAudit(
            record_id=record.record_id,
            block_id=block.block_id,
            role=block.role,
            relation_kind=relation_kind,
            subject=record.subject,
            property_name=record.property_name,
            value_text=record.value_text,
            rule=rule,
            confidence=record.confidence,
            evidence=block.text,
        )
    )


def compile_generalized_relations(
    blocks: Sequence[TextBlock], anchors_by_block: Mapping[str, TextSubjectAnchor]
) -> tuple[List[TextFactRecord], List[GeneralizedRelationAudit]]:
    records: List[TextFactRecord] = []
    audits: List[GeneralizedRelationAudit] = []
    seen: set[tuple[str, str, str, str]] = set()

    for block in blocks:
        anchor = anchors_by_block.get(block.block_id)
        if anchor is None or _looks_like_toc_page_line(block.text):
            continue
        anchor_ok = _safe_anchor(block, anchor)
        for _, _, raw_clause in iter_clauses(block.text):
            clause = _clean(raw_clause)
            if not (5 <= len(clause) <= 700) or _looks_like_toc_page_line(clause):
                continue

            parallel = _PARALLEL_NUMERIC_RE.search(clause)
            if parallel:
                owners = _split_owners(parallel.group("owners"))
                values = _split_values(parallel.group("values"), len(owners)) if owners else []
                if owners and len(values) == len(owners):
                    for owner, value in zip(owners, values):
                        _append(
                            records, audits, seen, block, anchor,
                            subject=owner,
                            property_name=parallel.group("property"),
                            value_text=value,
                            relation_kind="attribute",
                            source_type="text_generalized_parallel_numeric",
                            confidence=0.69,
                            clause=clause,
                            rule="count_aligned_parallel_numeric",
                        )

            for match in _BELONGS_RE.finditer(clause):
                owner = _plausible_owner(match.group("owner"), strong=True)
                value = _clean(match.group("value"))
                value = re.split(r"(?:，|,|因为|由于|其中)", value)[0].strip()
                if owner and _RELATION_CLASS_END_RE.search(value):
                    _append(records, audits, seen, block, anchor, subject=owner, property_name="分类", value_text=value,
                            relation_kind="classification", source_type="text_generalized_classification", confidence=0.66,
                            clause=clause, rule="explicit_belongs_to")

            for match in _DEPENDS_RE.finditer(clause):
                owner = _plausible_owner(match.group("owner"), strong=True)
                if owner:
                    _append(records, audits, seen, block, anchor, subject=owner, property_name="影响因素",
                            value_text=f"取决于{_clean(match.group('value'))}", relation_kind="causal",
                            source_type="text_generalized_dependence", confidence=0.66, clause=clause,
                            rule="explicit_depends_on")

            for match in _AFFECTED_BY_RE.finditer(clause):
                owner = _plausible_owner(match.group("owner"), strong=True)
                if owner:
                    _append(records, audits, seen, block, anchor, subject=owner, property_name="影响因素",
                            value_text=f"受{_clean(match.group('value'))}影响", relation_kind="effect",
                            source_type="text_generalized_dependence", confidence=0.65, clause=clause,
                            rule="explicit_affected_by")

            for match in _RELATED_RE.finditer(clause):
                owner = _plausible_owner(match.group("owner"), strong=True)
                target = _plausible_owner(match.group("target"), strong=True)
                relation_surface = _clean(match.group(0))
                if owner and target and owner != target and not re.search(r"(?:不应认为|不能认为|并非)", clause):
                    relation_kind = "comparison" if re.search(r"正比|反比", relation_surface) else "connection"
                    _append(records, audits, seen, block, anchor, subject=owner, property_name="关联关系",
                            value_text=relation_surface[len(match.group("owner")):].strip(), relation_kind=relation_kind,
                            source_type="text_generalized_association", confidence=0.64, clause=clause,
                            rule="explicit_association")

            for match in _TRAIT_RE.finditer(clause):
                owner = _plausible_owner(match.group("owner"), strong=True)
                value = _clean(match.group("value"))
                if owner and len(value) <= 220 and not clause.endswith(("?", "？")):
                    label = match.group("label")
                    property_name = "特点" if label in {"特点", "特征"} else label
                    _append(records, audits, seen, block, anchor, subject=owner, property_name=property_name,
                            value_text=value, relation_kind="function", source_type="text_generalized_trait",
                            confidence=0.66, clause=clause, rule="explicit_trait_statement")

            for match in _FEATURE_RE.finditer(clause):
                owner = _plausible_owner(match.group("owner"), strong=True)
                value = _clean(match.group("value"))
                if owner and 2 <= len(value) <= 180 and not clause.endswith(("?", "？")) and not re.search(r"^(?:了|着|过|如下|图|表)", value):
                    _append(records, audits, seen, block, anchor, subject=owner, property_name="特性",
                            value_text=value, relation_kind="function", source_type="text_generalized_trait",
                            confidence=0.62, clause=clause, rule="explicit_has_feature")

            for match in _CONTAINS_RE.finditer(clause):
                owner = _plausible_owner(match.group("owner"), strong=True)
                value = re.sub(r"^的", "", _clean(match.group("value"))).strip()
                if owner and 2 <= len(value) <= 160 and not re.search(r"(?:常用于|可用于|用来|需要|必须|有助于|从而|进而)", value):
                    _append(records, audits, seen, block, anchor, subject=owner, property_name="组成描述",
                            value_text=f"含有{value}", relation_kind="composition",
                            source_type="text_generalized_composition", confidence=0.64, clause=clause,
                            rule="explicit_contains")

            # Broad copular definitions are deliberately narrow: the tail must
            # end in a concept/entity class and neither side may look verbal.
            for match in _NOUN_DEFINITION_RE.finditer(clause):
                owner = _plausible_owner(match.group("owner"), strong=True)
                value = _clean(match.group("value"))
                if owner and 2 <= len(value) <= 100 and _DEFINITION_END_RE.search(value):
                    if not re.search(r"(?:可以|能够|需要|必须|用于|导致|提高|降低|增加|减少|影响)", value):
                        _append(records, audits, seen, block, anchor, subject=owner, property_name="定义",
                                value_text=value, relation_kind="definition", source_type="text_generalized_definition",
                                confidence=0.61, clause=clause, rule="bounded_copular_definition")

            if anchor_ok and block.role in {"theory", "experiment", "comparison", "property", "material_profile"}:
                match = _RESULT_SIGNAL_RE.search(clause)
                if match:
                    _append(records, audits, seen, block, anchor, subject=anchor.subject, property_name="研究结论",
                            value_text=_clean(match.group("value")), relation_kind="experiment_result",
                            source_type="text_generalized_conclusion", confidence=0.60, clause=clause,
                            rule="anchored_result_signal")
                match = _REASON_SIGNAL_RE.search(clause)
                if match:
                    _append(records, audits, seen, block, anchor, subject=anchor.subject, property_name="因果关系",
                            value_text=_clean(match.group("value")), relation_kind="causal",
                            source_type="text_generalized_causal", confidence=0.61, clause=clause,
                            rule="anchored_reason_statement")
                match = _CHANGE_RE.search(clause)
                if match:
                    _append(records, audits, seen, block, anchor, subject=anchor.subject, property_name="变化规律",
                            value_text=_clean(match.group("value")), relation_kind="effect",
                            source_type="text_generalized_change", confidence=0.60, clause=clause,
                            rule="anchored_change_relation")

    return records, audits


def write_generalized_relation_audit(output_dir: Path, audits: Sequence[GeneralizedRelationAudit]) -> dict[str, object]:
    audit_dir = output_dir / "step_generalized_relations"
    audit_dir.mkdir(parents=True, exist_ok=True)
    path = audit_dir / "generalized_relation_candidates.tsv"
    fields = [
        "record_id", "block_id", "role", "relation_kind", "subject", "property_name", "value_text",
        "rule", "confidence", "evidence",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for item in audits:
            writer.writerow({name: getattr(item, name) for name in fields})
    return {
        "ok": True,
        "stage": "phase105_generalized_relation_compiler",
        "candidate_relations": len(audits),
        "path": str(path),
        "rule_counts": {
            rule: sum(1 for item in audits if item.rule == rule)
            for rule in sorted({item.rule for item in audits})
        },
    }


__all__ = [
    "GeneralizedRelationAudit",
    "compile_generalized_relations",
    "write_generalized_relation_audit",
]
