from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence

_ALLOWED_TOPOLOGIES = {
    "entity_by_property", "entity_by_condition", "condition_by_property", "formulation_matrix",
    "experiment_result", "process_parameter_result", "method_comparison", "pairwise_compatibility",
    "qualitative_taxonomy", "model_validation", "composition_and_performance",
}
_ALLOWED_ORIENTATIONS = {"row_subject", "column_subject"}
_ALLOWED_ROLES = {
    "entity", "identifier", "property", "property_value", "condition", "value", "method",
    "composition", "group", "note", "unknown",
}


@dataclass(frozen=True)
class TableAxisSelection:
    topology: str
    orientation: str
    column_roles: Mapping[int, str]
    confidence: float
    reason: str
    status: str


class TableAxisLLMAdjudicator:
    """Closed-set semantic-axis adjudicator.

    It only selects a topology, orientation and roles for existing columns. Raw
    table text, cells, values, units and material names are immutable.
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        base_url: str = "",
        api_key: str = "",
        model: str = "",
        cache_dir: Optional[Path] = None,
        timeout: float = 120.0,
        max_retries: int = 1,
    ) -> None:
        self.enabled = enabled
        self.base_url = (base_url or os.getenv("OPENAI_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "") or os.getenv("DASHSCOPE_API_KEY", "")
        self.model = model or os.getenv("OPENAI_MODEL", "") or os.getenv("DASHSCOPE_MODEL", "")
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.max_retries = max_retries
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    @property
    def available(self) -> bool:
        return bool(self.enabled and self.base_url and self.api_key and self.model)

    def adjudicate(
        self,
        *,
        table_id: str,
        heading: str,
        raw_table_text: str,
        current_topology: str,
        current_orientation: str,
        columns: Sequence[Mapping[str, object]],
        unresolved_reasons: Sequence[str],
    ) -> Optional[TableAxisSelection]:
        if not self.available or not columns:
            return None
        allowed_indices = {int(item["index"]) for item in columns}
        request_obj = {
            "task": "closed_set_table_axis_semantics",
            "constraints": [
                "Use only supplied topology, orientation and role labels.",
                "Assign roles only to supplied column indexes.",
                "Do not alter, normalize, infer, or generate any cell value, unit, material name, or condition value.",
                "Return unresolved by keeping unknown roles when evidence is insufficient.",
            ],
            "table_id": table_id,
            "heading": heading,
            "raw_table_text": raw_table_text,
            "current": {
                "topology": current_topology,
                "orientation": current_orientation,
                "unresolved_reasons": list(unresolved_reasons),
            },
            "allowed_topologies": sorted(_ALLOWED_TOPOLOGIES),
            "allowed_orientations": sorted(_ALLOWED_ORIENTATIONS),
            "allowed_column_roles": sorted(_ALLOWED_ROLES),
            "columns": list(columns),
            "response_schema": {
                "topology": "allowed topology",
                "orientation": "row_subject or column_subject",
                "column_roles": "object whose keys are supplied column indexes and values are allowed roles",
                "confidence": "0..1",
                "reason": "brief reason",
            },
        }
        payload = {
            "model": self.model,
            "temperature": 0.0,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a strict scientific table-axis adjudicator. Return JSON only. "
                        "You may classify existing axes but may never generate or modify scientific cell content."
                    ),
                },
                {"role": "user", "content": json.dumps(request_obj, ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
        }
        cache_file: Optional[Path] = None
        if self.cache_dir:
            key = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
            cache_file = self.cache_dir / f"{key}.json"
            if cache_file.exists():
                try:
                    return self._validate(json.loads(cache_file.read_text(encoding="utf-8")), allowed_indices, current_topology, current_orientation)
                except Exception:
                    pass
        url = self.base_url + "/chat/completions"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                try:
                    with urllib.request.urlopen(request, timeout=self.timeout) as response:
                        raw = json.loads(response.read().decode("utf-8"))
                except urllib.error.HTTPError as exc:
                    body = exc.read().decode("utf-8", errors="ignore")
                    if "response_format" in body or "json_object" in body:
                        fallback = dict(payload)
                        fallback.pop("response_format", None)
                        request = urllib.request.Request(
                            url,
                            data=json.dumps(fallback, ensure_ascii=False).encode("utf-8"),
                            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                            method="POST",
                        )
                        with urllib.request.urlopen(request, timeout=self.timeout) as response:
                            raw = json.loads(response.read().decode("utf-8"))
                    else:
                        raise
                data = json.loads(raw["choices"][0]["message"]["content"])
                result = self._validate(data, allowed_indices, current_topology, current_orientation)
                if cache_file:
                    cache_file.write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2), encoding="utf-8")
                return result
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(min(2 ** attempt, 8))
        return TableAxisSelection(current_topology, current_orientation, {}, 0.0, repr(last_error), "llm_failed")

    @staticmethod
    def _validate(
        data: Mapping[str, object],
        allowed_indices: set[int],
        current_topology: str,
        current_orientation: str,
    ) -> TableAxisSelection:
        topology = str(data.get("topology", "") or "")
        if topology not in _ALLOWED_TOPOLOGIES:
            topology = current_topology
        orientation = str(data.get("orientation", "") or "")
        if orientation not in _ALLOWED_ORIENTATIONS:
            orientation = current_orientation
        roles: dict[int, str] = {}
        raw_roles = data.get("column_roles", {})
        if isinstance(raw_roles, Mapping):
            for raw_index, raw_role in raw_roles.items():
                try:
                    index = int(raw_index)
                except Exception:
                    continue
                role = str(raw_role or "")
                if index in allowed_indices and role in _ALLOWED_ROLES:
                    roles[index] = role
        try:
            confidence = max(0.0, min(float(data.get("confidence", 0.0) or 0.0), 1.0))
        except Exception:
            confidence = 0.0
        return TableAxisSelection(topology, orientation, roles, confidence, str(data.get("reason", "") or ""), "selected" if roles else "no_change")


__all__ = ["TableAxisLLMAdjudicator", "TableAxisSelection"]
