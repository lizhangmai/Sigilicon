from __future__ import annotations

from pathlib import Path

import pytest

from sigilicon.flow import (
    ActionContext,
    ActionContract,
    AdapterExecution,
    AdapterResult,
    AdapterResultError,
    AdapterSelection,
    ArtifactPort,
    CollectedActionResult,
    ExecutionProfile,
    FlowEngine,
    FlowContractError,
    FlowExecutionError,
    FlowNode,
    FlowRegistry,
    FlowSpec,
    FlowTarget,
    ProducedArtifact,
)


class SingleMethodAdapter:
    def run(self, context: ActionContext) -> AdapterResult:
        output = context.output_path("report", "result.txt")
        output.write_text("complete\n", encoding="utf-8")
        return AdapterResult.succeeded(
            CollectedActionResult(
                artifacts=(
                    ProducedArtifact("report", "report.text", output),
                ),
                facts={"complete": True},
            )
        )


class CollectionFailureAdapter:
    def run(self, context: ActionContext) -> AdapterResult:
        execution = AdapterExecution.succeeded()
        try:
            raise ValueError("unreadable evidence")
        except ValueError as exc:
            raise AdapterResultError(execution, exc) from exc


def test_engine_consumes_one_complete_adapter_result(tmp_path: Path) -> None:
    registry = FlowRegistry()
    registry.register_action(
        ActionContract(
            "test.complete",
            outputs=(ArtifactPort("report", "report.text"),),
            facts=("complete",),
            adapters=("single-method",),
        )
    )
    registry.register_adapter("single-method", SingleMethodAdapter())
    engine = FlowEngine(registry)
    spec = FlowSpec(
        "test-owner",
        "adapter-interface",
        (FlowNode("complete", "test.complete"),),
        (FlowTarget("all", ("complete",)),),
    )
    profile = ExecutionProfile(
        "test-owner",
        "local",
        (AdapterSelection("test.complete", "single-method"),),
    )

    result = engine.run(
        engine.plan(spec, "all", profile),
        artifact_root=tmp_path / "artifacts",
        run_id="a" * 32,
    )

    assert result.status == "accepted"
    assert result.nodes["complete"].execution_status == "succeeded"
    assert result.nodes["complete"].result_status == "valid"
    assert result.nodes["complete"].facts == {"complete": True}


def test_adapter_result_keeps_execution_and_evidence_states_consistent() -> None:
    with pytest.raises(FlowExecutionError, match="must include"):
        AdapterResult(AdapterExecution.succeeded())
    with pytest.raises(FlowExecutionError, match="cannot include"):
        AdapterResult(
            AdapterExecution("failed", 1),
            CollectedActionResult(status="failed"),
        )


def test_registry_rejects_non_callable_run_at_registration() -> None:
    class InvalidAdapter:
        run = 1

    registry = FlowRegistry()
    with pytest.raises(FlowContractError, match=r"run\(context\)"):
        registry.register_adapter("invalid", InvalidAdapter())  # type: ignore[arg-type]


def test_direct_adapter_preserves_successful_execution_on_collection_failure(
    tmp_path: Path,
) -> None:
    registry = FlowRegistry()
    registry.register_action(
        ActionContract("test.collection-failure", adapters=("direct",))
    )
    registry.register_adapter("direct", CollectionFailureAdapter())
    engine = FlowEngine(registry)
    spec = FlowSpec(
        "test-owner",
        "adapter-collection-failure",
        (FlowNode("failure", "test.collection-failure"),),
        (FlowTarget("all", ("failure",)),),
    )
    profile = ExecutionProfile(
        "test-owner",
        "local",
        (AdapterSelection("test.collection-failure", "direct"),),
    )

    result = engine.run(
        engine.plan(spec, "all", profile),
        artifact_root=tmp_path / "artifacts",
        run_id="b" * 32,
    )

    outcome = result.nodes["failure"]
    assert outcome.execution_status == "succeeded"
    assert outcome.result_status == "failed"
    assert outcome.reason == "ValueError: unreadable evidence"
