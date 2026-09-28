from __future__ import annotations

import json
import os
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence

from app.extraction.contracts import ExtractionAdapter, PermanentAdapterError
from app.extraction.fake_adapter import FakeAdapter
from app.extraction.legacy.v105_rule import V105RuleAdapter
from app.extraction.legacy.v106_hybrid import V106HybridAdapter
from app.extraction.native.llm_table import LLMTableAdapter
from app.extraction.native.llm_text import LLMTextAdapter
from app.extraction.native.merge import MergeAdapter
from app.extraction.native.rule_table import RuleTableAdapter
from app.extraction.native.rule_text import RuleTextAdapter
from app.extraction.native.common import (
    BasicRuleEngine,
    DocumentReader,
    MarkdownDocumentReader,
    ModelClient,
    RuleEngine,
    UnconfiguredModelClient,
)


def native_registry(
    *,
    reader: DocumentReader | None = None,
    rule_engine: RuleEngine | None = None,
    model_client: ModelClient | None = None,
) -> Mapping[str, ExtractionAdapter]:
    configured_reader = reader or MarkdownDocumentReader()
    configured_rules = rule_engine or BasicRuleEngine()
    configured_model = model_client or UnconfiguredModelClient()
    return MappingProxyType(
        {
            "rule_text": RuleTextAdapter(configured_reader, configured_rules),
            "llm_text": LLMTextAdapter(configured_reader, configured_model),
            "rule_table": RuleTableAdapter(configured_reader, configured_rules),
            "llm_table": LLMTableAdapter(configured_reader, configured_model),
        }
    )


ADAPTERS: Mapping[str, ExtractionAdapter] = native_registry()
PIPELINE_ADAPTERS: Mapping[str, ExtractionAdapter] = MappingProxyType(
    {
        "merge": MergeAdapter(),
        "validate": FakeAdapter(name="validate", queue="rule"),
    }
)
_LEGACY_KEYS = frozenset({"legacy_v105_rule", "legacy_v106_hybrid"})


def build_legacy_adapters(
    *,
    engine_root: Path,
    input_roots: Sequence[Path],
    engine_subdir: str = "model",
    config_subdir: str = "config",
) -> Mapping[str, ExtractionAdapter]:
    if not str(engine_root) or not input_roots:
        raise PermanentAdapterError("legacy adapter is not configured")
    engine = Path(engine_root).absolute()
    inputs = tuple(Path(root).absolute() for root in input_roots)
    return MappingProxyType({
        "legacy_v105_rule": V105RuleAdapter(
            source_root=engine,
            input_source_roots=inputs,
            engine_subdir=engine_subdir,
            config_subdir=config_subdir,
        ),
        "legacy_v106_hybrid": V106HybridAdapter(
            source_root=engine,
            input_source_roots=inputs,
            engine_subdir=engine_subdir,
            config_subdir=config_subdir,
        ),
    })


def legacy_adapters_from_environment(
    environment: Mapping[str, str],
) -> Mapping[str, ExtractionAdapter]:
    engine = environment.get("LEGACY_ENGINE_SOURCE_ROOT", "").strip()
    roots_json = environment.get("LEGACY_READONLY_INPUT_ROOTS_JSON", "").strip()
    if not engine and not roots_json:
        return MappingProxyType({})
    try:
        raw_roots = json.loads(roots_json)
    except (json.JSONDecodeError, TypeError):
        raise PermanentAdapterError("legacy adapter is not configured") from None
    if (
        not engine
        or not isinstance(raw_roots, list)
        or not raw_roots
        or any(not isinstance(value, str) or not value.strip() for value in raw_roots)
    ):
        raise PermanentAdapterError("legacy adapter is not configured")
    # Layout defaults keep KGchouqu_clean working; the vendored hzy engine sets
    # LEGACY_ENGINE_SUBDIR="." and LEGACY_ENGINE_CONFIG_SUBDIR="src/config".
    return build_legacy_adapters(
        engine_root=Path(engine),
        input_roots=tuple(Path(value) for value in raw_roots),
        engine_subdir=environment.get("LEGACY_ENGINE_SUBDIR", "model"),
        config_subdir=environment.get("LEGACY_ENGINE_CONFIG_SUBDIR", "config"),
    )


try:
    LEGACY_ADAPTERS: Mapping[str, ExtractionAdapter] = legacy_adapters_from_environment(os.environ)
except PermanentAdapterError:
    LEGACY_ADAPTERS = MappingProxyType({})


def adapter_for(kind: str) -> ExtractionAdapter:
    try:
        return ADAPTERS[kind]
    except KeyError:
        raise PermanentAdapterError(f"unknown adapter: {kind}") from None


def execution_adapter_for(kind: str) -> ExtractionAdapter:
    if kind in PIPELINE_ADAPTERS:
        return PIPELINE_ADAPTERS[kind]
    if kind in LEGACY_ADAPTERS:
        return LEGACY_ADAPTERS[kind]
    if kind in _LEGACY_KEYS:
        raise PermanentAdapterError("legacy adapter is not configured")
    return adapter_for(kind)
