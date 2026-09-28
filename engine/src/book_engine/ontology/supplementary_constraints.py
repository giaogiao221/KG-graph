"""Portable, conservative supplements derived from legacy rawarrange assets.

The module does not make a legacy vocabulary a closed ontology.  It only
normalizes exact aliases, proposes a known relation type, routes exact
deny-list subjects to review, and records lexicon matches for audit.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


def _clean(value: object) -> str:
    return str(value or "").strip()


def _read_tsv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle, delimiter="\t")]


def _read_lines(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    return {_clean(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if _clean(line)}


@dataclass(frozen=True)
class SupplementaryConstraints:
    policy: Mapping[str, Any]
    property_aliases: Mapping[str, str]
    relation_mapping: Mapping[str, str]
    subject_denylist: set[str]
    subject_lexicon: set[str]

    @classmethod
    def from_paths(
        cls,
        *,
        policy_path: Path,
        property_alias_path: Path,
        relation_mapping_path: Path,
        subject_denylist_path: Path,
        subject_lexicon_path: Path,
    ) -> "SupplementaryConstraints":
        policy: dict[str, Any] = {"enabled": False}
        if policy_path.is_file():
            loaded = json.loads(policy_path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                policy.update(loaded)
        aliases = {
            _clean(row.get("alias")): _clean(row.get("canonical_name") or row.get("canonical"))
            for row in _read_tsv(property_alias_path)
            if _clean(row.get("alias")) and _clean(row.get("canonical_name") or row.get("canonical"))
        }
        relations = {
            _clean(row.get("alias")): _clean(row.get("relation_type"))
            for row in _read_tsv(relation_mapping_path)
            if _clean(row.get("alias")) and _clean(row.get("relation_type"))
        }
        lexicon_rows = _read_tsv(subject_lexicon_path)
        lexicon = {_clean(row.get("name")) for row in lexicon_rows if _clean(row.get("name"))}
        if not lexicon:
            lexicon = _read_lines(subject_lexicon_path)
        return cls(policy, aliases, relations, _read_lines(subject_denylist_path), lexicon)

    @classmethod
    def from_default_config(cls) -> "SupplementaryConstraints":
        config_dir = Path(__file__).resolve().parents[2] / "config"
        return cls.from_paths(
            policy_path=config_dir / "supplementary_constraint_policy.json",
            property_alias_path=config_dir / "supplementary_property_aliases.tsv",
            relation_mapping_path=config_dir / "supplementary_relation_mapping.tsv",
            subject_denylist_path=config_dir / "supplementary_subject_denylist.txt",
            subject_lexicon_path=config_dir / "supplementary_subject_lexicon.tsv",
        )

    def _enabled(self, key: str) -> bool:
        return bool(self.policy.get("enabled", False)) and bool(self.policy.get(key, False))

    def prepare_row(self, row: Mapping[str, Any], *, stage: str) -> tuple[dict[str, Any], list[dict[str, str]]]:
        """Apply safe, non-destructive row preparation before ontology alignment."""
        result = dict(row)
        audits: list[dict[str, str]] = []
        attribute_key = "attribute_name" if "attribute_name" in result else "属性名称"
        raw_attribute = _clean(result.get(attribute_key) or result.get("predicate_raw"))
        canonical = self.property_aliases.get(raw_attribute, "") if self._enabled("property_alias_enabled") else ""
        if canonical and canonical != raw_attribute:
            result[attribute_key] = canonical
            audits.append({"stage": stage, "action": "property_alias_normalized", "reason": "exact_legacy_parameter_alias", "before": raw_attribute, "after": canonical})
        subject = _clean(result.get("主体名称") or result.get("subject"))
        if subject and subject in self.subject_lexicon and self._enabled("subject_lexicon_audit_enabled"):
            audits.append({"stage": stage, "action": "subject_lexicon_match", "reason": "legacy_gazetteer_exact_match", "before": subject, "after": subject})
        return result, audits

    def normalize_relation_type(self, raw_relation: object, *, fallback: str, stage: str) -> tuple[str, list[dict[str, str]]]:
        raw = _clean(raw_relation)
        normalized = self.relation_mapping.get(raw, "") if self._enabled("relation_normalization_enabled") else ""
        if normalized and normalized != fallback:
            return normalized, [{"stage": stage, "action": "relation_normalized", "reason": "explicit_legacy_relation_mapping", "before": raw, "after": normalized}]
        return fallback, []

    def subject_decision(self, subject: object, *, stage: str) -> tuple[str, list[dict[str, str]]]:
        clean_subject = _clean(subject)
        if self._enabled("subject_deny_enabled") and clean_subject and clean_subject in self.subject_denylist:
            action = _clean(self.policy.get("subject_deny_action")) or "manual_review"
            return action, [{"stage": stage, "action": action, "reason": "subject_exact_denylist_match", "before": clean_subject, "after": clean_subject}]
        return "keep", []


def write_audits(path: Path, audits: list[Mapping[str, Any]]) -> None:
    """Write JSONL only when a caller has audit records to preserve."""
    if not audits:
        return
    with path.open("a", encoding="utf-8") as handle:
        for item in audits:
            handle.write(json.dumps(dict(item), ensure_ascii=False) + "\n")


__all__ = ["SupplementaryConstraints", "write_audits"]
