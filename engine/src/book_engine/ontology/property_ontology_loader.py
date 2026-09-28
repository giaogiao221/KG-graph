from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Sequence


def normalize_key(text: object) -> str:
    value = str(text or "").strip().lower().replace("（", "(").replace("）", ")")
    value = value.replace("−", "-").replace("–", "-").replace("—", "-")
    value = re.sub(r"\s+", "", value)
    value = re.sub(r"[：:;；,，。·•\-_/\\\"'()\[\]{}]", "", value)
    return value


def split_pipe(text: object) -> List[str]:
    return [item.strip() for item in str(text or "").split("|") if item.strip()]


@dataclass(frozen=True)
class PropertyOntologyEntry:
    property_id: str
    alias: str
    canonical_name: str
    attribute_category: str
    root_system: str = ""
    ontology_path: str = ""
    source: str = ""
    priority: int = 0
    unit_dimensions: Sequence[str] = field(default_factory=tuple)
    allowed_value_roles: Sequence[str] = field(default_factory=tuple)
    context_positive: Sequence[str] = field(default_factory=tuple)
    context_negative: Sequence[str] = field(default_factory=tuple)

    @property
    def alias_key(self) -> str:
        return normalize_key(self.alias)

    @property
    def canonical_key(self) -> str:
        return normalize_key(self.canonical_name)


@dataclass
class PropertyOntology:
    entries: List[PropertyOntologyEntry]
    by_alias: Dict[str, List[PropertyOntologyEntry]] = field(default_factory=dict)
    by_canonical: Dict[str, List[PropertyOntologyEntry]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for entry in self.entries:
            if entry.alias_key:
                self.by_alias.setdefault(entry.alias_key, []).append(entry)
            if entry.canonical_key:
                self.by_canonical.setdefault(entry.canonical_key, []).append(entry)
        for index in (self.by_alias, self.by_canonical):
            for key in index:
                index[key] = sorted(index[key], key=lambda item: (-item.priority, item.property_id))

    def __len__(self) -> int:
        """Return the number of ontology rows.

        Phase 8A smoke tests and downstream diagnostics treat the ontology as a
        collection. Exposing __len__ keeps that usage stable while preserving
        the explicit .entries member for code that needs the underlying list.
        """
        return len(self.entries)

    def __iter__(self) -> Iterator[PropertyOntologyEntry]:
        return iter(self.entries)

    def __bool__(self) -> bool:
        return bool(self.entries)


def load_property_ontology(path: Path) -> PropertyOntology:
    if not path.exists():
        raise FileNotFoundError(f"Property ontology does not exist: {path}")
    rows: List[PropertyOntologyEntry] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        required = {"property_id", "alias", "canonical_name", "attribute_category"}
        missing = sorted(required - set(reader.fieldnames or []))
        if missing:
            raise ValueError(f"Property ontology is missing required columns: {missing}")
        for raw in reader:
            alias = str(raw.get("alias", "") or "").strip()
            canonical = str(raw.get("canonical_name", "") or "").strip()
            category = str(raw.get("attribute_category", "") or "").strip()
            if not alias or not canonical or not category:
                continue
            try:
                priority = int(float(str(raw.get("priority", "0") or "0")))
            except ValueError:
                priority = 0
            rows.append(
                PropertyOntologyEntry(
                    property_id=str(raw.get("property_id", "") or "").strip(),
                    alias=alias,
                    canonical_name=canonical,
                    attribute_category=category,
                    root_system=str(raw.get("root_system", "") or "").strip(),
                    ontology_path=str(raw.get("ontology_path", "") or "").strip(),
                    source=str(raw.get("source", "") or "").strip(),
                    priority=priority,
                    unit_dimensions=tuple(split_pipe(raw.get("unit_dimensions", ""))),
                    allowed_value_roles=tuple(split_pipe(raw.get("allowed_value_roles", ""))),
                    context_positive=tuple(split_pipe(raw.get("context_positive", ""))),
                    context_negative=tuple(split_pipe(raw.get("context_negative", ""))),
                )
            )
    if not rows:
        raise ValueError(f"Property ontology contains no usable rows: {path}")
    return PropertyOntology(rows)


__all__ = [
    "PropertyOntology",
    "PropertyOntologyEntry",
    "load_property_ontology",
    "normalize_key",
]
