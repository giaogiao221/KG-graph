from __future__ import annotations

import csv
import hashlib
import json
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.extraction.contracts import AdapterContext, PermanentAdapterError
from app.extraction.native.common import (
    BuiltinRuleEngine,
    DocumentContent,
    OpenAICompatibleModelClient,
    PersistentUnitCache,
    SCHEMA59_COLUMNS,
    TableUnit,
    unit_idempotency_key,
)
from app.extraction.native.common import MarkdownDocumentReader
from app.extraction.native import common as native_common
from app.extraction.native.llm_table import LLMTableAdapter
from app.extraction.native.llm_text import LLMTextAdapter
from app.extraction.native.rule_table import RuleTableAdapter
from app.extraction.native.rule_text import RuleTextAdapter
from app.extraction.registry import ADAPTERS, native_registry


class Reader:
    def __init__(self) -> None:
        self.calls = 0

    def read(self, path: Path) -> DocumentContent:
        self.calls += 1
        assert path.name == "fixture.md"
        return DocumentContent(
            text="Alpha has mass 3 kg.\n\n| material | mass |\n|---|---|\n| Beta | 4 kg |",
            tables=(TableUnit("table-1", (("material", "mass"), ("Beta", "4 kg"))),),
        )


class Rules:
    def __init__(self) -> None:
        self.text_calls = 0
        self.table_calls = 0

    def extract_text(self, document: DocumentContent):
        self.text_calls += 1
        return [{"graph_fact_key": "alpha:mass", "subject": "Alpha", "property": "mass", "value": "3", "unit": "kg", "evidence_text": "Alpha has mass 3 kg."}]

    def extract_table(self, table: TableUnit):
        self.table_calls += 1
        return [{"graph_fact_key": "beta:mass", "subject": "Beta", "property": "mass", "value": "4", "unit": "kg", "evidence_text": "Beta | 4 kg", "source_locator": table.identifier}]


class Model:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, str]] = []

    def extract(self, *, route, payload, idempotency_key, model_config_id):
        self.jobs.append((route, payload, idempotency_key))
        assert len(idempotency_key) == 64
        assert model_config_id == "model-safe-reference"
        subject = "Beta" if route == "llm_table" else "Alpha"
        return [{"graph_fact_key": f"{subject.lower()}:density", "subject": subject, "property": "density", "value": "2", "unit": "g/cm3", "evidence_text": payload[:80]}]


@pytest.fixture(autouse=True)
def secure_cache_hmac_key(monkeypatch):
    monkeypatch.setenv("EXTRACTION_CACHE_HMAC_KEY_ENV", "TEST_CACHE_HMAC_KEY")
    monkeypatch.setenv("TEST_CACHE_HMAC_KEY", "test-only-cache-hmac-key-with-32-bytes-minimum")


@pytest.fixture
def context(tmp_path: Path) -> AdapterContext:
    source = tmp_path / "fixture.md"
    source.write_text("fixture remains unchanged", encoding="utf-8")
    return AdapterContext(
        work_dir=tmp_path / "work",
        execution_idempotency_key="stable-key",
        document_path=source,
        model_config_id="model-safe-reference",
    )


@pytest.mark.parametrize("route", ["rule_text", "rule_table", "llm_text", "llm_table"])
def test_each_route_runs_independently_and_writes_scoped_outputs(context, route):
    reader, rules, model = Reader(), Rules(), Model()
    adapters = {
        "rule_text": RuleTextAdapter(reader=reader, engine=rules),
        "rule_table": RuleTableAdapter(reader=reader, engine=rules),
        "llm_text": LLMTextAdapter(reader=reader, client=model),
        "llm_table": LLMTableAdapter(reader=reader, client=model),
    }
    events = []

    result = adapters[route].run(context, events.append)

    assert result.manifest_path == context.work_dir / route / "manifest.json"
    assert result.manifest_path.is_file()
    assert (context.work_dir / route / "candidates.schema59.tsv").is_file()
    assert {path.name for path in context.work_dir.iterdir()} == {route}
    assert events[-1].stage == f"{route}.completed"


def test_rule_text_does_not_initialize_or_call_table_extraction(context):
    rules = Rules()
    RuleTextAdapter(reader=Reader(), engine=rules).run(context, lambda _event: None)
    assert (rules.text_calls, rules.table_calls) == (1, 0)


def test_rule_table_is_rule_only_and_never_needs_a_model(context):
    rules = Rules()
    result = RuleTableAdapter(reader=Reader(), engine=rules).run(context, lambda _event: None)
    assert result.metrics["records"] == 1
    assert (rules.text_calls, rules.table_calls) == (0, 1)


def test_llm_text_does_not_need_a_rule_engine(context):
    model = Model()
    LLMTextAdapter(reader=Reader(), client=model).run(context, lambda _event: None)
    assert [job[0] for job in model.jobs] == ["llm_text"]


def test_llm_table_builds_jobs_directly_from_source_tables(context):
    model = Model()
    LLMTableAdapter(reader=Reader(), client=model).run(context, lambda _event: None)
    assert [job[0] for job in model.jobs] == ["llm_table"]
    assert "Beta" in model.jobs[0][1]
    assert not (context.work_dir / "rule_table").exists()


def test_candidate_tsv_has_59_columns_and_route_provenance(context):
    RuleTextAdapter(reader=Reader(), engine=Rules()).run(context, lambda _event: None)
    with (context.work_dir / "rule_text" / "candidates.schema59.tsv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    assert len(rows[0]) == 59
    assert rows[0]["路由"] == "rule_text"
    assert rows[0]["证据文本"] == "Alpha has mass 3 kg."


def test_candidate_cannot_inject_platform_owned_system_fields(context):
    class InjectingRules(Rules):
        def extract_text(self, document):
            return [{
                "subject": "A", "property": "p", "value": "1", "evidence_text": "e",
                "route": "llm_text", "review_status": "approved", "fact_id": "forged",
            }]

    with pytest.raises(PermanentAdapterError, match="native adapter output is invalid"):
        RuleTextAdapter(reader=Reader(), engine=InjectingRules()).run(context, lambda _event: None)


def test_registry_uses_native_adapters_and_preserves_typed_queues():
    assert isinstance(ADAPTERS["rule_text"], RuleTextAdapter)
    assert isinstance(ADAPTERS["rule_table"], RuleTableAdapter)
    assert isinstance(ADAPTERS["llm_text"], LLMTextAdapter)
    assert isinstance(ADAPTERS["llm_table"], LLMTableAdapter)
    assert {name: adapter.queue for name, adapter in ADAPTERS.items()} == {
        "rule_text": "rule", "rule_table": "rule", "llm_text": "llm", "llm_table": "llm"
    }


def test_native_registry_injects_shared_reader_rules_and_model(context):
    reader, rules, model = Reader(), Rules(), Model()
    adapters = native_registry(reader=reader, rule_engine=rules, model_client=model)
    adapters["rule_text"].run(context, lambda _event: None)
    adapters["llm_table"].run(context, lambda _event: None)
    assert reader.calls == 2
    assert rules.text_calls == 1
    assert [job[0] for job in model.jobs] == ["llm_table"]


def test_table_routes_report_table_candidate_metrics(context):
    rule = RuleTableAdapter(reader=Reader(), engine=Rules()).run(context, lambda _event: None)
    llm = LLMTableAdapter(reader=Reader(), client=Model()).run(context, lambda _event: None)
    assert rule.metrics["table_candidates"] == 1
    assert llm.metrics["table_candidates"] == 1


def test_native_route_leaves_read_only_source_fixture_byte_identical(tmp_path: Path):
    source_root = tmp_path / "readonly-source"
    source_root.mkdir()
    source = source_root / "fixture.md"
    source.write_text("| material | mass |\n|---|---|\n| Beta | 4 kg |", encoding="utf-8")
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    context = AdapterContext(work_dir=tmp_path / "work", document_path=source)

    RuleTableAdapter(reader=MarkdownDocumentReader(), engine=Rules()).run(
        context, lambda _event: None
    )

    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert {path.name for path in source_root.iterdir()} == {"fixture.md"}


def test_document_symlink_is_rejected_instead_of_resolved(tmp_path: Path, monkeypatch):
    link = tmp_path / "link.md"
    context = AdapterContext(work_dir=tmp_path / "work", document_path=link)
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path.absolute() == link.absolute(),
    )

    with pytest.raises(PermanentAdapterError, match="native adapter input is invalid"):
        RuleTextAdapter(reader=MarkdownDocumentReader(), engine=Rules()).run(
            context, lambda _event: None
        )


def test_schema59_uses_established_contract_boundaries():
    assert len(SCHEMA59_COLUMNS) == 59
    assert SCHEMA59_COLUMNS[:4] == ("fact_id", "graph_fact_key", "文档ID", "书名")
    assert SCHEMA59_COLUMNS[-2:] == ("抽取来源", "证据文本")


def test_builtin_rule_engine_produces_independent_text_and_table_candidates(tmp_path: Path):
    source = tmp_path / "source.md"
    source.write_text(
        "Alpha has mass 3 kg.\n\n| material | mass |\n|---|---|\n| Beta | 4 kg |",
        encoding="utf-8",
    )
    reader = MarkdownDocumentReader(allowed_root=tmp_path)
    document = reader.read(source)
    engine = BuiltinRuleEngine()

    text = list(engine.extract_text(document))
    table = list(engine.extract_table(document.tables[0]))

    assert text[0]["subject"] == "Alpha"
    assert text[0]["property"] == "mass"
    assert text[0]["value"] == "3"
    assert table[0]["subject"] == "Beta"
    assert table[0]["property"] == "mass"
    assert table[0]["value"] == "4 kg"


@pytest.mark.parametrize("supports_idempotency, expected_header", [(True, "stable-unit-key"), (False, None)])
def test_openai_client_uses_opaque_config_secret_env_and_strict_usage(tmp_path: Path, monkeypatch, supports_idempotency, expected_header):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            requests.append((self.headers.get("Authorization"), self.headers.get("Idempotency-Key"), json.loads(self.rfile.read(length))))
            body = json.dumps({
                "choices": [{"message": {"content": json.dumps([{"subject":"Alpha","property":"mass","value":"3","evidence_text":"Alpha has mass 3 kg."}])}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config_id = "01234567-89ab-cdef-8123-456789abcdef"
    monkeypatch.setenv("SAFE_MODEL_KEY", "local-test-secret")
    monkeypatch.setenv("EXTRACTION_MODEL_CONFIGS_JSON", json.dumps({config_id: {
        "endpoint": f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
        "model": "fixture-model", "api_key_env": "SAFE_MODEL_KEY",
        "allowed_hosts": ["127.0.0.1"], "allow_private_network": True,
        "allow_insecure_http": True, "provider_supports_idempotency": supports_idempotency
    }}))
    try:
        result = OpenAICompatibleModelClient().extract(
            route="llm_text", payload="Alpha has mass 3 kg.",
            idempotency_key="stable-unit-key", model_config_id=config_id,
        )
    finally:
        server.shutdown()
        thread.join()
    assert result.metrics == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18, "model_calls": 1}
    assert result.candidates[0]["subject"] == "Alpha"
    assert requests[0][0] == "Bearer local-test-secret"
    assert requests[0][1] == expected_header
    assert "local-test-secret" not in json.dumps(requests[0][2])


def test_model_unit_keys_are_distinct_per_unit_and_stable_on_retry():
    first = unit_idempotency_key("step-key", "llm_text", 0, "same payload")
    retry = unit_idempotency_key("step-key", "llm_text", 0, "same   payload")
    next_unit = unit_idempotency_key("step-key", "llm_text", 1, "same payload")
    assert first == retry
    assert first != next_unit


@pytest.mark.parametrize("endpoint", ["http://169.254.169.254/latest", "http://localhost/model"])
def test_model_endpoint_rejects_metadata_and_localhost_by_default(monkeypatch, endpoint):
    config_id = "01234567-89ab-cdef-8123-456789abcdef"
    monkeypatch.setenv("SAFE_MODEL_KEY", "secret")
    monkeypatch.setenv("EXTRACTION_MODEL_CONFIGS_JSON", json.dumps({config_id:{
        "endpoint":endpoint,"model":"m","api_key_env":"SAFE_MODEL_KEY",
        "allowed_hosts":[endpoint.split('/')[2]],"allow_private_network":False,
        "allow_insecure_http":True,"provider_supports_idempotency":False}}))
    with pytest.raises(PermanentAdapterError):
        OpenAICompatibleModelClient().extract(route="llm_text", payload="x", idempotency_key="k", model_config_id=config_id)


def test_model_transport_pins_first_dns_result_and_ignores_proxy_environment(monkeypatch):
    dns_calls = []
    connections = []

    def rebind_dns(host, port, **_kwargs):
        dns_calls.append((host, port))
        address = "93.184.216.34" if len(dns_calls) == 1 else "127.0.0.1"
        return [(2, 1, 6, "", (address, port))]

    class Response:
        status = 200
        def read(self, _limit):
            return json.dumps({
                "choices": [{"message": {"content": json.dumps([{"subject":"A","property":"p","value":"1","evidence_text":"e"}])}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }).encode()

    class Connection:
        def __init__(self, host, pinned_ip, port, allowed_ips, timeout):
            connections.append((host, pinned_ip, port, allowed_ips, timeout))
        def request(self, *_args, **_kwargs): pass
        def getresponse(self): return Response()
        def close(self): pass

    monkeypatch.setattr(socket, "getaddrinfo", rebind_dns)
    monkeypatch.setattr(native_common, "_PinnedHTTPSConnection", Connection, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    monkeypatch.setenv("SAFE_MODEL_KEY", "secret")
    config_id = "01234567-89ab-cdef-8123-456789abcdef"
    monkeypatch.setenv("EXTRACTION_MODEL_CONFIGS_JSON", json.dumps({config_id: {
        "endpoint": "https://example.com/v1/chat/completions", "model": "m", "api_key_env": "SAFE_MODEL_KEY",
        "allowed_hosts": ["example.com"], "allow_private_network": False,
        "allow_insecure_http": False, "provider_supports_idempotency": True,
    }}))

    result = OpenAICompatibleModelClient().extract(
        route="llm_text", payload="x", idempotency_key="unit", model_config_id=config_id
    )

    assert result.metrics["total_tokens"] == 2
    assert dns_calls == [("example.com", 443)]
    assert connections[0][1] == "93.184.216.34"
    assert connections[0][3] == frozenset({"93.184.216.34"})


def test_model_transport_retries_transient_connection_failures(monkeypatch):
    attempts = []

    class Response:
        status = 200
        def read(self, _limit):
            return json.dumps({
                "choices": [{"message": {"content": json.dumps([{"subject":"A","property":"p","value":"1","evidence_text":"e"}])}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }).encode()

    class Connection:
        def __init__(self, *_args): pass
        def request(self, *_args, **_kwargs):
            attempts.append(1)
            if len(attempts) < 3:
                raise OSError("transient reset")
        def getresponse(self): return Response()
        def close(self): pass

    config = native_common._ResolvedModelConfiguration(
        "https", "example.com", 443, "/v1/chat/completions", "m", "secret", True,
        frozenset({"93.184.216.34"}),
    )
    monkeypatch.setattr(native_common, "_PinnedHTTPSConnection", Connection)
    result = OpenAICompatibleModelClient(
        configuration_resolver=lambda _config_id: config,
        transport_attempts=3,
        retry_backoff_seconds=0,
    ).extract(route="llm_text", payload="x", idempotency_key="unit", model_config_id="model")

    assert len(attempts) == 3
    assert result.metrics["total_tokens"] == 2


def test_llm_route_reuses_local_unit_cache_and_reports_token_metrics(context):
    model = Model()
    adapter = LLMTextAdapter(reader=Reader(), client=model)
    first = adapter.run(context, lambda _event: None)
    second = adapter.run(context, lambda _event: None)
    assert len(model.jobs) == 1
    assert first.metrics["model_calls"] == second.metrics["model_calls"] == 1


def test_llm_unit_cache_survives_new_attempt_workdir(tmp_path: Path):
    model = Model()
    cache_root = tmp_path / "persistent-cache"
    contexts = [
        AdapterContext(
            work_dir=tmp_path / f"attempt-{attempt}",
            execution_idempotency_key="stable-key",
            document_path=tmp_path / "fixture.md",
            model_config_id="model-safe-reference",
            persistent_cache_root=cache_root,
        )
        for attempt in (1, 2)
    ]
    adapter = LLMTextAdapter(reader=Reader(), client=model)

    adapter.run(contexts[0], lambda _event: None)
    adapter.run(contexts[1], lambda _event: None)

    assert len(model.jobs) == 1
    cache_files = list(cache_root.rglob("*.json"))
    assert len(cache_files) == 1
    assert cache_files[0].stat().st_size < 1_000_000


def test_llm_unit_cache_serializes_concurrent_attempts(tmp_path: Path):
    model = Model()
    cache_root = tmp_path / "cache"
    contexts = [
        AdapterContext(
            work_dir=tmp_path / f"concurrent-{index}",
            execution_idempotency_key="same-execution",
            document_path=tmp_path / "fixture.md",
            model_config_id="model-safe-reference",
            persistent_cache_root=cache_root,
        )
        for index in range(2)
    ]
    adapter = LLMTextAdapter(reader=Reader(), client=model)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda value: adapter.run(value, lambda _event: None), contexts))
    assert len(model.jobs) == 1


def test_llm_unit_cache_rejects_tampered_json(tmp_path: Path):
    model = Model()
    context = AdapterContext(
        work_dir=tmp_path / "first",
        execution_idempotency_key="same-execution",
        document_path=tmp_path / "fixture.md",
        model_config_id="model-safe-reference",
        persistent_cache_root=tmp_path / "cache",
    )
    adapter = LLMTextAdapter(reader=Reader(), client=model)
    adapter.run(context, lambda _event: None)
    cache_file = next((tmp_path / "cache").rglob("*.json"))
    cache_file.chmod(0o600)
    envelope = json.loads(cache_file.read_text(encoding="utf-8"))
    assert set(envelope) == {"schema", "hmac_sha256", "payload"}
    envelope["payload"]["candidates"][0]["evidence_text"] = "tampered"
    payload_bytes = json.dumps(envelope["payload"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    envelope["hmac_sha256"] = hashlib.sha256(payload_bytes).hexdigest()
    cache_file.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(PermanentAdapterError, match="native unit cache is invalid"):
        adapter.run(
            AdapterContext(
                work_dir=tmp_path / "retry",
                execution_idempotency_key="same-execution",
                document_path=tmp_path / "fixture.md",
                model_config_id="model-safe-reference",
                persistent_cache_root=tmp_path / "cache",
            ),
            lambda _event: None,
        )


def test_cache_lock_waits_for_owner_even_when_lock_mtime_is_old(tmp_path: Path):
    cache = PersistentUnitCache(tmp_path / "cache", "execution", "llm_text")
    unit_key = "a" * 64
    lock_path = cache._path(unit_key).with_suffix(".lock")
    with ThreadPoolExecutor(max_workers=1) as pool:
        with cache._lock(cache._path(unit_key)):
            old = time.time() - 3600
            os.utime(lock_path, (old, old))
            future = pool.submit(
                cache.get_or_compute,
                unit_key,
                lambda: native_common.ModelResult((), {"prompt_tokens":0,"completion_tokens":0,"total_tokens":0,"model_calls":0}),
            )
            time.sleep(0.1)
            assert not future.done()
        assert future.result(timeout=2).metrics["model_calls"] == 0


def test_native_routes_ignore_external_hook_configuration(context, tmp_path: Path, monkeypatch):
    engine = tmp_path / "fake-engine"; engine.mkdir()
    hook_source = engine / "native_route_hooks.py"
    hook_source.write_text(
        "raise RuntimeError('native hook bridge must never load')\n",
        encoding="utf-8",
    )
    before = {path.relative_to(engine).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in engine.rglob("*") if path.is_file()}
    monkeypatch.setenv("EXTRACTION_ENGINE_SOURCE_ROOT", str(engine))

    result = RuleTextAdapter(reader=Reader(), engine=BuiltinRuleEngine()).run(context, lambda _event: None)

    assert result.metrics["records"] == 1
    after = {path.relative_to(engine).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in engine.rglob("*") if path.is_file()}
    assert after == before
    assert b"Alpha has mass 3 kg." in (result.manifest_path.parent / "candidates.schema59.tsv").read_bytes()
    assert not list(context.work_dir.rglob("staged-engine"))
