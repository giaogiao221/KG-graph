from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence


@dataclass(frozen=True)
class LLMSelection:
    selected_property_id: str
    confidence: float
    evidence_span: str
    reason: str
    status: str


class PropertyLLMAdjudicator:
    """Closed-set LLM adjudicator.

    The model may only select one supplied property_id or return "unresolved".
    It cannot edit the subject, value, unit, condition, or invent a property.
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
        max_retries: int = 2,
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

    def build_request(self, row: Mapping[str, object], candidates: Sequence[Mapping[str, object]]) -> Dict[str, object]:
        return {
            "task": "closed_set_property_alignment",
            "constraints": [
                "Only select a property_id from candidates or unresolved.",
                "Do not modify subject, value, unit, conditions, or evidence.",
                "Use the evidence span and semantic context only.",
            ],
            "fact": {
                "raw_property": row.get("predicate_raw") or row.get("attribute_name"),
                "subject": row.get("主体名称"),
                "subject_type": row.get("主体类型"),
                "value_text": row.get("尾实体/取值文本"),
                "unit": row.get("normalized_unit") or row.get("单位"),
                "conditions": row.get("条件文本"),
                "method": row.get("方法名称"),
                "source_type": row.get("来源类型"),
                "chapter": row.get("章节路径"),
                "table_title": row.get("所属表格标题"),
                "evidence": row.get("证据文本"),
            },
            "candidates": list(candidates),
            "response_schema": {
                "selected_property_id": "candidate property_id or unresolved",
                "confidence": "0..1",
                "evidence_span": "short exact span from provided evidence",
                "reason": "brief reason",
            },
        }

    def adjudicate(self, row: Mapping[str, object], candidates: Sequence[Mapping[str, object]]) -> Optional[LLMSelection]:
        if not self.available or not candidates:
            return None
        request_obj = self.build_request(row, candidates)
        system = (
            "You are a strict scientific property alignment adjudicator. "
            "Return JSON only. You must choose one supplied property_id or unresolved. "
            "Never invent a property and never alter the source fact."
        )
        payload = {
            "model": self.model,
            "temperature": 0.0,
            "messages": [
                {"role": "system", "content": system},
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
                    data = json.loads(cache_file.read_text(encoding="utf-8"))
                    return self._validate(data, candidates)
                except Exception:
                    pass

        url = self.base_url + "/chat/completions"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
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
                content = raw["choices"][0]["message"]["content"]
                data = json.loads(content)
                selection = self._validate(data, candidates)
                if cache_file and selection:
                    cache_file.write_text(json.dumps(asdict(selection), ensure_ascii=False, indent=2), encoding="utf-8")
                return selection
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(min(2 ** attempt, 8))
        return LLMSelection("unresolved", 0.0, "", repr(last_error), "llm_failed")

    @staticmethod
    def _validate(data: Mapping[str, object], candidates: Sequence[Mapping[str, object]]) -> Optional[LLMSelection]:
        allowed = {str(item.get("property_id", "")) for item in candidates}
        selected = str(data.get("selected_property_id", "") or "").strip()
        if selected not in allowed and selected != "unresolved":
            selected = "unresolved"
        try:
            confidence = max(0.0, min(float(data.get("confidence", 0.0) or 0.0), 1.0))
        except Exception:
            confidence = 0.0
        return LLMSelection(
            selected_property_id=selected or "unresolved",
            confidence=confidence,
            evidence_span=str(data.get("evidence_span", "") or ""),
            reason=str(data.get("reason", "") or ""),
            status="selected" if selected and selected != "unresolved" else "unresolved",
        )


__all__ = ["LLMSelection", "PropertyLLMAdjudicator"]
