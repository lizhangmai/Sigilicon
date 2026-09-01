from __future__ import annotations

from pathlib import Path

import pytest

from sigilicon.flow import (
    ActionContext,
    ActionBinding,
    ActionContract,
    AdapterExecution,
    AdapterResult,
    ArtifactPort,
    CollectedActionResult,
    ExecutionEnvironment,
    FactKind,
    FactSchema,
    FactSet,
    FactSource,
    FactSpec,
    FlowEngine,
    FlowContractError,
    FlowExecutionError,
    FlowNode,
    FlowRegistry,
    FlowSpec,
    FlowTarget,
    ProducedArtifact,
)
from sigilicon.workflows.action_registry import build_action_registry
from sigilicon.flow.layout import XSTREAM_CALIBRE_LAYOUT_ADAPTER
from sigilicon.flow.physical_design import OA_XSTREAM_MATERIALIZATION_ADAPTER


class SingleMethodAdapter:
    def run(self, context: ActionContext) -> AdapterResult:
        output = context.output_path("report", "result.txt")
        output.write_text("complete\n", encoding="utf-8")
        return AdapterResult.succeeded(
            CollectedActionResult(
                facts=FactSet(
                    context.action.fact_schema,
                    {"complete": True},
                    FactSource(context.action.kind, context.node_id),
                ),
                artifacts=(
                    ProducedArtifact("report", "report.text", output),
                ),
            )
        )


def test_engine_consumes_one_complete_adapter_result(tmp_path: Path) -> None:
    registry = FlowRegistry()
    registry.register_action(
        ActionContract(
            "test.complete",
            outputs=(ArtifactPort("report", "report.text"),),
            fact_schema=FactSchema(
                "test.complete",
                (FactSpec("complete", FactKind.BOOLEAN),),
            ),
            adapters=("single-method",),
        )
    )
    registry.register_adapter("single-method", SingleMethodAdapter())
    engine = FlowEngine(registry)
    spec = FlowSpec(
        owner="test-owner",
        flow_id="adapter-interface",
        recipe_id="adapter-interface-recipe",
        nodes=(FlowNode("complete", "test.complete"),),
        targets=(FlowTarget("all", ("complete",)),),
        action_bindings=(ActionBinding("test.complete", "single-method"),),
    )

    result = engine.run(
        engine.plan(spec, "all"),
        artifact_root=tmp_path / "artifacts",
        environment=ExecutionEnvironment(),
        run_id="a" * 32,
    )

    assert result.status == "accepted"
    assert result.nodes["complete"].execution_status == "succeeded"
    assert result.nodes["complete"].result_status == "valid"
    assert result.nodes["complete"].facts is not None
    facts = result.nodes["complete"].facts
    assert facts.schema == registry.action("test.complete").fact_schema
    assert facts.source == FactSource("test.complete", "complete")
    assert facts["complete"] is True


def test_adapter_result_keeps_execution_and_evidence_states_consistent() -> None:
    with pytest.raises(FlowExecutionError, match="must include"):
        AdapterResult(AdapterExecution.succeeded())
    with pytest.raises(FlowExecutionError, match="cannot include"):
        AdapterResult(
            AdapterExecution("failed", 1),
            CollectedActionResult(
                facts=FactSet.empty(
                    FactSchema("test.failed"),
                    source=FactSource("test.failed"),
                ),
                status="failed",
            ),
        )


def test_registry_rejects_non_callable_run_at_registration() -> None:
    class InvalidAdapter:
        run = 1

    registry = FlowRegistry()
    with pytest.raises(FlowContractError, match=r"run\(context\)"):
        registry.register_adapter("invalid", InvalidAdapter())  # type: ignore[arg-type]


def test_common_registry_contains_explicit_custom_layout_backends() -> None:
    registry = build_action_registry()

    assert registry.has_adapter(OA_XSTREAM_MATERIALIZATION_ADAPTER)
    assert registry.has_adapter(XSTREAM_CALIBRE_LAYOUT_ADAPTER)
    assert (
        OA_XSTREAM_MATERIALIZATION_ADAPTER
        in registry.action("physical-design.materialize").adapters
    )
    assert (
        XSTREAM_CALIBRE_LAYOUT_ADAPTER
        in registry.action("custom-layout.verify").adapters
    )
