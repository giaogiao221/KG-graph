from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class QwenResponse:
    data: Mapping[str, Any]
    raw_content: str
    request_id: str
    usage: Mapping[str, Any]
    cached: bool
    attempts: int


class QwenClientError(RuntimeError):
    pass


class QwenOpenAIClient:
    """Small dependency-free OpenAI-compatible client for Alibaba Cloud Model Studio.

    qwen3-max structured output is used in non-thinking mode. Requests are cached
    by their complete body, so interrupted corpus runs can resume without paying
    for completed calls again.
    """

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "",
        model: str = "qwen3-max",
        cache_dir: Path | None = None,
        timeout: float = 180.0,
        max_retries: int = 6,
        min_interval_seconds: float = 0.2,
    ) -> None:
        # v106 follows the project's existing OpenAI-compatible environment
        # contract exclusively. No QWEN_* or DASHSCOPE_* variables are read.
        self.api_key = (api_key or os.getenv("OPENAI_API_KEY", "")).strip()
        self.base_url = (base_url or os.getenv("OPENAI_BASE_URL", "")).strip().rstrip("/")
        self.model = (model or os.getenv("OPENAI_MODEL", "") or "qwen3-max").strip()
        self.cache_dir = cache_dir
        self.timeout = float(timeout)
        self.max_retries = int(max_retries)
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._last_call_at = 0.0
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    @property
    def available(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    def assert_configured(self) -> None:
        missing = []
        if not self.api_key:
            missing.append("OPENAI_API_KEY")
        if not self.base_url:
            missing.append("OPENAI_BASE_URL")
        if not self.model:
            missing.append("OPENAI_MODEL")
        if missing:
            raise QwenClientError("Missing Qwen configuration: " + ", ".join(missing))

    @staticmethod
    def _extract_json(content: str) -> Mapping[str, Any]:
        text = (content or "").strip()
        if not text:
            raise QwenClientError("Model returned empty content")
        try:
            value = json.loads(text)
            if isinstance(value, Mapping):
                return value
        except Exception:
            pass
        fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.I | re.S)
        if fenced:
            try:
                value = json.loads(fenced.group(1))
                if isinstance(value, Mapping):
                    return value
            except Exception:
                pass
        decoder = json.JSONDecoder()
        for index, char in enumerate(text):
            if char != "{":
                continue
            try:
                value, _ = decoder.raw_decode(text[index:])
                if isinstance(value, Mapping):
                    return value
            except Exception:
                continue
        raise QwenClientError("Model response does not contain a valid JSON object")

    def _cache_path(self, payload: Mapping[str, Any]) -> Path | None:
        if not self.cache_dir:
            return None
        digest = hashlib.sha256(
            json.dumps({"base_url": self.base_url, "payload": payload}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return self.cache_dir / f"{digest}.json"

    def chat_json(
        self,
        *,
        system: str,
        user: Mapping[str, Any] | str,
        max_tokens: int = 8192,
        temperature: float = 0.0,
        request_tag: str = "",
    ) -> QwenResponse:
        self.assert_configured()
        user_content = user if isinstance(user, str) else json.dumps(user, ensure_ascii=False)
        # The API requires the word JSON when response_format=json_object is used.
        if "json" not in (system + " " + user_content).lower():
            system = system.rstrip() + " Return one valid JSON object only."
        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": temperature,
            "max_tokens": int(max_tokens),
            "enable_thinking": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            "response_format": {"type": "json_object"},
        }
        cache_path = self._cache_path(payload)
        if cache_path and cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                return QwenResponse(
                    data=cached["data"],
                    raw_content=str(cached.get("raw_content", "")),
                    request_id=str(cached.get("request_id", "cache")),
                    usage=cached.get("usage", {}),
                    cached=True,
                    attempts=0,
                )
            except Exception:
                cache_path.unlink(missing_ok=True)

        endpoint = self.base_url + "/chat/completions"
        last_error: Exception | None = None
        active_payload = dict(payload)
        for attempt in range(self.max_retries + 1):
            try:
                wait = self.min_interval_seconds - (time.time() - self._last_call_at)
                if wait > 0:
                    time.sleep(wait)
                req = urllib.request.Request(
                    endpoint,
                    data=json.dumps(active_payload, ensure_ascii=False).encode("utf-8"),
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        "X-DashScope-Client": "kg-v106-qwen3-max",
                    },
                    method="POST",
                )
                self._last_call_at = time.time()
                try:
                    with urllib.request.urlopen(req, timeout=self.timeout) as response:
                        response_text = response.read().decode("utf-8", errors="replace")
                        raw = json.loads(response_text)
                except urllib.error.HTTPError as exc:
                    body = exc.read().decode("utf-8", errors="replace")
                    status = int(getattr(exc, "code", 0) or 0)
                    lower = body.lower()
                    # Region/model combinations can differ slightly. Fall back
                    # without losing the strict JSON parser in this client.
                    changed = False
                    if status == 400 and "response_format" in lower and "response_format" in active_payload:
                        active_payload = dict(active_payload)
                        active_payload.pop("response_format", None)
                        changed = True
                    if status == 400 and "enable_thinking" in lower and "enable_thinking" in active_payload:
                        active_payload = dict(active_payload)
                        active_payload.pop("enable_thinking", None)
                        changed = True
                    if changed:
                        continue
                    retryable = status in {408, 409, 429, 500, 502, 503, 504}
                    if not retryable:
                        raise QwenClientError(f"HTTP {status}: {body[:1200]}") from exc
                    raise QwenClientError(f"Retryable HTTP {status}: {body[:1200]}") from exc

                choices = raw.get("choices") or []
                if not choices:
                    raise QwenClientError(f"No choices in response: {json.dumps(raw, ensure_ascii=False)[:1200]}")
                message = choices[0].get("message") or {}
                content = str(message.get("content", "") or "")
                data = self._extract_json(content)
                result = QwenResponse(
                    data=data,
                    raw_content=content,
                    request_id=str(raw.get("id", "") or raw.get("request_id", "")),
                    usage=raw.get("usage", {}) or {},
                    cached=False,
                    attempts=attempt + 1,
                )
                if cache_path:
                    temp = cache_path.with_suffix(".tmp")
                    temp.write_text(
                        json.dumps(
                            {
                                "request_tag": request_tag,
                                "data": data,
                                "raw_content": content,
                                "request_id": result.request_id,
                                "usage": result.usage,
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                    temp.replace(cache_path)
                return result
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt >= self.max_retries:
                    break
                delay = min(60.0, (2.0 ** attempt) + random.random())
                time.sleep(delay)
        raise QwenClientError(f"Qwen request failed after retries: {last_error!r}")


__all__ = ["QwenOpenAIClient", "QwenResponse", "QwenClientError"]
