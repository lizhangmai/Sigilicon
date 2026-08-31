from __future__ import annotations

from pathlib import Path

import pytest

from sigilicon.flow import (
    ActionContext,
    ActionBinding,
    ActionContract,
    AdapterExecution,
    AdapterResult,
    AdapterResultError,
    ArtifactPort,
    CollectedActionResult,
    ExecutionEnvironment,
    FlowEngine,
    FlowContractError,
    FlowExecutionError,
    FlowNode,
    FlowRegistry,
    FlowSpec,
    FlowTarget,
    ProducedArtifact,
    complete_staged_run,
)
from sigilicon.workflows.builtin import build_flow_registry


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


def test_public_staged_completion_helper_preserves_collection_failure_state(
    tmp_path: Path,
) -> None:
    execution = AdapterExecution.succeeded()

    with pytest.raises(AdapterResultError) as captured:
        complete_staged_run(
            None,  # type: ignore[arg-type]
            validate_inputs=lambda _context: (),
            prepare=lambda _context: None,
            execute=lambda _context: execution,
            collect_result=lambda _context, _execution: (_ for _ in ()).throw(
                ValueError("unreadable evidence")
            ),
        )

    assert captured.value.execution is execution
    assert str(captured.value) == "ValueError: unreadable evidence"


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


def test_registry_rejects_the_removed_four_operation_adapter_interface() -> None:
    class RemovedAdapter:
        def validate_inputs(self, _context):
            return ()

        def prepare(self, _context):
            pass

        def execute(self, _context):
            return AdapterExecution.succeeded()

        def collect_result(self, _context, _execution):
            return CollectedActionResult()

    with pytest.raises(FlowContractError, match=r"run\(context\)"):
        FlowRegistry().register_adapter("removed", RemovedAdapter())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "name",
    (
        "calibre-physical-verification",
        "calibre-xrc-pex",
        "oa-virtuoso-xstream-materialization",
        "physical-design-observation",
        "receipt-bound-verification-source",
        "reference-materialization",
        "reference-pnr",
        "source-assets",
        "synopsys-dc",
        "synopsys-fc",
        "synopsys-hspice",
        "synopsys-vcs",
    ),
)
def test_builtin_adapters_cross_the_registry_without_legacy_wrapping(
    name: str,
) -> None:
    implementation = build_flow_registry().adapter(name)

    assert callable(implementation.run)
    assert type(implementation).__module__ != "sigilicon.flow.registry"


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
        owner="test-owner",
        flow_id="adapter-collection-failure",
        recipe_id="adapter-collection-failure-recipe",
        nodes=(FlowNode("failure", "test.collection-failure"),),
        targets=(FlowTarget("all", ("failure",)),),
        action_bindings=(ActionBinding("test.collection-failure", "direct"),),
    )

    result = engine.run(
        engine.plan(spec, "all"),
        artifact_root=tmp_path / "artifacts",
        environment=ExecutionEnvironment(),
        run_id="b" * 32,
    )

    outcome = result.nodes["failure"]
    assert outcome.execution_status == "succeeded"
    assert outcome.result_status == "failed"
    assert outcome.reason == "ValueError: unreadable evidence"
