from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

from book_engine.document.block_segmenter import TextBlock
from book_engine.document.semantic_split_llm_adjudicator import SemanticSplitLLMAdjudicator
from book_engine.routing.heading_subject_resolver import (
    build_subject_lexicon,
    detect_local_subject_evidence,
    heading_path_candidates,
    nearest_scope_from_candidates,
    registry_subject_names,
)
from book_engine.routing.subject_registry_builder import SubjectRegistryEntry

_SENTENCE_RE = re.compile(r".+?(?:[。；;！？!?](?=\s|$)|\n+|$)", re.S)
_CLAUSE_BOUNDARY_RE = re.compile(r"(?<=，)(?=(?:而|但|同时|其中|相比之下|相反|此外))|(?<=[；;])")
_IDENTITY_FIELD_RE = re.compile(r"(?:中文名称|英文名称|分子式|CAS(?:登记号|号)?)\s*[：:]")


@dataclass
class BlockSplitAudit:
    parent_block_id: str
    child_block_id: str
    action: str
    reason: str
    subjects: List[str] = field(default_factory=list)
    llm_used: bool = False
    llm_status: str = ""
    llm_confidence: float = 0.0
    text: str = ""


def _mentioned_names(text: str, entries: Sequence[SubjectRegistryEntry]) -> List[str]:
    result: List[str] = []
    for entry in entries:
        if entry.status != "confirmed" or not entry.canonical_name:
            continue
        names = [entry.canonical_name] + list(entry.aliases)
        for name in names:
            if not name:
                continue
            if re.fullmatch(r"[A-Za-z0-9+._/-]+", name):
                hit = re.search(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", text, re.I)
            else:
                hit = name in text
            if hit:
                result.append(entry.canonical_name)
                break
    return list(dict.fromkeys(result))


def _base_fragments(text: str) -> List[str]:
    sentences = [m.group(0).strip() for m in _SENTENCE_RE.finditer(text or "") if m.group(0).strip()]
    result: List[str] = []
    for sentence in sentences:
        clauses = [item.strip() for item in _CLAUSE_BOUNDARY_RE.split(sentence) if item.strip()]
        result.extend(clauses if len(clauses) > 1 else [sentence])
    return result


def _owner_names(fragment: str, lexicon: Sequence[str], heading_subject: str) -> List[str]:
    evidence = detect_local_subject_evidence(fragment, lexicon, heading_subject=heading_subject)
    return list(dict.fromkeys(item.subject for item in evidence if item.is_owner and item.confidence >= 0.82))


def _fragments(text: str, lexicon: Sequence[str], heading_subject: str) -> List[str]:
    """Split only when punctuation separates different explicit fact owners.

    A comma is not normally a semantic boundary because it often separates a
    material from a reagent or condition.  It becomes a boundary only when two
    comma-separated clauses each have a different explicit owner.
    """
    output: List[str] = []
    for fragment in _base_fragments(text):
        pieces = [item.strip() for item in re.split(r"(?<=[，,])", fragment) if item.strip()]
        owners = [_owner_names(piece, lexicon, heading_subject) for piece in pieces]
        explicit = [(index, names[0]) for index, names in enumerate(owners) if len(names) == 1]
        if len(explicit) >= 2 and len({name for _, name in explicit}) >= 2:
            output.extend(pieces)
        else:
            output.append(fragment)
    return output


def _child(block: TextBlock, index: int, text: str) -> TextBlock:
    digest = hashlib.sha1(f"{block.block_id}|{index}|{text}".encode("utf-8")).hexdigest()[:12]
    return replace(block, block_id=f"{block.block_id}:S{index:02d}:{digest}", text=text)


def _fragment_focus(fragment: str, lexicon: Sequence[str], heading_subject: str) -> Tuple[str, str, List[str]]:
    evidence = detect_local_subject_evidence(fragment, lexicon, heading_subject=heading_subject)
    owners = list(dict.fromkeys(item.subject for item in evidence if item.is_owner and item.confidence >= 0.82))
    if len(owners) == 1:
        return owners[0], "explicit_local_owner", owners
    if len(owners) > 1:
        return "", "multiple_local_owners", owners

    # Comparison targets and reagents do not replace the heading scope.  A
    # unique simple mention can establish focus only when no entity heading is
    # available at all.
    simple = list(dict.fromkeys(
        item.subject for item in evidence
        if item.role == "simple_mention" and item.confidence >= 0.60
    ))
    if not heading_subject and len(simple) == 1:
        return simple[0], "unique_unscoped_mention", simple
    if heading_subject:
        return heading_subject, "heading_default", [heading_subject]
    return "", "unresolved", simple


def split_multisubject_blocks(
    blocks: Sequence[TextBlock],
    registry_entries: Sequence[SubjectRegistryEntry],
) -> Tuple[List[TextBlock], List[BlockSplitAudit]]:
    enabled = os.getenv("KGCHOUQU_SEMANTIC_SPLIT_LLM_ENABLED") == "1"
    model_root = Path(__file__).resolve().parents[2]
    adjudicator = SemanticSplitLLMAdjudicator(
        enabled=enabled,
        cache_dir=model_root / "cache" / "semantic_split_phase94",
    )
    output: List[TextBlock] = []
    audits: List[BlockSplitAudit] = []
    registry_names = registry_subject_names(registry_entries)
    lexicon = build_subject_lexicon((block.heading_path for block in blocks), registry_names)

    for block in blocks:
        # Ordered process extraction needs the full paragraph.  Its own subject
        # resolver distinguishes products from reagents and conditions.
        if block.role == "process" or _IDENTITY_FIELD_RE.search(block.text):
            output.append(block)
            audits.append(BlockSplitAudit(block.block_id, block.block_id, "keep", "protected_process_or_identity_block", text=block.text))
            continue

        path_candidates = heading_path_candidates(block.heading_path, registry_names)
        heading = next((item for item in reversed(path_candidates) if item.is_entity), None)
        heading_scope_names = nearest_scope_from_candidates(path_candidates)
        heading_subject = heading.entity if heading else ""
        local_lexicon = sorted(dict.fromkeys([*heading_scope_names, *lexicon]), key=len, reverse=True)
        fragments = _fragments(block.text, local_lexicon, heading_subject)
        focus = [_fragment_focus(fragment, local_lexicon, heading_subject) for fragment in fragments]
        all_subjects = list(dict.fromkeys(
            name for _, _, names in focus for name in names
        ))

        deterministic_boundaries: List[int] = []
        for index in range(len(fragments) - 1):
            left_name, left_source, _ = focus[index]
            right_name, right_source, _ = focus[index + 1]
            if not left_name or not right_name or left_name == right_name:
                continue
            # At least one side must explicitly establish a local owner.  This
            # prevents a mere comparison/reagent mention from creating a split.
            if "explicit_local_owner" in {left_source, right_source}:
                deterministic_boundaries.append(index)

        selected = set(deterministic_boundaries)
        llm_used = False
        llm_status = ""
        llm_confidence = 0.0
        ambiguous = any(source == "multiple_local_owners" for _, source, _ in focus)
        if len(fragments) >= 2 and not selected and ambiguous and adjudicator.available:
            candidate_boundaries = list(range(len(fragments) - 1))
            fragment_mentions: Dict[int, List[str]] = {
                index: names for index, (_, _, names) in enumerate(focus)
            }
            result = adjudicator.adjudicate(
                heading_path=block.heading_path,
                fragments=fragments,
                candidate_boundaries=candidate_boundaries,
                subject_mentions=fragment_mentions,
            )
            if result is not None:
                llm_used = True
                llm_status = result.status
                llm_confidence = result.confidence
                if result.confidence >= 0.78:
                    selected.update(result.split_after_indexes)

        if not selected:
            output.append(block)
            reason = "multiple_subjects_but_no_reliable_boundary" if (ambiguous or len(all_subjects) > 1) else "single_subject_scope"
            action = "hold_unsplit" if ambiguous else "keep"
            audits.append(
                BlockSplitAudit(
                    block.block_id, block.block_id, action, reason, all_subjects,
                    llm_used, llm_status, llm_confidence, block.text,
                )
            )
            continue

        groups: List[str] = []
        start = 0
        for index in range(len(fragments) - 1):
            if index in selected:
                groups.append("".join(fragments[start:index + 1]).strip())
                start = index + 1
        groups.append("".join(fragments[start:]).strip())
        groups = [item for item in groups if item]
        if len(groups) < 2:
            output.append(block)
            audits.append(BlockSplitAudit(block.block_id, block.block_id, "keep", "split_collapsed_to_one_group", all_subjects, llm_used, llm_status, llm_confidence, block.text))
            continue

        for index, text in enumerate(groups, start=1):
            child = _child(block, index, text)
            child_focus, _, child_names = _fragment_focus(text, local_lexicon, heading_subject)
            output.append(child)
            audits.append(
                BlockSplitAudit(
                    block.block_id, child.block_id, "split", "explicit_subject_focus_change",
                    child_names or ([child_focus] if child_focus else []),
                    llm_used, llm_status, llm_confidence, text,
                )
            )
    return output, audits


__all__ = ["BlockSplitAudit", "split_multisubject_blocks"]
