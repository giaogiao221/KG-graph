from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence


@dataclass(frozen=True)
class FactAdjudication:
    action: str
    confidence: float
    supported_by_evidence: bool
    subject_valid: bool
    property_valid: bool
    value_valid: bool
    condition_valid: bool
    reason: str
    status: str
    rounds: int = 0


class ProductionFactAdjudicator:
    """Closed decision LLM for uncertain facts.

    The model may only release, keep as candidate, or reject the existing row.
    It cannot rewrite any scientific field.
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
        consensus_rounds: int = 2,
    ) -> None:
        self.enabled = enabled
        self.base_url = (base_url or os.getenv("OPENAI_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "") or os.getenv("DASHSCOPE_API_KEY", "")
        self.model = model or os.getenv("OPENAI_MODEL", "") or os.getenv("DASHSCOPE_MODEL", "")
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.max_retries = max_retries
        self.consensus_rounds = max(1, min(int(consensus_rounds), 3))
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    @property
    def available(self) -> bool:
        return bool(self.enabled and self.base_url and self.api_key and self.model)

    @staticmethod
    def _request_payload(row: Mapping[str, object], risk_reasons: Sequence[str], round_index: int) -> dict[str, object]:
        focus = "evidence_grounding" if round_index % 2 == 0 else "semantic_role_validity"
        return {
            "task": "closed_set_scientific_fact_release_decision",
            "focus": focus,
            "constraints": [
                "Do not rewrite or repair any field.",
                "Return action only from release, candidate, reject.",
                "Release only when the evidence directly supports subject, property, value and bound conditions.",
                "Reject structural headings, property-like subjects, grade-only subjects, axis inversions and malformed values.",
                "When uncertain, return candidate.",
            ],
            "fact": {
                "subject": row.get("主体名称"),
                "subject_type": row.get("主体类型"),
                "attribute_category": row.get("attribute_category"),
                "attribute_name": row.get("attribute_name"),
                "raw_property": row.get("predicate_raw"),
                "value_text": row.get("尾实体/取值文本"),
                "normalized_value": row.get("normalized_value_text"),
                "unit": row.get("normalized_unit") or row.get("单位"),
                "condition": row.get("条件文本"),
                "method": row.get("方法名称"),
                "source_type": row.get("来源类型"),
                "chapter": row.get("章节路径"),
                "table_title": row.get("所属表格标题"),
                "evidence": row.get("证据文本"),
                "deterministic_risks": list(risk_reasons),
            },
            "response_schema": {
                "action": "release|candidate|reject",
                "confidence": "0..1",
                "supported_by_evidence": True,
                "subject_valid": True,
                "property_valid": True,
                "value_valid": True,
                "condition_valid": True,
                "reason": "brief explanation",
            },
        }

    @staticmethod
    def _extract_json(text: str) -> Mapping[str, object]:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S)
        try:
            value = json.loads(text)
            return value if isinstance(value, dict) else {}
        except Exception:
            match = re.search(r"\{.*\}", text, flags=re.S)
            if not match:
                return {}
            value = json.loads(match.group(0))
            return value if isinstance(value, dict) else {}

    @staticmethod
    def _validate(data: Mapping[str, object]) -> FactAdjudication:
        action = str(data.get("action", "candidate") or "candidate").strip().lower()
        if action not in {"release", "candidate", "reject"}:
            action = "candidate"
        try:
            confidence = max(0.0, min(float(data.get("confidence", 0.0) or 0.0), 1.0))
        except Exception:
            confidence = 0.0
        supported = bool(data.get("supported_by_evidence", False))
        subject_valid = bool(data.get("subject_valid", False))
        property_valid = bool(data.get("property_valid", False))
        value_valid = bool(data.get("value_valid", False))
        condition_valid = bool(data.get("condition_valid", True))
        if action == "release" and not all((supported, subject_valid, property_valid, value_valid, condition_valid)):
            action = "candidate"
        return FactAdjudication(
            action=action,
            confidence=confidence,
            supported_by_evidence=supported,
            subject_valid=subject_valid,
            property_valid=property_valid,
            value_valid=value_valid,
            condition_valid=condition_valid,
            reason=str(data.get("reason", "") or ""),
            status="validated",
            rounds=1,
        )

    def _single_round(self, row: Mapping[str, object], risk_reasons: Sequence[str], round_index: int) -> FactAdjudication:
        request_obj = self._request_payload(row, risk_reasons, round_index)
        payload = {
            "model": self.model,
            "temperature": 0.0,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a strict scientific knowledge graph release auditor. "
                        "Return JSON only. Never repair the row. Release only if every field is directly grounded."
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
                    cached = json.loads(cache_file.read_text(encoding="utf-8"))
                    item = self._validate(cached)
                    return FactAdjudication(**{**asdict(item), "status": "cache_hit"})
                except Exception:
                    pass

        url = self.base_url + "/chat/completions"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                try:
                    with urllib.request.urlopen(request, timeout=self.timeout) as response:
                        raw = json.loads(response.read().decode("utf-8"))
                except urllib.error.HTTPError as exc:
                    error_body = exc.read().decode("utf-8", errors="ignore")
                    if "response_format" not in error_body and "json_object" not in error_body:
                        raise
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
                content = raw["choices"][0]["message"]["content"]
                data = self._extract_json(content)
                item = self._validate(data)
                if cache_file:
                    cache_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                return item
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(min(2**attempt, 8))
        return FactAdjudication(
            action="candidate",
            confidence=0.0,
            supported_by_evidence=False,
            subject_valid=False,
            property_valid=False,
            value_valid=False,
            condition_valid=False,
            reason=repr(last_error),
            status="llm_failed_closed",
            rounds=1,
        )

    def adjudicate(self, row: Mapping[str, object], risk_reasons: Sequence[str]) -> FactAdjudication:
        if not self.available:
            return FactAdjudication(
                action="candidate",
                confidence=0.0,
                supported_by_evidence=False,
                subject_valid=False,
                property_valid=False,
                value_valid=False,
                condition_valid=False,
                reason="llm_not_configured",
                status="not_available",
                rounds=0,
            )
        results = [self._single_round(row, risk_reasons, index) for index in range(self.consensus_rounds)]
        actions = [item.action for item in results]
        if all(action == "release" for action in actions):
            action = "release"
        elif any(action == "reject" for action in actions):
            action = "reject"
        else:
            action = "candidate"
        confidence = min(item.confidence for item in results) if results else 0.0
        return FactAdjudication(
            action=action,
            confidence=confidence,
            supported_by_evidence=all(item.supported_by_evidence for item in results),
            subject_valid=all(item.subject_valid for item in results),
            property_valid=all(item.property_valid for item in results),
            value_valid=all(item.value_valid for item in results),
            condition_valid=all(item.condition_valid for item in results),
            reason=" | ".join(item.reason for item in results if item.reason),
            status="consensus:" + ",".join(actions),
            rounds=len(results),
        )


__all__ = ["FactAdjudication", "ProductionFactAdjudicator"]
