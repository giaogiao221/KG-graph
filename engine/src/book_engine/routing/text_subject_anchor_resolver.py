from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

from book_engine.document.block_segmenter import TextBlock
from book_engine.routing.heading_subject_resolver import (
    build_subject_lexicon,
    detect_local_subject_evidence,
    heading_path_candidates,
    nearest_scope_from_candidates,
    registry_subject_names,
    select_local_owner,
    strip_heading_number,
)
from book_engine.routing.subject_anchor_llm_adjudicator import SubjectAnchorLLMAdjudicator
from book_engine.routing.subject_name_normalizer import normalize_subject_name
from book_engine.routing.subject_registry_builder import SubjectRegistryEntry
from book_engine.routing.subject_type_inferer import infer_subject_type

_IDENTITY_LABELS = r"中文名称|英文名称|中文别称|英文别称|CAS(?:登记号|号)?|分子式|化学式|分子量|相对分子质量"
_IDENTITY_NAME_RE = re.compile(
    rf"(?:^|\n)\s*(?:中文名称|名称)\s*[：:]\s*(?P<value>.*?)(?=\s*(?:{_IDENTITY_LABELS})\s*[：:]|$)",
    re.S,
)
_NUMBER_PREFIX_RE = re.compile(r"^\s*(?:第\s*\d+\s*章\s*)?(?:\d+(?:\s*[\.．]\s*\d+){0,5}\s*)")
_GENERIC_TITLE_RE = re.compile(
    r"^(?:概述|导言|绪论|引论|原理|机理|理论|定义|概念|分类|特点|要求|性能|性质|物理性质|化学性质|"
    r"制备方法|合成方法|测试方法|分析方法|应用|用途|安全|储存运输|毒性与防护|参考文献|小结|结论)$"
)
_PRONOUN_RE = re.compile(r"(?:该材料|这种材料|上述材料|该推进剂|这种推进剂|上述推进剂|该炸药|本品|该物质|其(?:密度|性能|燃速|爆速|组成|制备))")
_PROCESS_SUBJECT_RE = re.compile(r"(?:(?:报道了|研究了|介绍了)[^。；]{0,16}?)?(?:以|对)\s*(?P<subject>[A-Za-z0-9＋+－\-/.（）()\u4e00-\u9fff]{2,80}?)\s*(?:的)?\s*(?:制备方法|合成方法|制备工艺|生产工艺|加工工艺|包覆方法|装配方法)(?:和性能)?")
_PROCESS_FINAL_PRODUCT_RE = re.compile(
    r"(?:得到|获得|制得|制备出|生成|产得)(?:了)?(?:目标物|最终产物|最终产品|成品)?\s*"
    r"(?P<subject>[A-Za-z0-9＋+－\-/.（）()\u4e00-\u9fff]{2,60}?)(?=[，,。；;]|$)"
)
_PROCESS_TERSE_PRODUCT_RE = re.compile(
    r"(?:浓缩|蒸发|分离|过滤|干燥|冷却|结晶|提取|萃取|合成|反应|处理)\s*得(?:到)?(?:了)?\s*"
    r"(?P<subject>[A-Za-z0-9＋+－\-/.（）()\u4e00-\u9fff]{2,60}?)(?=[，,。；;]|$)"
)
_PROCESS_STANDALONE_PRODUCT_RE = re.compile(
    r"(?:^|[，,。；;\s])(?:即|可)?得(?:到)?(?:了)?\s*"
    r"(?P<subject>[A-Za-z0-9＋+－\-/.（）()\u4e00-\u9fff]{2,60}?)(?=[，,。；;]|$)"
)
_PROCESS_OUTPUT_ENTITY_RE = re.compile(
    r"(?:以此工艺|采用该工艺|该工艺|本工艺|该方法|本方法|此法)(?:可|能|用于)?\s*"
    r"(?:生产|制备|合成|制得)(?:的|出)?\s*"
    r"(?P<subject>[A-Za-z0-9＋+－\-/.（）()\u4e00-\u9fff]{2,60}?)(?=(?:的典型|的形貌|见图|[，,。；;]|$))"
)
_PROCESS_LOCAL_DEVICE_RE = re.compile(
    r"^\s*(?P<subject>[A-Za-z0-9＋+－\-/.（）()\u4e00-\u9fff]{2,50}?(?:加强帽|管壳|雷管|火帽|药柱|药块|药筒|装置|组件|部件))(?=(?:长|短|厚|薄|需|必须|应|采用|先|在|由|，|,|。))"
)
_PROCESS_GENERIC_PRODUCTS = {"溶液", "沉淀", "凝胶", "混合物", "产物", "产品", "成品", "样品", "目标物"}
_INVALID_SUBJECT_SEMANTIC_RE = re.compile(
    r"(?:注意的是|值得注意|取决于|结果表明|研究表明|可以看出|由此可见|用于|可为|必须|通常比|"
    r"速度|时间|晶核量(?:多|少)?|的影响|的比较|的关系|的确定|的研究|的分析|的讨论|"
    r"基本原理|反应机理|工作流程|系统流程|算法流程|推理流程)$"
)
_DEICTIC_ONLY_SUBJECT_RE = re.compile(
    r"^(?:它|其|该|此|上述|前述|本|这些|那些)(?:系统|装置|材料|物质|产品|产物|聚合物|"
    r"推进剂|火药|炸药|组件|设备|结构|方法|体系|样品|试样)?$"
)
_GENERIC_PRODUCT_RE = re.compile(r"^(?:(?:微胶囊化|最终|目标|所得|粒状|中间|反应)?)?(?:产品|产物|成品|样品|物质|材料)$")
_TOPIC_TITLE_RE = re.compile(
    r"(?:设计.+时|(?:的|及|与).*(?:确定|选择|比较|影响|关系|研究|分析|讨论|计算|优化)|"
    r"(?:原理|机理|理论|方法|过程|流程|规律|变化|趋势|结果|性能|性质|条件|因素))$"
)
_DYNAMIC_SUBJECT_PHRASE_RE = re.compile(
    r"^(?:得到的|所得的|合成出的|制得的|得到|获得|这些|上述|最简单并|则|因此|其中|其)"
    r"|(?:中|下|时)$|(?:可以|能够|必须|通常|一般|即为|用于|得到|获得|合成|制备|储存|应用)"
)
_METHOD_SUBJECT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._/+\-]{0,30}(?:方法|模型|公式|定律|算法|准则|法)$", re.I)


@dataclass
class TextSubjectAnchor:
    block_id: str
    subject: str
    subject_type: str
    source: str
    confidence: float
    status: str
    reasons: List[str] = field(default_factory=list)


@dataclass
class SubjectAnchorAudit:
    block_id: str
    candidate_subject_ids: List[str]
    candidate_names: List[str]
    deterministic_source: str
    final_subject: str
    final_status: str
    llm_used: bool = False
    llm_status: str = ""
    llm_confidence: float = 0.0
    llm_evidence_span: str = ""
    llm_reason: str = ""


def _clean_heading_subject(title: str) -> str:
    return strip_heading_number(title)


def _entry_names(entry: SubjectRegistryEntry) -> List[str]:
    return [entry.canonical_name] + list(entry.aliases)


def _text_mentions(text: str, entries: Sequence[SubjectRegistryEntry]) -> List[SubjectRegistryEntry]:
    found: List[SubjectRegistryEntry] = []
    for entry in entries:
        if entry.status != "confirmed" or not entry.canonical_name:
            continue
        for name in _entry_names(entry):
            if not name:
                continue
            if re.fullmatch(r"[A-Za-z0-9+._/-]+", name):
                hit = re.search(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", text, re.I)
            else:
                hit = name in text
            if hit:
                found.append(entry)
                break
    return list({item.subject_id: item for item in found}.values())


def _candidate_payload(entry: SubjectRegistryEntry, source: str) -> Dict[str, object]:
    return {
        "subject_id": entry.subject_id,
        "name": entry.canonical_name,
        "subject_type": entry.subject_type,
        "source": source,
        "registry_score": entry.score,
        "aliases": entry.aliases,
    }


def _llm_choice(
    adjudicator: SubjectAnchorLLMAdjudicator,
    block: TextBlock,
    candidates: Sequence[SubjectRegistryEntry],
    previous: TextSubjectAnchor | None,
) -> Tuple[TextSubjectAnchor | None, SubjectAnchorAudit]:
    payloads = [_candidate_payload(entry, "direct_or_context_candidate") for entry in candidates]
    result = adjudicator.adjudicate(
        block_id=block.block_id,
        heading_path=block.heading_path,
        text=block.text,
        candidates=payloads,
        previous_subject_id=(previous.subject if previous else ""),
    )
    audit = SubjectAnchorAudit(
        block_id=block.block_id,
        candidate_subject_ids=[entry.subject_id for entry in candidates],
        candidate_names=[entry.canonical_name for entry in candidates],
        deterministic_source="ambiguous_candidates",
        final_subject="",
        final_status="unresolved",
        llm_used=result is not None,
        llm_status=(result.status if result else "not_available"),
        llm_confidence=(result.confidence if result else 0.0),
        llm_evidence_span=(result.evidence_span if result else ""),
        llm_reason=(result.reason if result else ""),
    )
    if result is None or result.selected_subject_id == "unresolved" or result.confidence < 0.72:
        return None, audit
    by_id = {entry.subject_id: entry for entry in candidates}
    entry = by_id.get(result.selected_subject_id)
    if entry is None:
        return None, audit
    anchor = TextSubjectAnchor(
        block_id=block.block_id,
        subject=entry.canonical_name,
        subject_type=entry.subject_type,
        source="closed_set_subject_llm",
        confidence=min(0.94, result.confidence),
        status="confirmed",
        reasons=["closed_set_candidate_selection", result.reason] if result.reason else ["closed_set_candidate_selection"],
    )
    audit.final_subject = anchor.subject
    audit.final_status = anchor.status
    return anchor, audit


def is_plausible_subject_name(value: str) -> bool:
    normalized = normalize_subject_name(value or "").canonical_name
    if not normalized or len(normalized) > 70:
        return False
    if re.fullmatch(r"[0-9一二三四五六七八九十.．()（）]+", normalized):
        return False
    if normalized.startswith("的") or _DEICTIC_ONLY_SUBJECT_RE.fullmatch(normalized) or _GENERIC_TITLE_RE.fullmatch(normalized):
        return False
    if normalized in _PROCESS_GENERIC_PRODUCTS or _GENERIC_PRODUCT_RE.fullmatch(normalized):
        return False
    if any(normalized.endswith(token) for token in ("溶液", "沉淀", "凝胶")):
        return False
    if _INVALID_SUBJECT_SEMANTIC_RE.search(normalized):
        return False
    if _METHOD_SUBJECT_RE.fullmatch(normalized) or _DYNAMIC_SUBJECT_PHRASE_RE.search(normalized):
        return False
    if re.search(r"[。；;：:]", normalized):
        return False
    # Long function-word-rich spans are sentence fragments, not entity names.
    if len(normalized) >= 14 and re.search(r"(?:的|了|为|是|在|中|由|可|并|即)", normalized):
        return False
    return True


def _normalize_process_product(raw: str) -> str:
    value = (raw or "").strip()
    value = re.sub(r"^(?:的|不同|各种|若干)", "", value).strip()
    value = re.sub(r"^(?:淡|深)?(?:蓝|黄|橘黄|红|黑|白|灰|棕|褐|绿|无)(?:色)?(?:的)?", "", value).strip()
    value = re.sub(r"^(?:目标物|最终产物|最终产品|成品)", "", value).strip()
    normalized = normalize_subject_name(value).canonical_name
    return normalized if is_plausible_subject_name(normalized) else ""


def _process_final_product_subject(text: str) -> str:
    candidates: List[Tuple[int, str]] = []
    for pattern in (
        _PROCESS_FINAL_PRODUCT_RE, _PROCESS_TERSE_PRODUCT_RE,
        _PROCESS_STANDALONE_PRODUCT_RE, _PROCESS_OUTPUT_ENTITY_RE,
    ):
        for match in pattern.finditer(text or ""):
            normalized = _normalize_process_product(match.group("subject"))
            if normalized:
                candidates.append((match.start(), normalized))
    candidates.sort(key=lambda item: item[0])
    return candidates[-1][1] if candidates else ""


def _process_local_entity_subject(block: TextBlock) -> str:
    heading_context = " > ".join(block.heading_path)
    if not re.search(r"(?:装配|生产工艺|工艺流程|装药|部件|组件)", heading_context):
        return ""
    match = _PROCESS_LOCAL_DEVICE_RE.search(block.text or "")
    if not match:
        return ""
    candidate = normalize_subject_name(match.group("subject")).canonical_name
    return candidate if is_plausible_subject_name(candidate) else ""


def _within_heading_scope(current_path: Tuple[str, ...], target_path: Sequence[str]) -> bool:
    if not current_path or not target_path or len(target_path) < len(current_path):
        return False
    return tuple(target_path[: len(current_path)]) == current_path


def resolve_text_subject_anchors_with_audit(
    blocks: Sequence[TextBlock],
    table_registry: Sequence[SubjectRegistryEntry],
) -> Tuple[List[TextSubjectAnchor], Dict[str, TextSubjectAnchor], List[SubjectAnchorAudit]]:
    anchors: List[TextSubjectAnchor] = []
    by_block: Dict[str, TextSubjectAnchor] = {}
    audits: List[SubjectAnchorAudit] = []
    current: TextSubjectAnchor | None = None
    current_chapter = ""
    current_heading_path: Tuple[str, ...] = ()
    model_root = Path(__file__).resolve().parents[2]
    adjudicator = SubjectAnchorLLMAdjudicator(
        enabled=os.getenv("KGCHOUQU_SUBJECT_LLM_ENABLED") == "1",
        cache_dir=model_root / "cache" / "subject_anchor_phase95",
    )
    entry_by_name = {entry.canonical_name: entry for entry in table_registry if entry.status == "confirmed"}
    registry_names = registry_subject_names(table_registry)
    lexicon = build_subject_lexicon((block.heading_path for block in blocks), registry_names)

    for block in blocks:
        chapter = block.heading_path[0] if block.heading_path else ""
        if current is not None and chapter and current_chapter and chapter != current_chapter:
            current = None
            current_heading_path = ()
        reasons: List[str] = []
        anchor: TextSubjectAnchor | None = None
        audit = SubjectAnchorAudit(block.block_id, [], [], "", "", "unresolved")

        path_candidates = heading_path_candidates(block.heading_path, registry_names)
        heading_candidate = next((item for item in reversed(path_candidates) if item.is_entity), None)
        heading_scope_names = nearest_scope_from_candidates(path_candidates)
        heading_subject = heading_candidate.entity if heading_candidate else ""
        local_lexicon = sorted(dict.fromkeys([*heading_scope_names, *lexicon]), key=len, reverse=True)
        local_evidence = detect_local_subject_evidence(
            block.text,
            local_lexicon,
            heading_subject=heading_subject,
        )
        local_owner = select_local_owner(local_evidence)
        local_owner_names = list(dict.fromkeys(
            item.subject for item in local_evidence
            if item.is_owner and item.confidence >= 0.82
        ))
        local_non_owner_names = list(dict.fromkeys(
            item.subject for item in local_evidence
            if not item.is_owner and item.role in {"comparison_reference", "condition_or_reagent"}
        ))
        explicit_owner_conflicts = list(dict.fromkeys(
            item.subject for item in local_evidence
            if item.role == "explicit_owner_conflict" and item.confidence >= 0.82
        ))
        if explicit_owner_conflicts:
            reasons.append("explicit_non_entity_owner_blocks_heading_fallback")

        identity_match = _IDENTITY_NAME_RE.search(block.text)
        if identity_match:
            subject = normalize_subject_name(identity_match.group("value").strip()).canonical_name
            if subject and is_plausible_subject_name(subject):
                subject_type, _, type_reasons = infer_subject_type(subject)
                anchor = TextSubjectAnchor(
                    block.block_id, subject, subject_type, "identity_field", 0.96,
                    "confirmed", ["explicit_identity_name"] + type_reasons,
                )
                audit.deterministic_source = "identity_field"

        if anchor is None and block.role == "process":
            process_match = _PROCESS_SUBJECT_RE.search(block.text)
            if process_match:
                subject = normalize_subject_name(process_match.group("subject").strip()).canonical_name
                if subject and len(subject) <= 80 and is_plausible_subject_name(subject):
                    subject_type, _, type_reasons = infer_subject_type(subject)
                    if any(token in subject for token in ("包覆", "复合", "基", "/")) and subject_type in {"材料", "未知类型"}:
                        subject_type = "配方/材料体系"
                    anchor = TextSubjectAnchor(
                        block.block_id, subject, subject_type, "explicit_process_subject_span",
                        0.91, "confirmed", ["exact_process_subject_span"] + type_reasons,
                    )
                    audit.deterministic_source = "explicit_process_subject_span"

        if anchor is None and block.role == "process":
            subject = _process_final_product_subject(block.text)
            if subject:
                subject_type, _, type_reasons = infer_subject_type(subject)
                anchor = TextSubjectAnchor(
                    block.block_id, subject, subject_type, "explicit_process_final_product_span",
                    0.89, "confirmed", ["last_explicit_final_product_span"] + type_reasons,
                )
                audit.deterministic_source = "explicit_process_final_product_span"

        if anchor is None and block.role == "process":
            subject = _process_local_entity_subject(block)
            if subject:
                subject_type, _, type_reasons = infer_subject_type(subject)
                if subject_type in {"材料", "未知类型"} and re.search(r"(?:加强帽|管壳|雷管|火帽|装置|组件|部件)$", subject):
                    subject_type = "火工品器件"
                anchor = TextSubjectAnchor(
                    block.block_id, subject, subject_type, "explicit_process_local_entity_span",
                    0.88, "confirmed", ["exact_local_process_entity_span"] + type_reasons,
                )
                audit.deterministic_source = "explicit_process_local_entity_span"

        # A strong local owner is more specific than a chapter default.  This is
        # the key rule that allows a section anchored to material A to contain a
        # valuable fact explicitly owned by material B.
        if anchor is None and local_owner is not None:
            subject = normalize_subject_name(local_owner.subject).canonical_name
            if is_plausible_subject_name(subject):
                subject_type, _, type_reasons = infer_subject_type(subject)
                confidence = min(0.95, max(0.86, local_owner.confidence))
                anchor = TextSubjectAnchor(
                    block.block_id, subject, subject_type, "explicit_local_fact_owner",
                    confidence, "confirmed",
                    [local_owner.role, "local_owner_overrides_heading_default"] + type_reasons,
                )
                audit.deterministic_source = "explicit_local_fact_owner"
        elif anchor is None and len(local_owner_names) > 1:
            reasons.append("multiple_explicit_local_fact_owners")

        direct_mentions = _text_mentions(block.text, table_registry)
        heading_mentions = _text_mentions(" > ".join(block.heading_path), table_registry)
        candidates: List[SubjectRegistryEntry] = list({item.subject_id: item for item in direct_mentions + heading_mentions}.values())
        if current and current.subject in entry_by_name:
            current_entry = entry_by_name[current.subject]
            if current_entry.subject_id not in {item.subject_id for item in candidates}:
                candidates.append(current_entry)

        # Multiple explicit local owners should normally have been split before
        # this stage.  Use the LLM only as a closed-set adjudicator when every
        # owner is already a confirmed registry entity; otherwise fail closed.
        if anchor is None and len(local_owner_names) > 1:
            owner_entries = [entry_by_name[name] for name in local_owner_names if name in entry_by_name]
            if len(owner_entries) == len(local_owner_names) and adjudicator.available:
                anchor, audit = _llm_choice(adjudicator, block, owner_entries, current)
            else:
                audit.deterministic_source = "multiple_local_owners_unresolved"

        # A coordinated heading defines a candidate set, never a single default.
        # A unique non-reference local mention may select one member; pronouns or
        # mentions of several members remain unresolved (or may be adjudicated
        # later as a closed set).
        if anchor is None and len(heading_scope_names) > 1:
            scoped_mentions = list(dict.fromkeys(
                item.subject for item in local_evidence
                if item.subject in heading_scope_names
                and item.role not in {"comparison_reference", "condition_or_reagent"}
                and item.confidence >= 0.60
            ))
            if len(scoped_mentions) == 1:
                subject = scoped_mentions[0]
                subject_type, _, type_reasons = infer_subject_type(subject)
                anchor = TextSubjectAnchor(
                    block.block_id, subject, subject_type, "multi_heading_unique_local_mention",
                    0.84, "confirmed", ["coordinated_heading_closed_scope", "unique_local_scope_member"] + type_reasons,
                )
                audit.deterministic_source = "multi_heading_unique_local_mention"
            else:
                reasons.append("coordinated_heading_requires_local_owner")

        # Resolve the full path on every block.  The nearest leaf may be a topic
        # (“力学性能”), while an ancestor is the actual entity (“HTPB推进剂”).
        if anchor is None and heading_candidate is not None and not explicit_owner_conflicts:
            subject = heading_candidate.entity
            subject_type = heading_candidate.subject_type or infer_subject_type(subject)[0]
            confidence = heading_candidate.confidence
            same_local_mention = any(
                item.subject == subject and item.role in {"property_owner", "entity_statement", "simple_mention"}
                for item in local_evidence
            )
            if heading_candidate.broad_class and same_local_mention:
                confidence = max(confidence, 0.86)
                reasons.append("broad_heading_confirmed_by_local_mention")
            status = "confirmed" if confidence >= 0.82 else "candidate"
            anchor = TextSubjectAnchor(
                block.block_id, subject, subject_type, "nearest_entity_heading_anchor",
                confidence, status,
                [heading_candidate.kind, "nearest_entity_heading_in_path"] + list(heading_candidate.reasons),
            )
            audit.deterministic_source = "nearest_entity_heading_anchor"
            if local_non_owner_names:
                reasons.append("local_reference_or_reagent_did_not_override_heading")

        # A unique confirmed registry mention is a fallback only when the
        # heading path contains no entity anchor.  Under an entity heading, an
        # unqualified mention may be a reagent, comparison target or citation.
        if anchor is None and not explicit_owner_conflicts and len(direct_mentions) == 1:
            entry = direct_mentions[0]
            reference_only = any(
                item.subject == entry.canonical_name and item.role in {"comparison_reference", "condition_or_reagent"}
                for item in local_evidence
            )
            if not reference_only:
                anchor = TextSubjectAnchor(
                    block.block_id, entry.canonical_name, entry.subject_type, "unique_confirmed_registry_mention",
                    min(0.90, 0.72 + entry.score * 0.18), "confirmed", ["unique_confirmed_subject_mention"],
                )
                audit.deterministic_source = "unique_confirmed_registry_mention"
        elif anchor is None and not explicit_owner_conflicts and len(direct_mentions) > 1 and not local_owner_names:
            reasons.append("multiple_subject_mentions")
            if adjudicator.available:
                anchor, audit = _llm_choice(adjudicator, block, candidates, current)
            else:
                audit.deterministic_source = "multiple_subject_mentions_no_llm"

        # Same-path and bounded inheritance are last-resort mechanisms.  They
        # are disabled when the block explicitly owns a fact for another entity.
        if (
            anchor is None
            and not local_owner_names
            and not explicit_owner_conflicts
            and current is not None
            and tuple(block.heading_path) == current_heading_path
            and block.role in {"material_profile", "formulation", "device_system"}
        ):
            anchor = TextSubjectAnchor(
                block.block_id, current.subject, current.subject_type, "same_heading_identity_inheritance",
                min(0.88, current.confidence - 0.04), current.status, ["reuse_subject_for_same_heading_path"],
            )
            audit.deterministic_source = "same_heading_identity_inheritance"

        if anchor is None and current is not None and not local_owner_names and not explicit_owner_conflicts:
            allowed_roles = {"property", "process", "experiment", "application_safety", "comparison", "unknown"}
            if (
                block.role in allowed_roles
                and chapter == current_chapter
                and _within_heading_scope(current_heading_path, block.heading_path)
            ):
                conflicting_heading = bool(heading_mentions and all(item.canonical_name != current.subject for item in heading_mentions))
                if not conflicting_heading:
                    anchor = TextSubjectAnchor(
                        block.block_id, current.subject, current.subject_type, "bounded_heading_inheritance",
                        min(0.76, current.confidence - 0.10),
                        "confirmed" if current.status == "confirmed" else "candidate",
                        ["same_chapter_bounded_inheritance"],
                    )
                    audit.deterministic_source = "bounded_heading_inheritance"

        if anchor is None:
            anchor = TextSubjectAnchor(block.block_id, "", "", "none", 0.0, "unresolved", reasons or ["no_reliable_subject_anchor"])

        candidate_names = [item.canonical_name for item in candidates]
        candidate_names.extend(local_owner_names)
        candidate_names.extend(heading_scope_names)
        if heading_subject:
            candidate_names.append(heading_subject)
        audit.candidate_subject_ids = [item.subject_id for item in candidates]
        audit.candidate_names = list(dict.fromkeys(name for name in candidate_names if name))
        audit.final_subject = anchor.subject
        audit.final_status = anchor.status
        if not audit.deterministic_source:
            audit.deterministic_source = anchor.source
        anchors.append(anchor)
        audits.append(audit)
        by_block[block.block_id] = anchor

        if anchor.source in {
            "identity_field", "nearest_entity_heading_anchor", "explicit_local_fact_owner",
            "explicit_process_subject_span", "explicit_process_final_product_span",
            "explicit_process_local_entity_span", "multi_heading_unique_local_mention", "closed_set_subject_llm",
        } and anchor.status in {"confirmed", "candidate"}:
            current = anchor
            current_chapter = chapter
            current_heading_path = tuple(block.heading_path)
        elif block.role == "theory" and block.heading_level <= 2 and heading_candidate is None:
            current = None
            current_chapter = chapter
            current_heading_path = ()

    return anchors, by_block, audits


def resolve_text_subject_anchors(
    blocks: Sequence[TextBlock],
    table_registry: Sequence[SubjectRegistryEntry],
) -> Tuple[List[TextSubjectAnchor], Dict[str, TextSubjectAnchor]]:
    anchors, by_block, _ = resolve_text_subject_anchors_with_audit(blocks, table_registry)
    return anchors, by_block


__all__ = [
    "TextSubjectAnchor", "SubjectAnchorAudit", "resolve_text_subject_anchors",
    "resolve_text_subject_anchors_with_audit", "is_plausible_subject_name",
]
