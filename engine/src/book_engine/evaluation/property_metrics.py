from __future__ import annotations

from collections import Counter, defaultdict
from typing import Dict, Iterable, List, Mapping, MutableMapping, Sequence, Tuple


def _norm(text: object) -> str:
    return "".join(str(text or "").strip().casefold().split())


def _bool(text: object) -> str:
    value = _norm(text)
    if value in {"1", "true", "yes", "y", "是", "正确", "接受", "保留"}:
        return "1"
    if value in {"0", "false", "no", "n", "否", "错误", "拒绝", "删除"}:
        return "0"
    return ""


def _safe_div(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _accuracy(rows: Sequence[Mapping[str, str]], predicted: str, gold: str) -> Dict[str, object]:
    eligible = [row for row in rows if _norm(row.get(gold, ""))]
    correct = sum(1 for row in eligible if _norm(row.get(predicted, "")) == _norm(row.get(gold, "")))
    return {"eligible": len(eligible), "correct": correct, "accuracy": _safe_div(correct, len(eligible))}


def _accept_accuracy(rows: Sequence[Mapping[str, str]]) -> Dict[str, object]:
    eligible = [row for row in rows if _bool(row.get("gold_accept"))]
    correct = sum(1 for row in eligible if _bool(row.get("predicted_accepted")) == _bool(row.get("gold_accept")))
    return {"eligible": len(eligible), "correct": correct, "accuracy": _safe_div(correct, len(eligible))}


def _full_property(rows: Sequence[Mapping[str, str]]) -> Dict[str, object]:
    eligible = [
        row
        for row in rows
        if _norm(row.get("gold_attribute_category")) and _norm(row.get("gold_attribute_name"))
    ]
    correct = 0
    for row in eligible:
        if (
            _norm(row.get("predicted_attribute_category")) == _norm(row.get("gold_attribute_category"))
            and _norm(row.get("predicted_attribute_name")) == _norm(row.get("gold_attribute_name"))
        ):
            correct += 1
    return {"eligible": len(eligible), "correct": correct, "accuracy": _safe_div(correct, len(eligible))}


def _binary_dimension(rows: Sequence[Mapping[str, str]], field: str) -> Dict[str, object]:
    eligible = [row for row in rows if _bool(row.get(field))]
    correct = sum(1 for row in eligible if _bool(row.get(field)) == "1")
    return {"eligible": len(eligible), "correct": correct, "accuracy": _safe_div(correct, len(eligible))}


def _group_metrics(rows: Sequence[Mapping[str, str]], group_field: str) -> Dict[str, object]:
    groups: MutableMapping[str, List[Mapping[str, str]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(group_field, "") or "(blank)")].append(row)
    output: Dict[str, object] = {}
    for group, items in sorted(groups.items()):
        output[group] = {
            "rows": len(items),
            "category": _accuracy(items, "predicted_attribute_category", "gold_attribute_category"),
            "name": _accuracy(items, "predicted_attribute_name", "gold_attribute_name"),
            "full_property": _full_property(items),
            "accept": _accept_accuracy(items),
        }
    return output


def evaluate_gold_rows(rows: Sequence[Mapping[str, str]], thresholds: Mapping[str, float]) -> Dict[str, object]:
    completed = [
        row
        for row in rows
        if _norm(row.get("annotation_status")) in {"completed", "complete", "done", "已完成", "完成"}
    ]
    category = _accuracy(completed, "predicted_attribute_category", "gold_attribute_category")
    name = _accuracy(completed, "predicted_attribute_name", "gold_attribute_name")
    full_property = _full_property(completed)
    accept = _accept_accuracy(completed)
    subject = _binary_dimension(completed, "gold_subject_correct")
    value = _binary_dimension(completed, "gold_value_correct")
    condition = _binary_dimension(completed, "gold_condition_correct")
    method = _binary_dimension(completed, "gold_method_correct")

    checks = {
        "category_accuracy": category["accuracy"] >= float(thresholds.get("category_accuracy", 0.0)),
        "name_accuracy": name["accuracy"] >= float(thresholds.get("name_accuracy", 0.0)),
        "full_property_accuracy": full_property["accuracy"] >= float(thresholds.get("full_property_accuracy", 0.0)),
        "accept_accuracy": accept["accuracy"] >= float(thresholds.get("accept_accuracy", 0.0)),
    }
    predicted_status = Counter(str(row.get("predicted_status", "") or "(blank)") for row in rows)
    annotation_status = Counter(str(row.get("annotation_status", "") or "(blank)") for row in rows)
    return {
        "ok": bool(completed) and all(checks.values()),
        "rows_total": len(rows),
        "rows_completed": len(completed),
        "completion_rate": _safe_div(len(completed), len(rows)),
        "category": category,
        "name": name,
        "full_property": full_property,
        "accept": accept,
        "subject_correctness": subject,
        "value_correctness": value,
        "condition_correctness": condition,
        "method_correctness": method,
        "thresholds": dict(thresholds),
        "threshold_checks": checks,
        "predicted_status_distribution": dict(sorted(predicted_status.items())),
        "annotation_status_distribution": dict(sorted(annotation_status.items())),
        "by_source_type": _group_metrics(completed, "来源类型"),
        "by_predicted_status": _group_metrics(completed, "predicted_status"),
        "by_predicted_category": _group_metrics(completed, "predicted_attribute_category"),
        "by_run": _group_metrics(completed, "run_name"),
    }


__all__ = ["evaluate_gold_rows"]
