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
class SubjectAnchorSelection:
    selected_subject_id: str
    confidence: float
    evidence_span: str
    reason: str
    status: str


class SubjectAnchorLLMAdjudicator:
    """Closed-set subject anchor adjudicator.

    It may only choose one supplied subject_id or return ``unresolved``. It may
    not invent, rename, merge, or rewrite a scientific entity.
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
        text: str,
        candidates: Sequence[Mapping[str, object]],
        previous_subject_id: str = "",
    ) -> Optional[SubjectAnchorSelection]:
        if not self.available or not candidates:
            return None
        allowed = {str(item.get("subject_id", "") or "") for item in candidates if item.get("subject_id")}
        if not allowed:
            return None
        request_obj = {
            "task": "closed_set_subject_anchor_selection",
            "constraints": [
                "Choose exactly one supplied subject_id or unresolved.",
                "Do not invent, rename, merge, or rewrite a subject.",
                "Use explicit local evidence before heading inheritance.",
                "A pronoun may inherit only when the antecedent is unambiguous.",
                "When a sentence compares multiple entities and no single subject owns the whole text, return unresolved.",
            ],
            "block_id": block_id,
            "heading_path": list(heading_path),
            "text": text,
            "previous_subject_id": previous_subject_id,
            "candidates": list(candidates),
            "response_schema": {
                "selected_subject_id": "candidate subject_id or unresolved",
                "confidence": "0..1",
                "evidence_span": "exact short span from text or heading_path",
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
                        "You are a strict scientific entity anchor adjudicator. Return JSON only. "
                        "Choose only a supplied subject_id or unresolved. Never generate a new entity."
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
                    return self._validate(json.loads(cache_file.read_text(encoding="utf-8")), allowed, text, heading_path)
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
                selection = self._validate(data, allowed, text, heading_path)
                if cache_file:
                    cache_file.write_text(json.dumps(asdict(selection), ensure_ascii=False, indent=2), encoding="utf-8")
                return selection
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(min(2 ** attempt, 8))
        return SubjectAnchorSelection("unresolved", 0.0, "", repr(last_error), "llm_failed")

    @staticmethod
    def _validate(
        data: Mapping[str, object],
        allowed: set[str],
        text: str,
        heading_path: Sequence[str],
    ) -> SubjectAnchorSelection:
        selected = str(data.get("selected_subject_id", "") or "").strip()
        if selected not in allowed and selected != "unresolved":
            selected = "unresolved"
        try:
            confidence = max(0.0, min(float(data.get("confidence", 0.0) or 0.0), 1.0))
        except Exception:
            confidence = 0.0
        evidence_span = str(data.get("evidence_span", "") or "").strip()
        context = text + "\n" + " > ".join(heading_path)
        if evidence_span and evidence_span not in context:
            evidence_span = ""
            confidence = min(confidence, 0.60)
        return SubjectAnchorSelection(
            selected_subject_id=selected or "unresolved",
            confidence=confidence,
            evidence_span=evidence_span,
            reason=str(data.get("reason", "") or ""),
            status="selected" if selected and selected != "unresolved" else "unresolved",
        )


__all__ = ["SubjectAnchorLLMAdjudicator", "SubjectAnchorSelection"]
