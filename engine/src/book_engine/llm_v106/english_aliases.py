from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

_WORD_SEP_RE = re.compile(r"[_\-/]+")
_NON_WORD_RE = re.compile(r"[^a-z0-9%µμ°]+")
_UNIT_SUFFIX_RE = re.compile(r"\s*[\[(](?:[^\])]{0,30}(?:%|°c|k|pa|mpa|gpa|m|cm|mm|µm|μm|nm|s|min|h))[^\])]*[\])]\s*$", re.I)
_ABBREVIATION_RE = re.compile(r"^[A-Z][A-Z0-9-]{1,11}$")

# Full-name boundaries are deliberately conservative. A full name must contain
# alphabetic material and may include common scientific punctuation.
_FULL_NAME = r"[A-Za-z][A-Za-z0-9'α-ωΑ-Ωµμ+./-]*(?:\s+[A-Za-z][A-Za-z0-9'α-ωΑ-Ωµμ+./-]*){0,8}"
_FULL_THEN_ABBR = re.compile(rf"(?P<full>{_FULL_NAME})\s*\(\s*(?P<abbr>[A-Z][A-Z0-9-]{{1,11}})\s*\)")
_ABBR_THEN_FULL = re.compile(rf"(?P<abbr>[A-Z][A-Z0-9-]{{1,11}})\s*\(\s*(?P<full>{_FULL_NAME})\s*\)")
_FULL_COMMA_ABBR = re.compile(rf"(?P<full>{_FULL_NAME})\s*,\s*(?P<abbr>[A-Z][A-Z0-9-]{{1,11}})(?=\b|[.;,])")
_LEADING_STOPWORDS = {
    "the", "a", "an", "and", "or", "with", "using", "of", "for", "in", "on", "to", "from",
    "was", "were", "is", "are", "be", "been", "being", "also", "then", "subsequently",
    "blended", "mixed", "combined", "prepared", "tested", "used",
}
_TRAILING_STOPWORDS = {"and", "or", "with", "using", "of", "for", "in", "on", "to", "from", "was", "were", "is", "are"}


@dataclass(frozen=True)
class AbbreviationPair:
    full_name: str
    abbreviation: str
    full_name_span: str
    abbreviation_span: str
    evidence_span: str
    start: int
    end: int


def normalize_english_alias(value: str) -> str:
    text = str(value or "").strip().casefold()
    text = text.replace("−", "-").replace("–", "-").replace("—", "-")
    text = _UNIT_SUFFIX_RE.sub("", text)
    text = _WORD_SEP_RE.sub(" ", text)
    text = _NON_WORD_RE.sub(" ", text)
    tokens = [token for token in text.split() if token]
    # Very small morphology normalization for common table-header variants.
    normalized: list[str] = []
    for token in tokens:
        if len(token) > 4 and token.endswith("ies"):
            token = token[:-3] + "y"
        elif len(token) > 4 and token.endswith("s") and not token.endswith(("ss", "us")):
            token = token[:-1]
        normalized.append(token)
    return " ".join(normalized)


def _clean_full_name(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip(" ,.;:")
    tokens = text.split()
    while tokens and tokens[0].casefold() in _LEADING_STOPWORDS:
        tokens.pop(0)
    while tokens and tokens[-1].casefold() in _TRAILING_STOPWORDS:
        tokens.pop()
    return " ".join(tokens)


def _plausible_pair(full_name: str, abbreviation: str) -> bool:
    if not _ABBREVIATION_RE.fullmatch(abbreviation):
        return False
    full_name = _clean_full_name(full_name)
    if len(full_name) < 3 or len(full_name) > 120:
        return False
    words = re.findall(r"[A-Za-z][A-Za-z0-9'/-]*", full_name)
    if not words:
        return False
    if len(words) == 1 and len(words[0]) <= len(abbreviation):
        return False
    # Reject obvious prose fragments that end in auxiliaries/connectors.
    if words[-1].casefold() in _TRAILING_STOPWORDS:
        return False
    return True


def extract_explicit_abbreviation_pairs(text: str) -> tuple[AbbreviationPair, ...]:
    source = str(text or "")
    results: list[AbbreviationPair] = []
    seen: set[tuple[str, str, int, int]] = set()
    for pattern in (_FULL_THEN_ABBR, _ABBR_THEN_FULL, _FULL_COMMA_ABBR):
        for match in pattern.finditer(source):
            full_raw = match.group("full")
            abbreviation = match.group("abbr").strip()
            full_name = _clean_full_name(full_raw)
            if not _plausible_pair(full_name, abbreviation):
                continue
            # Preserve exact source spans after trimming any leading prose word.
            local_start = full_raw.find(full_name)
            full_span = full_name if local_start >= 0 else full_raw.strip()
            key = (full_name.casefold(), abbreviation, match.start(), match.end())
            if key in seen:
                continue
            seen.add(key)
            results.append(
                AbbreviationPair(
                    full_name=full_name,
                    abbreviation=abbreviation,
                    full_name_span=full_span,
                    abbreviation_span=abbreviation,
                    evidence_span=match.group(0),
                    start=match.start(),
                    end=match.end(),
                )
            )
    results.sort(key=lambda item: (item.start, item.end, item.full_name.casefold(), item.abbreviation))
    return tuple(results)


def load_tsv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle, delimiter="\t")]


def load_english_property_aliases(path: Path) -> list[dict[str, str]]:
    return [row for row in load_tsv_rows(path) if str(row.get("alias", "") or "").strip() and str(row.get("canonical_name", "") or "").strip()]


def load_english_relation_aliases(path: Path) -> list[dict[str, str]]:
    return [row for row in load_tsv_rows(path) if str(row.get("alias", "") or "").strip() and str(row.get("relation_type", "") or "").strip()]


def relation_prompt_examples(rows: Sequence[Mapping[str, str]], limit: int = 24) -> list[str]:
    examples: list[str] = []
    for row in rows:
        alias = str(row.get("alias", "") or "").strip()
        relation = str(row.get("relation_type", "") or "").strip()
        if alias and relation:
            examples.append(f"{alias} → {relation}")
        if len(examples) >= limit:
            break
    return examples


__all__ = [
    "AbbreviationPair",
    "extract_explicit_abbreviation_pairs",
    "load_english_property_aliases",
    "load_english_relation_aliases",
    "normalize_english_alias",
    "relation_prompt_examples",
]
