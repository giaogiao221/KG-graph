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


@dataclass(frozen=True)
class ProcessStepSelection:
    selected_candidate_ids: tuple[str, ...]
    confidence: float
    reason: str
    status: str


class ProcessStepLLMAdjudicator:
    """Closed-set process-step adjudicator.

    The model may only select and order supplied candidate IDs. Candidate text,
    scientific values, materials, conditions and source order are immutable.
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
        block_id: str,
        heading_path: Sequence[str],
        process_subject: str,
        source_text: str,
        candidates: Sequence[Mapping[str, object]],
    ) -> Optional[ProcessStepSelection]:
        if not self.available or len(candidates) < 2:
            return None
        candidate_by_id = {
            str(item.get("candidate_id", "") or ""): item
            for item in candidates
            if item.get("candidate_id")
        }
        allowed = set(candidate_by_id)
        if len(allowed) < 2:
            return None
        request_obj = {
            "task": "closed_set_process_step_selection",
            "constraints": [
                "Select only supplied candidate_id values or return an empty list.",
                "Keep candidates in their original source order.",
                "Do not invent, rewrite, merge or split candidate text.",
                "Do not change materials, values, units, conditions, methods or actions.",
                "Select only concrete operations; reject principles, properties, results discussion and table-of-contents items.",
            ],
            "block_id": block_id,
            "heading_path": list(heading_path),
            "process_subject": process_subject,
            "source_text": source_text,
            "candidates": list(candidates),
            "response_schema": {
                "selected_candidate_ids": "ordered list of supplied candidate_id values",
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
                        "You are a strict scientific process-step adjudicator. Return JSON only. "
                        "Choose only supplied candidate IDs and preserve source order."
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
                    return self._validate(json.loads(cache_file.read_text(encoding="utf-8")), candidate_by_id)
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
                result = self._validate(data, candidate_by_id)
                if cache_file:
                    cache_file.write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2), encoding="utf-8")
                return result
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(min(2 ** attempt, 8))
        return ProcessStepSelection((), 0.0, repr(last_error), "llm_failed")

    @staticmethod
    def _validate(
        data: Mapping[str, object],
        candidate_by_id: Mapping[str, Mapping[str, object]],
    ) -> ProcessStepSelection:
        raw = data.get("selected_candidate_ids", [])
        selected: list[str] = []
        if isinstance(raw, list):
            for item in raw:
                value = str(item or "")
                if value in candidate_by_id and value not in selected:
                    selected.append(value)
        selected.sort(key=lambda item: int(candidate_by_id[item].get("source_index", 0) or 0))
        try:
            confidence = max(0.0, min(float(data.get("confidence", 0.0) or 0.0), 1.0))
        except Exception:
            confidence = 0.0
        return ProcessStepSelection(
            selected_candidate_ids=tuple(selected),
            confidence=confidence,
            reason=str(data.get("reason", "") or ""),
            status="selected" if len(selected) >= 2 else "unresolved",
        )


__all__ = ["ProcessStepLLMAdjudicator", "ProcessStepSelection"]
