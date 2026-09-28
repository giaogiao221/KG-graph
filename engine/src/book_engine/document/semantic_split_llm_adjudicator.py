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
class SemanticSplitSelection:
    split_after_indexes: tuple[int, ...]
    confidence: float
    reason: str
    status: str


class SemanticSplitLLMAdjudicator:
    """Closed-set splitter.

    The model can only choose boundaries from supplied candidate indexes. It is
    never allowed to rewrite, summarize, reorder, or generate source text.
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
        heading_path: Sequence[str],
        fragments: Sequence[str],
        candidate_boundaries: Sequence[int],
        subject_mentions: Mapping[int, Sequence[str]],
    ) -> Optional[SemanticSplitSelection]:
        if not self.available or not candidate_boundaries or len(fragments) < 2:
            return None
        allowed = sorted({int(i) for i in candidate_boundaries if 0 <= int(i) < len(fragments) - 1})
        if not allowed:
            return None
        request_obj = {
            "task": "closed_set_semantic_boundary_selection",
            "constraints": [
                "Only choose indexes from candidate_boundaries.",
                "Do not rewrite, merge, reorder, summarize, or generate text.",
                "Choose a boundary only when the subject or fact focus changes.",
                "Return an empty list when no reliable split is justified.",
            ],
            "heading_path": list(heading_path),
            "fragments": [{"index": i, "text": text, "subject_mentions": list(subject_mentions.get(i, ())) } for i, text in enumerate(fragments)],
            "candidate_boundaries": allowed,
            "response_schema": {
                "split_after_indexes": "list of candidate boundary indexes",
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
                        "You are a strict scientific text boundary adjudicator. Return JSON only. "
                        "You may select only supplied boundary indexes and may not rewrite the source."
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
                    return self._validate(json.loads(cache_file.read_text(encoding="utf-8")), allowed)
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
                result = self._validate(data, allowed)
                if cache_file and result:
                    cache_file.write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2), encoding="utf-8")
                return result
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(min(2 ** attempt, 8))
        return SemanticSplitSelection((), 0.0, repr(last_error), "llm_failed")

    @staticmethod
    def _validate(data: Mapping[str, object], allowed: Sequence[int]) -> SemanticSplitSelection:
        allowed_set = set(allowed)
        raw = data.get("split_after_indexes", [])
        indexes: list[int] = []
        if isinstance(raw, list):
            for item in raw:
                try:
                    value = int(item)
                except Exception:
                    continue
                if value in allowed_set and value not in indexes:
                    indexes.append(value)
        try:
            confidence = max(0.0, min(float(data.get("confidence", 0.0) or 0.0), 1.0))
        except Exception:
            confidence = 0.0
        return SemanticSplitSelection(
            split_after_indexes=tuple(sorted(indexes)),
            confidence=confidence,
            reason=str(data.get("reason", "") or ""),
            status="selected" if indexes else "no_split",
        )


__all__ = ["SemanticSplitLLMAdjudicator", "SemanticSplitSelection"]
