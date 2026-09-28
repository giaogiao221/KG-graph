from __future__ import annotations

from typing import Sequence, Tuple

from book_engine.ontology.property_alignment_engine import PropertyAlignmentDecision


def summarize_property_gate(decisions: Sequence[PropertyAlignmentDecision]) -> dict[str, object]:
    status_counts: dict[str, int] = {}
    accepted = 0
    rejected = 0
    for item in decisions:
        status_counts[item.status] = status_counts.get(item.status, 0) + 1
        if item.accepted:
            accepted += 1
        else:
            rejected += 1
    return {
        "ok": True,
        "decisions": len(decisions),
        "accepted": accepted,
        "rejected": rejected,
        "status_counts": status_counts,
    }


__all__ = ["summarize_property_gate"]
