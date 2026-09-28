from __future__ import annotations

from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from book_engine.core.schemas import ConditionAtom, ConditionBinding, ConditionCandidate


def _norm_value(candidate: ConditionCandidate) -> str:
    return "".join((candidate.value_text or "").lower().split())


def _applies(candidate: ConditionCandidate, row: Optional[int], column: Optional[int]) -> bool:
    if candidate.scope in {"document", "section", "table"}:
        return True
    if candidate.scope == "row":
        return candidate.target_row == row
    if candidate.scope == "column":
        return candidate.target_column == column
    if candidate.scope == "cell":
        return candidate.target_row == row and candidate.target_column == column
    return False


def _binding_target(candidate: ConditionCandidate, row: Optional[int], column: Optional[int]) -> str:
    if candidate.scope == "cell":
        return f"cell:{row}:{column}"
    if candidate.scope == "row":
        return f"row:{row}"
    if candidate.scope == "column":
        return f"column:{column}"
    return candidate.scope


def bind_conditions(
    candidates: Sequence[ConditionCandidate],
    record_id: str,
    table_id: str,
    row: Optional[int],
    column: Optional[int],
    include_composition: bool = True,
) -> Tuple[List[ConditionAtom], List[ConditionBinding], List[str]]:
    applicable = [candidate for candidate in candidates if _applies(candidate, row, column)]
    if not include_composition:
        applicable = [candidate for candidate in applicable if candidate.condition_type != "composition"]

    groups: Dict[Tuple[str, str], List[ConditionCandidate]] = defaultdict(list)
    for candidate in applicable:
        key = (candidate.condition_type, candidate.normalized_name or candidate.name)
        groups[key].append(candidate)

    atoms: List[ConditionAtom] = []
    bindings: List[ConditionBinding] = []
    unresolved: List[str] = []

    for key, group in sorted(groups.items(), key=lambda item: item[0]):
        ranked = sorted(group, key=lambda item: (-item.priority, -item.confidence, item.condition_id))
        top_priority = ranked[0].priority
        top = [candidate for candidate in ranked if candidate.priority == top_priority]
        distinct_values = {_norm_value(candidate) for candidate in top}
        conflict = len(distinct_values) > 1
        selected: List[ConditionCandidate]
        if conflict:
            selected = top
            unresolved.append(f"condition_conflict:{key[1]}")
        else:
            selected = [top[0]]

        selected_ids = {candidate.condition_id for candidate in selected}
        for candidate in ranked:
            applied = candidate.condition_id in selected_ids
            overridden = not applied and candidate.priority < top_priority
            bindings.append(
                ConditionBinding(
                    record_id=record_id,
                    condition_id=candidate.condition_id,
                    table_id=table_id,
                    row_index=row,
                    column_index=column,
                    binding_target=_binding_target(candidate, row, column),
                    applied=applied,
                    overridden=overridden,
                    conflict=conflict and candidate.priority == top_priority,
                    reason=(
                        "selected_highest_scope_priority"
                        if applied and not conflict
                        else "selected_conflicting_same_priority"
                        if applied and conflict
                        else "overridden_by_more_specific_condition"
                        if overridden
                        else "duplicate_lower_confidence"
                    ),
                )
            )

        for candidate in selected:
            atom = candidate.to_atom(_binding_target(candidate, row, column))
            atom.is_conflict = conflict
            atoms.append(atom)

    atoms.sort(key=lambda atom: (-atom.priority, atom.condition_type, atom.normalized_name, atom.value_text))
    return atoms, bindings, sorted(set(unresolved))


__all__ = ["bind_conditions"]
