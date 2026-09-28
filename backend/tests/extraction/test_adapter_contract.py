from __future__ import annotations

from pathlib import Path
import json

import pytest

from app.extraction.contracts import AdapterContext, PermanentAdapterError
from app.extraction.fake_adapter import FakeAdapter
from app.extraction.registry import ADAPTERS, adapter_for


def test_adapter_result_requires_manifest_and_metrics(tmp_path: Path) -> None:
    events = []
    context = AdapterContext(
        work_dir=tmp_path,
        execution_idempotency_key="stable-provider-idempotency-key",
    )

    result = FakeAdapter().run(context, events.append)

    assert result.manifest_path.exists()
    assert result.manifest_path == tmp_path / "manifest.json"
    assert result.metrics["records"] == 1
    assert events[-1].stage == "completed"
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert (
        manifest["execution_idempotency_key"]
        == "stable-provider-idempotency-key"
    )


@pytest.mark.parametrize("unsafe", ["../escape.json", "/tmp/escape.json", "C:\\escape.json"])
def test_adapter_context_rejects_absolute_and_traversal_paths(
    tmp_path: Path, unsafe: str
) -> None:
    context = AdapterContext(work_dir=tmp_path)

    with pytest.raises(PermanentAdapterError, match="work directory"):
        context.output_path(unsafe)


def test_registry_exposes_all_four_typed_routes() -> None:
    assert set(ADAPTERS) == {"rule_text", "llm_text", "rule_table", "llm_table"}
    assert adapter_for("rule_text").queue == "rule"
    assert adapter_for("rule_table").queue == "rule"
    assert adapter_for("llm_text").queue == "llm"
    assert adapter_for("llm_table").queue == "llm"


def test_unknown_adapter_is_a_permanent_configuration_failure() -> None:
    with pytest.raises(PermanentAdapterError, match="unknown adapter"):
        adapter_for("invented")
