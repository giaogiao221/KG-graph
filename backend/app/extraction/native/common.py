from __future__ import annotations

import csv
import hashlib
import hmac
import http.client
import ipaddress
import io
import json
import os
import re
import stat
import socket
import ssl
import time
from contextlib import contextmanager
from urllib.parse import urlsplit
from datetime import UTC, datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Protocol, Sequence

from app.extraction.contracts import AdapterContext, AdapterResult, PermanentAdapterError, ProgressEvent, ProgressSink, RetryableAdapterError
from app.facts.schema59 import (
    FIELD,
    SCHEMA59_COLUMNS,
    canonical_evidence,
    evidence_hash,
)
from app.extraction.native.pure_llm_prompts import prompt_for

MAX_DOCUMENT_BYTES = 2_000_000
# KGchouqu preserves the original 59-column script row inside the platform
# row for lossless export, so legitimate hybrid artifacts can exceed 2 MB.
MAX_ROUTE_TSV_BYTES = 32_000_000
MAX_CANDIDATE_ROWS = 10_000
MAX_FIELD_CHARS = 32_000
MAX_MODEL_RESPONSE_BYTES = 1_000_000
ROUTES = frozenset({"rule_text", "rule_table", "llm_text", "llm_table"})

CANDIDATE_BUSINESS_FIELDS = frozenset({
    "graph_fact_key", "book_title", "subject", "property", "value", "unit",
    "condition", "table_id", "table_row", "table_column", "source_locator",
    "source_kind", "evidence_text", "model_call_id",
})


class ModelConfigurationError(PermanentAdapterError):
    pass

@dataclass(frozen=True, slots=True)
class TableUnit:
    identifier: str
    cells: tuple[tuple[str, ...], ...]
    def markdown(self) -> str:
        if not self.cells: return ""
        width = max(len(row) for row in self.cells)
        rows = [tuple(row) + ("",) * (width-len(row)) for row in self.cells]
        return "\n".join(["| " + " | ".join(rows[0]) + " |", "| " + " | ".join("---" for _ in range(width)) + " |", *("| " + " | ".join(row) + " |" for row in rows[1:])])

@dataclass(frozen=True, slots=True)
class DocumentContent:
    text: str
    tables: tuple[TableUnit, ...] = ()

@dataclass(frozen=True, slots=True)
class ModelResult:
    candidates: tuple[Mapping[str, object], ...]
    metrics: Mapping[str, int]
    model_call_id: str | None = None


class PersistentUnitCache:
    """Small, immutable, cross-attempt model result cache."""

    def __init__(self, root: Path, execution_key: str, route: str):
        namespace = hashlib.sha256(f"{execution_key}\0{route}".encode()).hexdigest()
        self.root = root.absolute()
        self.directory = self.root / namespace
        self._hmac_secret = self._resolve_hmac_secret()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            assert_no_links(self.root)
            from app.extraction.process_runner import ProcessRunner
            ProcessRunner._secure_private_directory(self.root)
            self.directory.mkdir(mode=0o700, exist_ok=True)
            assert_no_links(self.directory)
            ProcessRunner._secure_private_directory(self.directory)
        except Exception:
            raise PermanentAdapterError("native unit cache is invalid") from None

    @staticmethod
    def _resolve_hmac_secret() -> bytes:
        name = os.environ.get("EXTRACTION_CACHE_HMAC_KEY_ENV", "").strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", name):
            raise ModelConfigurationError("native unit cache is not configured")
        secret = os.environ.get(name, "").encode("utf-8")
        if len(secret) < 32:
            raise ModelConfigurationError("native unit cache is not configured")
        return secret

    def _path(self, unit_key: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", unit_key):
            raise PermanentAdapterError("native unit cache is invalid")
        return self.directory / f"{unit_key}.json"

    @staticmethod
    def _payload(result: ModelResult) -> dict[str, object]:
        candidates = [dict(value) for value in result.candidates]
        metrics = dict(result.metrics)
        metrics.setdefault("cached_tokens", 0)
        if (
            len(candidates) > MAX_CANDIDATE_ROWS
            or set(metrics) != {"prompt_tokens", "completion_tokens", "cached_tokens", "total_tokens", "model_calls"}
            or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 100_000_000 for value in metrics.values())
            or metrics["total_tokens"] != metrics["prompt_tokens"] + metrics["completion_tokens"] + metrics["cached_tokens"]
            or any(
                not isinstance(candidate, dict)
                or len(candidate) > 64
                or any(
                    not isinstance(key, str)
                    or len(key) > 128
                    or not isinstance(value, str)
                    or len(value) > MAX_FIELD_CHARS
                    or "\x00" in value
                    for key, value in candidate.items()
                )
                for candidate in candidates
            )
        ):
            raise PermanentAdapterError("native unit cache is invalid")
        return {"candidates": candidates, "metrics": metrics}

    def _read(self, path: Path) -> ModelResult | None:
        if not path.exists():
            return None
        try:
            assert_no_links(path)
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_MODEL_RESPONSE_BYTES:
                raise ValueError
            envelope = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(envelope, dict) or set(envelope) != {"schema", "hmac_sha256", "payload"} or envelope["schema"] != "native-unit-cache-v1":
                raise ValueError
            payload_bytes = json.dumps(envelope["payload"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            expected = hmac.new(self._hmac_secret, payload_bytes, hashlib.sha256).hexdigest()
            if not isinstance(envelope["hmac_sha256"], str) or not hmac.compare_digest(expected, envelope["hmac_sha256"]):
                raise ValueError
            payload = envelope["payload"]
            result = ModelResult(tuple(payload["candidates"]), payload["metrics"])
            checked = self._payload(result)
            return ModelResult(tuple(checked["candidates"]), checked["metrics"])  # type: ignore[arg-type]
        except (OSError, UnicodeError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            raise PermanentAdapterError("native unit cache is invalid") from None

    @contextmanager
    def _lock(self, path: Path):
        lock = path.with_suffix(".lock")
        deadline = time.monotonic() + 30
        handle = lock.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        acquired = False
        while not acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise RetryableAdapterError("native unit cache is busy") from None
                time.sleep(0.01)
        try:
            yield
        finally:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def get_or_compute(self, unit_key: str, compute, on_cache_hit=None) -> ModelResult:
        path = self._path(unit_key)
        cached = self._read(path)
        if cached is not None:
            return on_cache_hit(unit_key, cached) if on_cache_hit is not None else cached
        with self._lock(path):
            cached = self._read(path)
            if cached is not None:
                return on_cache_hit(unit_key, cached) if on_cache_hit is not None else cached
            result = compute()
            payload = self._payload(result)
            payload_bytes = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            envelope = {"schema": "native-unit-cache-v1", "hmac_sha256": hmac.new(self._hmac_secret, payload_bytes, hashlib.sha256).hexdigest(), "payload": payload}
            data = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            if len(data) > MAX_MODEL_RESPONSE_BYTES:
                raise PermanentAdapterError("native unit cache is invalid")
            temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            try:
                with temporary.open("xb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, path)
                os.chmod(path, 0o400)
            except OSError:
                temporary.unlink(missing_ok=True)
                raise RetryableAdapterError("native unit cache is unavailable") from None
            return ModelResult(tuple(payload["candidates"]), payload["metrics"], result.model_call_id)  # type: ignore[arg-type]

class DocumentReader(Protocol):
    def read(self, path: Path) -> DocumentContent: ...
class RuleEngine(Protocol):
    def extract_text(self, document: DocumentContent) -> Iterable[Mapping[str, object]]: ...
    def extract_table(self, table: TableUnit) -> Iterable[Mapping[str, object]]: ...
class ModelClient(Protocol):
    def extract(self, *, route: str, payload: str, idempotency_key: str, model_config_id: str, prompt_snapshot: object | None = None) -> ModelResult | Iterable[Mapping[str, object]]: ...
    def record_cache_hit(self, *, route: str, idempotency_key: str, model_config_id: str) -> str | None: ...

def _fail(message: str = "native adapter input is invalid") -> None:
    raise PermanentAdapterError(message)

def assert_no_links(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    try:
        for part in absolute.parts[1:]:
            current /= part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400):
                _fail()
    except PermanentAdapterError: raise
    except OSError: raise PermanentAdapterError("native adapter input is invalid") from None

def _assert_safe_file(path: Path, allowed_root: Path, max_bytes: int) -> None:
    try:
        assert_no_links(allowed_root)
        assert_no_links(path)
        root = allowed_root.resolve(strict=True)
        absolute = path.absolute()
        if root != absolute and root not in absolute.parents: _fail()
        current = root
        for part in absolute.relative_to(root).parts:
            current = current / part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode): _fail()
        resolved = absolute.resolve(strict=True)
        if root != resolved and root not in resolved.parents: _fail()
        if not resolved.is_file() or resolved.stat().st_size > max_bytes: _fail()
    except PermanentAdapterError: raise
    except OSError: raise PermanentAdapterError("native adapter input is invalid") from None

class MarkdownDocumentReader:
    def __init__(self, allowed_root: Path | None = None): self.allowed_root = allowed_root
    def read(self, path: Path) -> DocumentContent:
        root = self.allowed_root or path.absolute().parent
        _assert_safe_file(path, root, MAX_DOCUMENT_BYTES)
        try:
            raw = path.read_bytes()
            if len(raw) > MAX_DOCUMENT_BYTES: _fail()
            text = raw.decode("utf-8")
        except PermanentAdapterError: raise
        except (OSError, UnicodeError): raise PermanentAdapterError("native adapter input is invalid") from None
        return DocumentContent(text, _markdown_tables(text))

def _markdown_tables(text: str) -> tuple[TableUnit, ...]:
    tables, current = [], []
    for line in (*text.splitlines(), ""):
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = tuple(cell.strip() for cell in stripped[1:-1].split("|"))
            if all(cell and set(cell) <= {"-", ":"} for cell in cells): continue
            current.append(cells)
        elif current:
            tables.append(TableUnit(f"table-{len(tables)+1}", tuple(current))); current = []
    return tuple(tables)

class BuiltinRuleEngine:
    _fact = re.compile(r"(?P<subject>[A-Za-z][\w .-]{0,100}?)\s+(?:has|is|=)\s+(?P<property>[A-Za-z][\w -]{0,80}?)\s+(?P<value>-?\d+(?:\.\d+)?)\s*(?P<unit>[A-Za-z%/0-9³²^-]{0,24})", re.I)
    def extract_text(self, document: DocumentContent) -> Iterable[Mapping[str, object]]:
        for index, sentence in enumerate(re.split(r"(?<=[.!?])\s+|\n+", document.text)):
            match = self._fact.search(sentence.strip())
            if match:
                data = match.groupdict(); subject=data["subject"].strip(); prop=data["property"].strip()
                yield {"graph_fact_key": f"{subject.casefold()}:{prop.casefold()}", **data, "subject":subject, "property":prop, "evidence_text":sentence.strip(), "source_locator":f"sentence-{index+1}", "source_kind":"text"}
    def extract_table(self, table: TableUnit) -> Iterable[Mapping[str, object]]:
        if len(table.cells) < 2 or len(table.cells[0]) < 2: return
        headers = table.cells[0]
        for row_index, row in enumerate(table.cells[1:], 1):
            subject = row[0].strip() if row else ""
            for column_index in range(1, min(len(headers), len(row))):
                prop, value = headers[column_index].strip(), row[column_index].strip()
                if subject and prop and value:
                    yield {"graph_fact_key":f"{subject.casefold()}:{prop.casefold()}", "subject":subject, "property":prop, "value":value, "evidence_text":" | ".join(row), "source_locator":f"{table.identifier}:r{row_index}:c{column_index}", "source_kind":"table", "table_id":table.identifier, "table_row":row_index, "table_column":column_index}

BasicRuleEngine = BuiltinRuleEngine

@dataclass(frozen=True, slots=True)
class _ResolvedModelConfiguration:
    scheme: str
    host: str
    port: int
    target: str
    model: str
    secret: str
    supports_idempotency: bool
    addresses: frozenset[str]


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, pinned_ip: str, port: int, allowed_ips: frozenset[str], timeout: float):
        super().__init__(host, port=port, timeout=timeout)
        self._pinned_ip = pinned_ip
        self._allowed_ips = allowed_ips

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout, self.source_address)
        peer = str(ipaddress.ip_address(self.sock.getpeername()[0]))
        if peer not in self._allowed_ips:
            self.close()
            raise OSError("model peer identity changed")


class _PinnedHTTPSConnection(_PinnedHTTPConnection):
    def connect(self) -> None:
        raw = socket.create_connection((self._pinned_ip, self.port), self.timeout, self.source_address)
        peer = str(ipaddress.ip_address(raw.getpeername()[0]))
        if peer not in self._allowed_ips:
            raw.close()
            raise OSError("model peer identity changed")
        try:
            self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host)
            peer = str(ipaddress.ip_address(self.sock.getpeername()[0]))
            if peer not in self._allowed_ips:
                raise OSError("model peer identity changed")
        except BaseException:
            raw.close()
            raise


def resolve_model_configuration(*, endpoint: str, model: str, secret: str,
                                allowed_hosts: Sequence[str], allow_private_network: bool,
                                allow_insecure_http: bool, provider_supports_idempotency: bool) -> _ResolvedModelConfiguration:
    try:
        if not all(isinstance(v, str) and v for v in (endpoint, model, secret)): raise ValueError
        parsed = urlsplit(endpoint); host = (parsed.hostname or "").rstrip(".").casefold()
        if parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise ValueError
        if not isinstance(allowed_hosts, (list, tuple)) or host not in {str(v).rstrip(".").casefold() for v in allowed_hosts}: raise ValueError
        if parsed.scheme != "https" and not (parsed.scheme == "http" and allow_insecure_http is True): raise ValueError
        if parsed.port not in (None, 80, 443) and not allow_private_network is True: raise ValueError
        private = allow_private_network
        supports_idempotency = provider_supports_idempotency
        if not isinstance(private, bool) or not isinstance(supports_idempotency, bool): raise ValueError
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses: set[str] = set()
        for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
            address = ipaddress.ip_address(item[4][0])
            if not private and not address.is_global: raise ValueError
            addresses.add(str(address))
        if not addresses:
            raise ValueError
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query
        return _ResolvedModelConfiguration(parsed.scheme, host, port, target, model, secret, supports_idempotency, frozenset(addresses))
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
        raise ModelConfigurationError("native model client is not configured") from None

def _model_configuration(config_id: str) -> _ResolvedModelConfiguration:
    """Temporary compatibility for standalone adapter tests; workers use DB config."""
    try:
        config = json.loads(os.environ.get("EXTRACTION_MODEL_CONFIGS_JSON", "{}"))[config_id]
        key_env = config["api_key_env"]
        if not isinstance(key_env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", key_env): raise ValueError
        return resolve_model_configuration(endpoint=config["endpoint"], model=config["model"], secret=os.environ[key_env],
            allowed_hosts=config["allowed_hosts"], allow_private_network=config["allow_private_network"],
            allow_insecure_http=config.get("allow_insecure_http", False), provider_supports_idempotency=config["provider_supports_idempotency"])
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
        raise ModelConfigurationError("native model client is not configured") from None

class OpenAICompatibleModelClient:
    def __init__(self, timeout_seconds: float = 120.0, *, configuration_resolver=None, observer=None, transport_attempts: int = 3, retry_backoff_seconds: float = 0.5):
        self.timeout_seconds = timeout_seconds; self.configuration_resolver = configuration_resolver or _model_configuration; self.observer = observer
        self.transport_attempts = max(1, min(int(transport_attempts), 5)); self.retry_backoff_seconds = max(0.0, min(float(retry_backoff_seconds), 10.0))
    def extract(self, *, route: str, payload: str, idempotency_key: str, model_config_id: str, prompt_snapshot: object | None = None) -> ModelResult:
        last_error: RetryableAdapterError | None = None
        for attempt in range(self.transport_attempts):
            try:
                return self._extract_once(route=route, payload=payload, idempotency_key=idempotency_key, model_config_id=model_config_id, prompt_snapshot=prompt_snapshot)
            except RetryableAdapterError as error:
                last_error = error
                if attempt + 1 < self.transport_attempts and self.retry_backoff_seconds:
                    time.sleep(self.retry_backoff_seconds * (2 ** attempt))
        assert last_error is not None
        raise last_error

    def _extract_once(self, *, route: str, payload: str, idempotency_key: str, model_config_id: str, prompt_snapshot: object | None = None) -> ModelResult:
        config = self.configuration_resolver(model_config_id)
        called_at = datetime.now(UTC)
        instruction = prompt_for(route, prompt_snapshot)
        request_body = json.dumps({"model":config.model, "messages":[{"role":"system","content":instruction},{"role":"user","content":payload}]}, separators=(",", ":")).encode()
        headers = {"Authorization": f"Bearer {config.secret}", "Content-Type": "application/json"}
        if config.supports_idempotency:
            headers["Idempotency-Key"] = idempotency_key
        pinned_ip = sorted(config.addresses)[0]
        connection_type = _PinnedHTTPSConnection if config.scheme == "https" else _PinnedHTTPConnection
        connection = connection_type(config.host, pinned_ip, config.port, config.addresses, self.timeout_seconds)
        try:
            target = config.target if config.target.rstrip("/").endswith("/chat/completions") else f"{config.target.rstrip('/')}/chat/completions"
            connection.request("POST", target, body=request_body, headers=headers)
            response = connection.getresponse()
            if response.status >= 500:
                raise RetryableAdapterError("model dependency unavailable")
            if response.status < 200 or response.status >= 300:
                raise PermanentAdapterError("native model response is invalid")
            raw = response.read(MAX_MODEL_RESPONSE_BYTES + 1)
            if len(raw) > MAX_MODEL_RESPONSE_BYTES: raise ValueError
            payload_json = json.loads(raw.decode("utf-8"))
            content = payload_json["choices"][0]["message"]["content"]
            if not isinstance(content, str): raise ValueError
            # DashScope and other OpenAI-compatible providers may wrap a valid
            # JSON answer in a Markdown fence despite the system instruction.
            # Accept only the array inside that fence; never parse prose around it.
            match = re.fullmatch(r"\s*```(?:json)?\s*(\[.*\])\s*```\s*", content, re.DOTALL | re.IGNORECASE)
            if match is None:
                # Some compatible providers prepend a short explanation before
                # the requested JSON. Extract one balanced JSON array only.
                start = content.find("[")
                end = content.rfind("]")
                candidate_text = content[start:end + 1] if start >= 0 and end > start else content
            else:
                candidate_text = match.group(1)
            candidates = json.loads(candidate_text)
            usage = payload_json["usage"]
            raw_prompt, completion, total = [usage[name] for name in ("prompt_tokens","completion_tokens","total_tokens")]
            details = usage.get("prompt_tokens_details", {})
            if not isinstance(details, dict): raise ValueError
            cached = details.get("cached_tokens", 0)
            values=[raw_prompt,completion,cached,total]
            if any(isinstance(v,bool) or not isinstance(v,int) or v<0 or v>100_000_000 for v in values) or cached > raw_prompt: raise ValueError
            metrics={"prompt_tokens":raw_prompt-cached,"completion_tokens":completion,"cached_tokens":cached,"total_tokens":total}
            required={"subject","property","value","evidence_text"}
            if metrics["total_tokens"] != metrics["prompt_tokens"] + metrics["completion_tokens"] + metrics["cached_tokens"] or not isinstance(candidates, list) or len(candidates)>MAX_CANDIDATE_ROWS or any(not isinstance(v,dict) or not required<=set(v) or set(v)-required-{"unit","condition","graph_fact_key","source_locator"} or any(not isinstance(x,str) or len(x)>MAX_FIELD_CHARS or "\x00" in x for x in v.values()) or not v["evidence_text"].strip() for v in candidates): raise ValueError
        except (RetryableAdapterError, PermanentAdapterError) as error:
            if self.observer is not None: self.observer(route=route,idempotency_key=idempotency_key,model_config_id=model_config_id,error=error,called_at=called_at)
            raise
        except (OSError, TimeoutError, http.client.HTTPException):
            error=RetryableAdapterError("model dependency unavailable")
            if self.observer is not None: self.observer(route=route,idempotency_key=idempotency_key,model_config_id=model_config_id,error=error,called_at=called_at)
            raise error from None
        except (KeyError, IndexError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            error=PermanentAdapterError("native model response is invalid")
            if self.observer is not None: self.observer(route=route,idempotency_key=idempotency_key,model_config_id=model_config_id,error=error,called_at=called_at)
            raise error from None
        finally:
            connection.close()
        call_id = self.observer(route=route,idempotency_key=idempotency_key,model_config_id=model_config_id,metrics=metrics,error=None,called_at=called_at) if self.observer is not None else None
        public_metrics = dict(metrics)
        if public_metrics["cached_tokens"] == 0:
            public_metrics.pop("cached_tokens")
        return ModelResult(tuple(candidates), {**public_metrics, "model_calls":1}, call_id)

    def record_cache_hit(self, *, route: str, idempotency_key: str, model_config_id: str) -> str | None:
        return self.observer(route=route,idempotency_key=idempotency_key,model_config_id=model_config_id,metrics={"prompt_tokens":0,"completion_tokens":0,"cached_tokens":0,"total_tokens":0},error=None,cache_hit=True) if self.observer is not None else None

class UnconfiguredModelClient(OpenAICompatibleModelClient):
    pass

def require_document(context: AdapterContext, reader: DocumentReader) -> DocumentContent:
    if context.document_path is None: _fail()
    return reader.read(context.document_path)
def text_windows(text: str, size: int = 4_000) -> tuple[str, ...]:
    cleaned=text.strip(); return tuple(cleaned[i:i+size] for i in range(0,len(cleaned),size)) if cleaned else ()
def unit_idempotency_key(step_key: str, route: str, index: int, payload: str) -> str:
    canonical = " ".join(payload.split())
    return hashlib.sha256(f"{step_key}\x1f{route}\x1f{index}\x1f{hashlib.sha256(canonical.encode()).hexdigest()}".encode()).hexdigest()
def _clean(value: object) -> str:
    cleaned = "" if value is None else (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) if isinstance(value,(dict,list,tuple,set)) else str(value))
    if any((ord(character) < 32 and character not in "\t\r\n") or ord(character) == 127 for character in cleaned): _fail("native adapter output is invalid")
    return cleaned

def candidate_fact_id(row: Mapping[str, str]) -> str:
    identity="\x1f".join(row[c] for c in (FIELD["step_id"],FIELD["document_id"],FIELD["execution_idempotency_key"],"graph_fact_key",FIELD["subject"],FIELD["property"],FIELD["value"],FIELD["unit"],FIELD["condition"],FIELD["evidence_hash"],FIELD["route"]))
    return hashlib.sha256(identity.encode()).hexdigest()

def normalize_candidate(raw: Mapping[str, object], context: AdapterContext, route: str) -> dict[str,str]:
    if route not in ROUTES: _fail()
    if any(not isinstance(key,str) or key not in CANDIDATE_BUSINESS_FIELDS for key in raw): _fail("native adapter output is invalid")
    row={c:"" for c in SCHEMA59_COLUMNS}
    for key,value in raw.items():
        target=FIELD.get(key,key)
        if target in row: row[target]=_clean(value)
    evidence=row[FIELD["evidence_text"]].strip()
    if not evidence or any(len(v)>MAX_FIELD_CHARS for v in row.values()): _fail("native adapter output is invalid")
    row[FIELD["evidence_text"]]=evidence; row[FIELD["evidence_hash"]]=evidence_hash(evidence); row[FIELD["route"]]=route
    row[FIELD["document_id"]]=_clean(context.document_version_id); row[FIELD["step_id"]]=_clean(context.step_id); row[FIELD["model_config_id"]]=_clean(context.model_config_id)
    row[FIELD["execution_idempotency_key"]]=context.execution_idempotency_key; row[FIELD["schema_version"]]="schema59-v1"; row[FIELD["extraction_source"]]=route
    row[FIELD["review_status"]]="candidate"; row[SCHEMA59_COLUMNS[37]]="native"; row[SCHEMA59_COLUMNS[38]]="native-1"; row[SCHEMA59_COLUMNS[56]]=__import__("datetime").datetime.now(__import__("datetime").UTC).isoformat()
    provenance={"route":route,"locator":row[FIELD["source_locator"]],"model_call":row[FIELD["model_call_id"]],"model_config_id":row[FIELD["model_config_id"]],"step_id":row[FIELD["step_id"]],"document_id":row[FIELD["document_id"]],"execution_key":row[FIELD["execution_idempotency_key"]],"evidence":evidence}
    row[FIELD["provenance"]]=json.dumps([provenance],ensure_ascii=False,sort_keys=True,separators=(",", ":"))
    row["fact_id"]=candidate_fact_id(row); return row

def _serialize_tsv(rows: Sequence[Mapping[str,str]]) -> bytes:
    stream=io.StringIO(newline=""); writer=csv.DictWriter(stream,fieldnames=SCHEMA59_COLUMNS,delimiter="\t",lineterminator="\n",extrasaction="raise"); writer.writeheader(); writer.writerows(rows)
    data=stream.getvalue().encode("utf-8")
    if len(data)>MAX_ROUTE_TSV_BYTES: _fail("native adapter output is invalid")
    return data
def write_route_output(context:AdapterContext,route:str,candidates:Iterable[Mapping[str,object]],emit_progress:ProgressSink,metrics:Mapping[str,int]|None=None)->AdapterResult:
    route_dir=context.output_path(route); route_dir.mkdir(parents=True,exist_ok=True); rows=[]
    for raw in candidates:
        if len(rows)>=MAX_CANDIDATE_ROWS or not isinstance(raw,Mapping): _fail("native adapter output is invalid")
        rows.append(normalize_candidate(raw,context,route))
    rows.sort(key=lambda row:tuple(row[c] for c in SCHEMA59_COLUMNS)); candidate=route_dir/"candidates.schema59.tsv"; temp=candidate.with_suffix(".tmp"); temp.write_bytes(_serialize_tsv(rows)); os.replace(temp,candidate)
    manifest=route_dir/"manifest.json"; payload={"candidate_tsv":candidate.name,"document_version_id":str(context.document_version_id) if context.document_version_id else None,"execution_idempotency_key":context.execution_idempotency_key,"records":len(rows),"route":route,"schema":"schema59-v1","step_id":str(context.step_id) if context.step_id else None}; temp=manifest.with_suffix(".tmp"); temp.write_text(json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(",", ":")),encoding="utf-8"); os.replace(temp,manifest)
    emit_progress(ProgressEvent(stage=f"{route}.completed",processed=len(rows),total=len(rows))); return AdapterResult(manifest,{"records":len(rows),**(metrics or {})})
