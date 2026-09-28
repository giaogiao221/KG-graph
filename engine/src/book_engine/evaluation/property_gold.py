from __future__ import annotations

import csv
import hashlib
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple


GOLD_COLUMNS: List[str] = [
    "sample_id",
    "run_name",
    "fact_id",
    "graph_fact_key",
    "书名",
    "章节路径",
    "来源类型",
    "所属表格ID",
    "所属表格标题",
    "主体名称",
    "主体类型",
    "raw_property",
    "predicted_attribute_category",
    "predicted_attribute_name",
    "predicted_property_id",
    "predicted_status",
    "predicted_confidence",
    "predicted_accepted",
    "尾实体/取值文本",
    "normalized_unit",
    "条件文本",
    "方法名称",
    "证据文本",
    "gold_attribute_category",
    "gold_attribute_name",
    "gold_property_id",
    "gold_accept",
    "gold_subject_correct",
    "gold_value_correct",
    "gold_condition_correct",
    "gold_method_correct",
    "annotation_status",
    "annotator",
    "annotation_note",
]


@dataclass(frozen=True)
class GoldSample:
    row: Mapping[str, str]
    stratum: str
    risk_score: float


def read_tsv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle, delimiter="\t")]


def write_tsv(path: Path, rows: Sequence[Mapping[str, object]], columns: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})


def discover_run_directories(run_root: Path) -> List[Path]:
    if not run_root.exists():
        raise FileNotFoundError(f"Run root does not exist: {run_root}")
    found: Dict[str, Path] = {}
    candidates = [run_root]
    candidates.extend(path for path in run_root.rglob("step_property_alignment") if path.is_dir())
    for candidate in candidates:
        run_dir = candidate.parent if candidate.name == "step_property_alignment" else candidate
        decisions = run_dir / "step_property_alignment" / "property_alignment_decisions.tsv"
        graph = run_dir / "step_graph_guard" / "graph_import_ready.tsv"
        if decisions.exists() and graph.exists():
            found[str(run_dir.resolve())] = run_dir
    return sorted(found.values(), key=lambda item: str(item).casefold())


def _normalize_bool(text: object) -> str:
    value = str(text or "").strip().casefold()
    if value in {"1", "true", "yes", "y", "是", "正确", "接受", "保留"}:
        return "1"
    if value in {"0", "false", "no", "n", "否", "错误", "拒绝", "删除"}:
        return "0"
    return ""


def _risk_score(decision: Mapping[str, str], graph: Mapping[str, str]) -> float:
    status = str(decision.get("status", ""))
    confidence_text = str(decision.get("confidence", "") or "0")
    try:
        confidence = float(confidence_text)
    except ValueError:
        confidence = 0.0
    score = 1.0 - max(0.0, min(1.0, confidence))
    if status in {"ambiguous", "unmapped", "rejected_non_property_label"}:
        score += 1.2
    elif status in {"aligned_llm", "aligned_legacy_default", "aligned_synthetic"}:
        score += 0.45
    if _normalize_bool(decision.get("accepted")) == "0":
        score += 0.5
    if str(graph.get("来源类型", "")).startswith("text"):
        score += 0.15
    if str(graph.get("条件文本", "")).strip():
        score += 0.12
    if not str(graph.get("normalized_unit", "")).strip() and str(graph.get("数值", "")).strip():
        score += 0.08
    return round(score, 6)


def _stratum(decision: Mapping[str, str], graph: Mapping[str, str]) -> str:
    source = "table" if str(graph.get("来源类型", "")).startswith("table") else "text"
    status = str(decision.get("status", "") or "unknown")
    category = str(decision.get("attribute_category", "") or "unknown")
    condition = "conditioned" if str(graph.get("条件文本", "")).strip() else "unconditioned"
    return f"{source}|{status}|{category}|{condition}"


def load_run_records(run_dir: Path) -> List[GoldSample]:
    decisions_path = run_dir / "step_property_alignment" / "property_alignment_decisions.tsv"
    graph_path = run_dir / "step_graph_guard" / "graph_import_ready.tsv"
    decisions = read_tsv(decisions_path)
    graph_rows = read_tsv(graph_path)
    graph_by_fact = {str(row.get("fact_id", "")): row for row in graph_rows if str(row.get("fact_id", ""))}
    output: List[GoldSample] = []
    for index, decision in enumerate(decisions):
        row_ref = str(decision.get("row_ref", ""))
        graph = graph_by_fact.get(row_ref)
        if graph is None:
            # Decisions rejected before final export can still be useful for acceptance evaluation.
            graph = {
                "fact_id": row_ref,
                "graph_fact_key": "",
                "书名": run_dir.name,
                "章节路径": "",
                "来源类型": "alignment_only",
                "所属表格ID": "",
                "所属表格标题": "",
                "主体名称": "",
                "主体类型": "",
                "尾实体/取值文本": "",
                "normalized_unit": "",
                "条件文本": "",
                "方法名称": "",
                "证据文本": "",
            }
        digest = hashlib.sha1(f"{run_dir.resolve()}|{row_ref}|{index}".encode("utf-8")).hexdigest()[:18]
        gold_row: Dict[str, str] = {
            "sample_id": f"gold:{digest}",
            "run_name": run_dir.name,
            "fact_id": str(graph.get("fact_id", row_ref)),
            "graph_fact_key": str(graph.get("graph_fact_key", "")),
            "书名": str(graph.get("书名", "")),
            "章节路径": str(graph.get("章节路径", "")),
            "来源类型": str(graph.get("来源类型", "")),
            "所属表格ID": str(graph.get("所属表格ID", "")),
            "所属表格标题": str(graph.get("所属表格标题", "")),
            "主体名称": str(graph.get("主体名称", "")),
            "主体类型": str(graph.get("主体类型", "")),
            "raw_property": str(decision.get("raw_property", graph.get("predicate_raw", ""))),
            "predicted_attribute_category": str(decision.get("attribute_category", graph.get("attribute_category", ""))),
            "predicted_attribute_name": str(decision.get("canonical_name", graph.get("attribute_name", ""))),
            "predicted_property_id": str(decision.get("property_id", "")),
            "predicted_status": str(decision.get("status", "")),
            "predicted_confidence": str(decision.get("confidence", "")),
            "predicted_accepted": _normalize_bool(decision.get("accepted")),
            "尾实体/取值文本": str(graph.get("尾实体/取值文本", "")),
            "normalized_unit": str(graph.get("normalized_unit", graph.get("单位", ""))),
            "条件文本": str(graph.get("条件文本", "")),
            "方法名称": str(graph.get("方法名称", "")),
            "证据文本": str(graph.get("证据文本", "")),
            "gold_attribute_category": "",
            "gold_attribute_name": "",
            "gold_property_id": "",
            "gold_accept": "",
            "gold_subject_correct": "",
            "gold_value_correct": "",
            "gold_condition_correct": "",
            "gold_method_correct": "",
            "annotation_status": "pending",
            "annotator": "",
            "annotation_note": "",
        }
        output.append(GoldSample(gold_row, _stratum(decision, graph), _risk_score(decision, graph)))
    return output


def _round_robin_strata(groups: Mapping[str, List[GoldSample]], target: int, rng: random.Random) -> List[GoldSample]:
    prepared: Dict[str, List[GoldSample]] = {}
    for key, items in groups.items():
        local = sorted(items, key=lambda item: (-item.risk_score, item.row["sample_id"]))
        # Randomize equal-risk tails reproducibly.
        buckets: Dict[float, List[GoldSample]] = {}
        for item in local:
            buckets.setdefault(item.risk_score, []).append(item)
        flattened: List[GoldSample] = []
        for risk in sorted(buckets, reverse=True):
            bucket = buckets[risk]
            rng.shuffle(bucket)
            flattened.extend(bucket)
        prepared[key] = flattened
    selected: List[GoldSample] = []
    keys = sorted(prepared)
    cursor = 0
    while len(selected) < target and keys:
        key = keys[cursor % len(keys)]
        if prepared[key]:
            selected.append(prepared[key].pop(0))
        keys = [item for item in keys if prepared[item]]
        cursor += 1
    return selected


def sample_gold_records(
    records: Sequence[GoldSample],
    *,
    sample_size: int,
    seed: int = 20260710,
    include_all_high_risk: bool = True,
) -> List[Mapping[str, str]]:
    if sample_size <= 0:
        raise ValueError("sample_size must be positive")
    if not records:
        return []
    rng = random.Random(seed)
    selected: List[GoldSample] = []
    selected_ids = set()
    if include_all_high_risk:
        high_risk = [item for item in records if item.risk_score >= 1.0]
        high_risk.sort(key=lambda item: (-item.risk_score, item.row["sample_id"]))
        for item in high_risk[:sample_size]:
            selected.append(item)
            selected_ids.add(item.row["sample_id"])
    remaining_target = sample_size - len(selected)
    if remaining_target > 0:
        groups: Dict[str, List[GoldSample]] = {}
        for item in records:
            if item.row["sample_id"] in selected_ids:
                continue
            groups.setdefault(item.stratum, []).append(item)
        for item in _round_robin_strata(groups, remaining_target, rng):
            selected.append(item)
            selected_ids.add(item.row["sample_id"])
    selected.sort(key=lambda item: (item.row["run_name"], item.row["来源类型"], item.row["sample_id"]))
    return [dict(item.row) for item in selected]


def load_policy(path: Optional[Path]) -> Dict[str, object]:
    defaults: Dict[str, object] = {
        "sample_size": 300,
        "seed": 20260710,
        "include_all_high_risk": True,
        "required_annotation_status": "completed",
        "thresholds": {
            "category_accuracy": 0.95,
            "name_accuracy": 0.90,
            "full_property_accuracy": 0.88,
            "accept_accuracy": 0.95,
        },
    }
    if path is None:
        return defaults
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    merged = dict(defaults)
    merged.update(payload)
    thresholds = dict(defaults["thresholds"])
    thresholds.update(payload.get("thresholds", {}))
    merged["thresholds"] = thresholds
    return merged


__all__ = [
    "GOLD_COLUMNS",
    "GoldSample",
    "discover_run_directories",
    "load_policy",
    "load_run_records",
    "read_tsv",
    "sample_gold_records",
    "write_tsv",
]
