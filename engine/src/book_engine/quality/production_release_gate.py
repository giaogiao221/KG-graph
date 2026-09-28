from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Sequence

from .evidence_consistency import EvidenceAssessment, assess_row, semantic_key
from .production_fact_adjudicator import FactAdjudication, ProductionFactAdjudicator


_DEFAULT_POLICY: dict[str, object] = {
    "profile": "precision",
    "profiles": {
        "precision": {"release_threshold": 0.80, "review_threshold": 0.52},
        "balanced": {"release_threshold": 0.74, "review_threshold": 0.48},
        "recall": {"release_threshold": 0.68, "review_threshold": 0.42},
    },
    "llm_enabled": False,
    "llm_release_confidence": 0.82,
    "llm_timeout": 120,
    "llm_max_retries": 1,
    "llm_consensus_rounds": 2,
    "fail_closed": True,
    "unmapped_property_requires_llm": True,
    "source_adjustments": {},
}


@dataclass(frozen=True)
class ProductionDecision:
    fact_id: str
    graph_fact_key: str
    action: str
    deterministic_action: str
    score: float
    hard_reject_reasons: Sequence[str] = field(default_factory=tuple)
    review_reasons: Sequence[str] = field(default_factory=tuple)
    positive_reasons: Sequence[str] = field(default_factory=tuple)
    llm_used: bool = False
    llm_action: str = ""
    llm_confidence: float = 0.0
    llm_status: str = ""
    llm_reason: str = ""


@dataclass(frozen=True)
class ProductionGateResult:
    released_rows: Sequence[Dict[str, object]]
    candidate_rows: Sequence[Dict[str, object]]
    rejected_rows: Sequence[Dict[str, object]]
    decisions: Sequence[ProductionDecision]
    llm_queue: Sequence[Dict[str, object]]
    llm_responses: Sequence[Dict[str, object]]
    profile: str
    llm_available: bool


def _load_policy(policy_path: Path | None = None) -> dict[str, object]:
    policy = json.loads(json.dumps(_DEFAULT_POLICY))
    if policy_path and policy_path.exists():
        loaded = json.loads(policy_path.read_text(encoding="utf-8-sig"))
        if isinstance(loaded, dict):
            for key, value in loaded.items():
                if key == "profiles" and isinstance(value, dict):
                    policy["profiles"].update(value)  # type: ignore[index]
                else:
                    policy[key] = value
    profile_env = os.getenv("KGCHOUQU_PRODUCTION_PROFILE", "").strip().lower()
    if profile_env:
        policy["profile"] = profile_env
    return policy


def _default_policy_path() -> Path:
    model_root = Path(__file__).resolve().parents[2]
    return model_root / "config" / "production_release_policy_v2_phase9.json"


def _initial_action(assessment: EvidenceAssessment, release_threshold: float, review_threshold: float) -> str:
    if assessment.hard_reject_reasons:
        return "reject"
    if assessment.score >= release_threshold:
        return "release"
    if assessment.score >= review_threshold:
        return "candidate"
    return "reject"


def apply_production_release_gate(
    rows: Sequence[Dict[str, object]],
    *,
    policy_path: Path | None = None,
) -> ProductionGateResult:
    policy = _load_policy(policy_path or _default_policy_path())
    profile = str(policy.get("profile", "precision") or "precision").lower()
    profiles = policy.get("profiles", {})
    profile_cfg = profiles.get(profile, profiles.get("precision", {})) if isinstance(profiles, dict) else {}
    release_threshold = float(profile_cfg.get("release_threshold", 0.80))
    review_threshold = float(profile_cfg.get("review_threshold", 0.52))
    source_adjustments = policy.get("source_adjustments", {})
    source_adjustments = source_adjustments if isinstance(source_adjustments, dict) else {}

    llm_enabled = bool(policy.get("llm_enabled", False)) or os.getenv("KGCHOUQU_PRODUCTION_LLM_ENABLED") == "1"
    model_root = Path(__file__).resolve().parents[2]
    cache_env = os.getenv("KGCHOUQU_PRODUCTION_LLM_CACHE", "").strip()
    cache_dir = Path(cache_env) if cache_env else model_root / "cache" / "production_fact_adjudication"
    adjudicator = ProductionFactAdjudicator(
        enabled=llm_enabled,
        cache_dir=cache_dir,
        timeout=float(policy.get("llm_timeout", 120)),
        max_retries=int(policy.get("llm_max_retries", 1)),
        consensus_rounds=int(policy.get("llm_consensus_rounds", 2)),
    )
    llm_release_confidence = float(policy.get("llm_release_confidence", 0.82))
    require_llm_for_unmapped = bool(policy.get("unmapped_property_requires_llm", True))

    corroboration = Counter(semantic_key(row) for row in rows)
    released: List[Dict[str, object]] = []
    candidates: List[Dict[str, object]] = []
    rejected: List[Dict[str, object]] = []
    decisions: List[ProductionDecision] = []
    llm_queue: List[Dict[str, object]] = []
    llm_responses: List[Dict[str, object]] = []

    for row in rows:
        assessment = assess_row(
            row,
            corroboration_count=corroboration[semantic_key(row)],
            source_adjustments=source_adjustments,
        )
        deterministic_action = _initial_action(assessment, release_threshold, review_threshold)
        action = deterministic_action
        llm_used = False
        llm_result: FactAdjudication | None = None

        alignment_status = str(row.get("_property_alignment_status", "") or "")
        is_unmapped = str(row.get("attribute_category", "") or "") in {"", "其他属性"} or alignment_status == "unmapped"
        if action == "release" and is_unmapped and require_llm_for_unmapped:
            action = "candidate"

        risk_reasons = list(assessment.hard_reject_reasons) + list(assessment.review_reasons)
        if action == "candidate":
            llm_queue.append(
                {
                    "fact_id": row.get("fact_id"),
                    "graph_fact_key": row.get("graph_fact_key"),
                    "score": round(assessment.score, 6),
                    "risk_reasons": risk_reasons,
                    "row": {key: row.get(key, "") for key in (
                        "主体名称", "主体类型", "attribute_category", "attribute_name", "predicate_raw",
                        "尾实体/取值文本", "单位", "条件文本", "方法名称", "来源类型", "章节路径",
                        "所属表格标题", "证据文本",
                    )},
                }
            )
            if adjudicator.available:
                llm_used = True
                llm_result = adjudicator.adjudicate(row, risk_reasons)
                if llm_result.action == "release" and llm_result.confidence >= llm_release_confidence:
                    action = "release"
                elif llm_result.action == "reject":
                    action = "reject"
                else:
                    action = "candidate"
                llm_responses.append(
                    {
                        "fact_id": row.get("fact_id"),
                        "graph_fact_key": row.get("graph_fact_key"),
                        **llm_result.__dict__,
                    }
                )

        output_row = dict(row)
        # Internal metadata is deliberately not part of the legacy 59-column export.
        output_row["_production_score"] = f"{assessment.score:.6f}"
        output_row["_production_action"] = action
        if action == "release":
            released.append(output_row)
        elif action == "candidate":
            candidates.append(output_row)
        else:
            rejected.append(output_row)

        decisions.append(
            ProductionDecision(
                fact_id=str(row.get("fact_id", "") or ""),
                graph_fact_key=str(row.get("graph_fact_key", "") or ""),
                action=action,
                deterministic_action=deterministic_action,
                score=assessment.score,
                hard_reject_reasons=assessment.hard_reject_reasons,
                review_reasons=assessment.review_reasons,
                positive_reasons=assessment.positive_reasons,
                llm_used=llm_used,
                llm_action=llm_result.action if llm_result else "",
                llm_confidence=llm_result.confidence if llm_result else 0.0,
                llm_status=llm_result.status if llm_result else "",
                llm_reason=llm_result.reason if llm_result else "",
            )
        )

    return ProductionGateResult(
        released_rows=released,
        candidate_rows=candidates,
        rejected_rows=rejected,
        decisions=decisions,
        llm_queue=llm_queue,
        llm_responses=llm_responses,
        profile=profile,
        llm_available=adjudicator.available,
    )


__all__ = ["ProductionDecision", "ProductionGateResult", "apply_production_release_gate"]
