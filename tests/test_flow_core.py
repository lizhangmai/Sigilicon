from __future__ import annotations

from dataclasses import replace
import json
import hashlib
from pathlib import Path

import pytest

from sigilicon.execution import RunStore, RunStoreError
from sigilicon.flow import (
    ActionBinding,
    ActionContext,
    ActionContract,
    ActionPlan,
    AdapterExecution,
    ArtifactBinding,
    ArtifactPort,
    CollectedActionResult,
    ExecutionEnvironment,
    FactKind,
    FactSchema,
    FactSet,
    FactSource,
    FactSpec,
    FlowContractError,
    FlowEngine,
    FlowExecutionError,
    FlowNode,
    FlowRegistry,
    FlowSpec,
    FlowTarget,
    PolicyCheck,
    PolicySpec,
    ProducedArtifact,
    SourceMember,
)
from sigilicon.virtuoso.operation_journal import write_operation_incident
from sigilicon.paths import ProjectContext

from conftest import (
    StagedAdapterFixture,
)


def _run_store(tmp_path: Path, artifact_root: Path) -> RunStore:
    return RunStore(
        ProjectContext.from_roots(
            tmp_path,
            artifact_root=artifact_root,
            workspace_root=tmp_path / "workspace",
        )
    )


class SourceAdapter(StagedAdapterFixture):
    def __init__(self) -> None:
        self.executions = 0

    def validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        return ()

    def prepare(self, context: ActionContext) -> None:
        pass

    def execute(self, context: ActionContext) -> AdapterExecution:
        self.executions += 1
        output = context.output_path("source", "value.txt")
        output.write_text(str(context.action_config["text"]), encoding="utf-8")
        return AdapterExecution.succeeded()

    def collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        return CollectedActionResult(
            facts=FactSet.empty(
                context.action.fact_schema,
                source=FactSource(context.action.kind, context.node_id),
            ),
            artifacts=(
                ProducedArtifact(
                    role="source",
                    kind="text.plain",
                    path=context.output_path("source", "value.txt"),
                ),
            )
        )


class TransformAdapter(StagedAdapterFixture):
    def __init__(self) -> None:
        self.executions = 0

    def validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        return ()

    def prepare(self, context: ActionContext) -> None:
        pass

    def execute(self, context: ActionContext) -> AdapterExecution:
        self.executions += 1
        value = context.input("input").path.read_text(encoding="utf-8").upper()
        output = context.output_path("transformed", "value.txt")
        output.write_text(value, encoding="utf-8")
        return AdapterExecution.succeeded()

    def collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        output = context.output_path("transformed", "value.txt")
        return CollectedActionResult(
            facts=FactSet(
                context.action.fact_schema,
                {"length": len(output.read_text(encoding="utf-8"))},
                FactSource(context.action.kind, context.node_id),
            ),
            artifacts=(
                ProducedArtifact(
                    role="transformed",
                    kind="text.plain",
                    path=output,
                    qualifiers=context.input("input").qualifiers,
                ),
            ),
            evidence=(context.input("input").path,)
            if context.action_config.get("foreign_evidence")
            else (),
        )


class VerifyAdapter(StagedAdapterFixture):
    def __init__(self) -> None:
        self.executions = 0

    def validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        return ()

    def prepare(self, context: ActionContext) -> None:
        pass

    def execute(self, context: ActionContext) -> AdapterExecution:
        self.executions += 1
        value = context.input("candidate").path.read_text(encoding="utf-8")
        expected = str(context.action_config["expected"])
        report = context.output_path("report", "verification.txt")
        report.write_text(
            f"actual={value}\nexpected={expected}\n",
            encoding="utf-8",
        )
        return AdapterExecution.succeeded()

    def collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        expected = str(context.action_config["expected"])
        return CollectedActionResult(
            facts=FactSet(
                context.action.fact_schema,
                {
                    "accepted": context.input("candidate").path.read_text(
                        encoding="utf-8"
                    )
                    == expected
                },
                FactSource(context.action.kind, context.node_id),
            ),
            artifacts=(
                ProducedArtifact(
                    role="report",
                    kind="report.text",
                    path=context.output_path("report", "verification.txt"),
                ),
            ),
        )


def registry() -> tuple[FlowRegistry, SourceAdapter, TransformAdapter, VerifyAdapter]:
    source = SourceAdapter()
    transform = TransformAdapter()
    verify = VerifyAdapter()
    result = FlowRegistry()
    result.register_action(
        ActionContract(
            kind="fake.source",
            outputs=(ArtifactPort("source", "text.plain"),),
            adapters=("fake-source",),
        )
    )
    result.register_action(
        ActionContract(
            kind="fake.transform",
            inputs=(ArtifactPort("input", "text.plain"),),
            outputs=(ArtifactPort("transformed", "text.plain"),),
            fact_schema=FactSchema(
                "fake.transform",
                (FactSpec("length", FactKind.INTEGER),),
            ),
            adapters=("fake-transform",),
        )
    )
    result.register_action(
        ActionContract(
            kind="fake.verify",
            inputs=(ArtifactPort("candidate", "text.plain"),),
            outputs=(ArtifactPort("report", "report.text"),),
            fact_schema=FactSchema(
                "fake.verify",
                (FactSpec("accepted", FactKind.BOOLEAN),),
            ),
            adapters=("fake-verify",),
        )
    )
    result.register_adapter("fake-source", source)
    result.register_adapter("fake-transform", transform)
    result.register_adapter("fake-verify", verify)
    return result, source, transform, verify


def test_action_plan_is_required_and_source_drift_blocks_preflight(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "intent.toml"
    source_path.write_text("value = 1\n", encoding="utf-8")
    member = SourceMember(
        "intent.toml",
        tmp_path,
        "value = 1\n",
        False,
        source_path,
    )
    with pytest.raises(FlowContractError, match="must declare explicit sources"):
        ActionPlan("fake.intent", object(), {"value": 1})
    adapter = SourceAdapter()
    flow_registry = FlowRegistry()
    flow_registry.register_action(
        ActionContract(
            kind="fake.planned",
            adapters=("fake-planned",),
            plan_input_kind="fake.intent",
        )
    )
    flow_registry.register_adapter("fake-planned", adapter)
    engine = FlowEngine(flow_registry)
    spec = FlowSpec(
        "example",
        "planned",
        "fixture",
        (FlowNode("planned", "fake.planned"),),
        (FlowTarget("planned", ("planned",)),),
        action_bindings=(ActionBinding("fake.planned", "fake-planned"),),
    )

    with pytest.raises(FlowContractError, match="no registered domain planner"):
        engine.plan(spec, "planned")

    flow_registry.register_action_planner(
        "fake.planned",
        lambda _node: ActionPlan(
            "fake.intent",
            object(),
            {"value": 1},
            (member,),
        ),
    )
    plan = engine.plan(spec, "planned")
    assert engine.preflight(plan, ExecutionEnvironment()).status == "ready"
    assert engine.plan_record(plan)["nodes"][0]["action_plan"] == {
        "kind": "fake.intent",
        "record": {"value": 1},
        "sources": [
            {
                "scope": "project",
                "path": "intent.toml",
                "sha256": hashlib.sha256(b"value = 1\n").hexdigest(),
                "executable": False,
            }
        ],
    }

    source_path.write_text("value = 2\n", encoding="utf-8")
    result = engine.preflight(plan, ExecutionEnvironment())

    assert result.status == "blocked"
    assert any(
        check.requirement_kind == "action-plan-source"
        and check.status == "changed"
        for check in result.checks
    )


def fake_bindings() -> tuple[ActionBinding, ...]:
    return (
        ActionBinding("fake.source", "fake-source"),
        ActionBinding("fake.transform", "fake-transform"),
        ActionBinding("fake.verify", "fake-verify"),
    )


def flow_spec(
    *,
    text: str = "hello",
    transform_policy: str | None = None,
    diagnostic_binding: bool = False,
    foreign_evidence: bool = False,
) -> FlowSpec:
    return FlowSpec(
        owner="example",
        flow_id="fake-pipeline",
        recipe_id="fake-recipe",
        nodes=(
            FlowNode(
                node_id="source",
                action_kind="fake.source",
                config={"text": text},
            ),
            FlowNode(
                node_id="transform",
                action_kind="fake.transform",
                config={"foreign_evidence": foreign_evidence},
                policy=transform_policy,
                bindings=(
                    ArtifactBinding(
                        input="input",
                        producer="source",
                        output="source",
                    ),
                ),
            ),
            FlowNode(
                node_id="verify",
                action_kind="fake.verify",
                config={"expected": text.upper()},
                policy="verify-accepted",
                bindings=(
                    ArtifactBinding(
                        input="candidate",
                        producer="transform",
                        output="transformed",
                        requires="valid" if diagnostic_binding else "accepted",
                    ),
                ),
            ),
        ),
        targets=(FlowTarget("qualification", ("verify",)),),
        policies=(
            PolicySpec(
                policy_id="verify-accepted",
                checks=(PolicyCheck("accepted", "accepted", "equals", True),),
            ),
            *(
                (
                    PolicySpec(
                        policy_id="long-text",
                        checks=(PolicyCheck("long", "length", "at_least", 100),),
                    ),
                )
                if transform_policy == "long-text"
                else ()
            ),
        ),
        action_bindings=fake_bindings(),
    )


def test_plan_validates_typed_bindings_before_creating_a_run(tmp_path: Path) -> None:
    registered, *_ = registry()
    registered.register_action(
        ActionContract(
            kind="fake.wrong-source",
            outputs=(ArtifactPort("source", "netlist.verilog"),),
            adapters=("fake-source",),
        )
    )
    invalid = flow_spec()
    invalid = FlowSpec(
        owner=invalid.owner,
        flow_id=invalid.flow_id,
        recipe_id=invalid.recipe_id,
        nodes=(
            FlowNode(
                node_id="source",
                action_kind="fake.wrong-source",
                config={"text": "hello"},
            ),
            *invalid.nodes[1:],
        ),
        targets=invalid.targets,
        policies=invalid.policies,
        action_bindings=(
            ActionBinding("fake.wrong-source", "fake-source"),
            *invalid.action_bindings[1:],
        ),
    )

    with pytest.raises(FlowContractError, match="netlist.verilog.*text.plain"):
        FlowEngine(registered).plan(
            invalid,
            "qualification",
        )

    assert not (tmp_path / "artifacts").exists()


def test_plan_validates_only_the_selected_target_closure() -> None:
    registered, *_ = registry()
    spec = flow_spec()
    spec = FlowSpec(
        owner=spec.owner,
        flow_id=spec.flow_id,
        recipe_id=spec.recipe_id,
        nodes=(
            *spec.nodes,
            FlowNode(
                node_id="unselected-external-tool",
                action_kind="unavailable.external-tool",
            ),
        ),
        targets=spec.targets,
        policies=spec.policies,
        action_bindings=(
            *spec.action_bindings,
            ActionBinding("unavailable.external-tool", "missing-adapter"),
        ),
    )

    plan = FlowEngine(registered).plan(spec, "qualification")

    assert plan.topology == ("source", "transform", "verify")
    assert all(
        node.node.action_kind != "unavailable.external-tool"
        for node in plan.nodes
    )


def test_plan_rejects_an_unavailable_action_in_the_selected_target_closure() -> None:
    registered, *_ = registry()
    spec = FlowSpec(
        owner="example",
        flow_id="selected-external-tool",
        recipe_id="selected-external-tool-recipe",
        nodes=(
            FlowNode(
                node_id="selected-external-tool",
                action_kind="unavailable.external-tool",
            ),
        ),
        targets=(FlowTarget("all", ("selected-external-tool",)),),
        action_bindings=(
            ActionBinding("unavailable.external-tool", "missing-adapter"),
        ),
    )

    with pytest.raises(FlowContractError, match="unknown Action"):
        FlowEngine(registered).plan(spec, "all")


def test_fake_vertical_slice_writes_stable_records(tmp_path: Path) -> None:
    registered, source, transform, verify = registry()
    engine = FlowEngine(registered)
    plan = engine.plan(flow_spec(), "qualification")

    assert plan.topology == ("source", "transform", "verify")
    result = engine.run(
        plan,
        artifact_root=tmp_path / "artifacts",
        environment=ExecutionEnvironment(),
        run_id="a" * 32,
    )

    assert result.status == "accepted"
    assert [source.executions, transform.executions, verify.executions] == [1, 1, 1]
    assert result.nodes["verify"].policy_status == "accepted"
    assert (result.run_root / "inputs/resolved_plan.json").is_file()
    assert (result.run_root / "inputs/preflight.json").is_file()
    assert (result.run_root / "outputs/flow_result.json").is_file()
    assert (result.run_root / "run_manifest.json").is_file()
    operation_ids = set()
    for node in plan.topology:
        request_path = result.run_root / "inputs" / node / "action_request.json"
        result_path = result.run_root / "outputs" / node / "action_result.json"
        assert request_path.is_file()
        assert result_path.is_file()
        assert (result.run_root / "outputs" / node / "policy_receipt.json").is_file()
        request = json.loads(request_path.read_text(encoding="utf-8"))
        action_result = json.loads(result_path.read_text(encoding="utf-8"))
        assert request["operation_id"] == action_result["operation_id"]
        assert result.nodes[node].operation_id == request["operation_id"]
        assert action_result["incident_reference"] is None
        operation_ids.add(request["operation_id"])
    assert len(operation_ids) == len(plan.topology)

    encoded_root = str(result.run_root).encode()
    for record in result.run_root.rglob("*.json"):
        assert encoded_root not in record.read_bytes()

    restored = engine.restore_result(
        plan,
        artifact_root=tmp_path / "artifacts",
        run_id="a" * 32,
    )
    assert restored == result


def test_flow_action_backlinks_a_workspace_incident(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    class IncidentOperation:
        def __init__(self, operation_id: str) -> None:
            self.operation_id = operation_id

        def register_artifact(self, record) -> None:
            record.bind_operation(self.operation_id)
            incident = write_operation_incident(
                workspace_root=workspace,
                artifact_root=record.paths.artifact_root,
                operation_id=self.operation_id,
                name="fixture workspace failure",
                policy="direct-mutation",
                status="failed",
                error=RuntimeError("fixture workspace failure"),
                uncertain_reason=None,
                view_snapshots=(),
                ownership_scopes=(),
            )
            record.attach_incident(incident)

    class IncidentAdapter(SourceAdapter):
        def execute(self, context: ActionContext) -> AdapterExecution:
            assert context.operation_id is not None
            context.bind_workspace_operation(IncidentOperation(context.operation_id))
            raise RuntimeError("fixture workspace failure")

    adapter = IncidentAdapter()
    registered = FlowRegistry()
    registered.register_action(
        ActionContract(
            "fake.incident",
            outputs=(ArtifactPort("source", "text.plain"),),
            adapters=("fake-incident",),
        )
    )
    registered.register_adapter("fake-incident", adapter)
    engine = FlowEngine(registered)
    plan = engine.plan(
        FlowSpec(
            owner="example",
            flow_id="incident-flow",
            recipe_id="incident-flow-recipe",
            nodes=(FlowNode("source", "fake.incident", {"text": "unused"}),),
            targets=(FlowTarget("all", ("source",)),),
            action_bindings=(ActionBinding("fake.incident", "fake-incident"),),
        ),
        "all",
    )

    result = engine.run(
        plan,
        artifact_root=tmp_path / "artifacts",
        environment=ExecutionEnvironment(),
        run_id="b" * 32,
    )

    outcome = result.nodes["source"]
    assert outcome.status == "failed"
    assert outcome.operation_id is not None
    assert outcome.incident_reference == (
        f"system/operations/{outcome.operation_id}/incident.json"
    )
    action_result = json.loads(
        (result.run_root / "outputs/source/action_result.json").read_text(
            encoding="utf-8"
        )
    )
    assert action_result["incident_reference"] == outcome.incident_reference
def test_restore_compares_the_exact_immutable_plan_identity(tmp_path: Path) -> None:
    registered, *_ = registry()
    engine = FlowEngine(registered)
    first = engine.plan(flow_spec(), "qualification")
    changed_spec = FlowSpec(
        owner=first.spec.owner,
        flow_id=first.spec.flow_id,
        recipe_id=first.spec.recipe_id,
        nodes=tuple(
            FlowNode(
                node.node_id,
                node.action_kind,
                {**node.config, "text": "changed-after-run"},
                node.bindings,
                node.order_after,
                node.policy,
            )
            if node.node_id == "source"
            else node
            for node in first.spec.nodes
        ),
        targets=first.spec.targets,
        policies=first.spec.policies,
        action_bindings=first.spec.action_bindings,
        source_members=first.spec.source_members,
        owner_root=first.spec.owner_root,
    )
    changed = engine.plan(changed_spec, "qualification")
    assert engine.plan_identity(first) != engine.plan_identity(changed)
    assert engine.plan_record(first) != engine.plan_record(changed)
    engine.run(
        first,
        artifact_root=tmp_path / "artifacts",
        environment=ExecutionEnvironment(),
        run_id="record-restore",
    )

    with pytest.raises(FlowExecutionError, match="Manifest identity"):
        engine.restore_result(
            changed,
            artifact_root=tmp_path / "artifacts",
            run_id="record-restore",
        )


def test_plan_identity_covers_target_acceptance_goals() -> None:
    registered, *_ = registry()
    engine = FlowEngine(registered)
    spec = flow_spec()
    changed = replace(
        spec,
        targets=(FlowTarget("qualification", ("source", "verify")),),
    )

    assert engine.plan_identity(
        engine.plan(spec, "qualification")
    ) != engine.plan_identity(
        engine.plan(changed, "qualification")
    )


def test_restore_rejects_action_operation_record_drift(tmp_path: Path) -> None:
    registered, *_ = registry()
    engine = FlowEngine(registered)
    plan = engine.plan(flow_spec(), "qualification")
    result = engine.run(
        plan,
        artifact_root=tmp_path / "artifacts",
        environment=ExecutionEnvironment(),
        run_id="operation-restore",
    )
    request_path = result.run_root / "inputs/source/action_request.json"
    request = json.loads(request_path.read_text(encoding="utf-8"))
    request["operation_id"] = "c" * 32
    request_path.write_text(
        json.dumps(request, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(FlowExecutionError, match="Action record identity drift"):
        engine.restore_result(
            plan,
            artifact_root=tmp_path / "artifacts",
            run_id=result.run_id,
        )


def test_node_cannot_claim_another_nodes_file_as_evidence(tmp_path: Path) -> None:
    registered, *_ = registry()
    engine = FlowEngine(registered)
    result = engine.run(
        engine.plan(
            flow_spec(foreign_evidence=True),
            "qualification",
        ),
        artifact_root=tmp_path / "artifacts",
        environment=ExecutionEnvironment(),
        run_id="8" * 32,
    )

    assert result.status == "failed"
    assert result.nodes["transform"].result_status == "failed"
    assert "Evidence is not a managed regular file" in str(
        result.nodes["transform"].reason
    )


def test_diagnostic_binding_can_consume_valid_rejected_artifact(tmp_path: Path) -> None:
    registered, _source, transform, verify = registry()
    engine = FlowEngine(registered)
    diagnostic = engine.run(
        engine.plan(
            flow_spec(transform_policy="long-text", diagnostic_binding=True),
            "qualification",
        ),
        artifact_root=tmp_path / "artifacts",
        environment=ExecutionEnvironment(),
        run_id="b" * 32,
    )

    assert diagnostic.nodes["transform"].status == "rejected"
    assert diagnostic.nodes["verify"].status == "accepted"
    assert diagnostic.status == "accepted"
    assert [transform.executions, verify.executions] == [1, 1]

    strict = engine.run(
        engine.plan(
            flow_spec(transform_policy="long-text", diagnostic_binding=False),
            "qualification",
        ),
        artifact_root=tmp_path / "artifacts",
        environment=ExecutionEnvironment(),
        run_id="c" * 32,
    )
    assert strict.nodes["transform"].status == "rejected"
    assert strict.nodes["verify"].status == "blocked"
    assert strict.status == "failed"


def test_run_identity_is_immutable(tmp_path: Path) -> None:
    registered, source, transform, verify = registry()
    engine = FlowEngine(registered)
    artifact_root = tmp_path / "artifacts"
    run_id = "d" * 32

    first = engine.run(
        engine.plan(flow_spec(text="hello"), "qualification"),
        artifact_root=artifact_root,
        environment=ExecutionEnvironment(),
        run_id=run_id,
    )
    with pytest.raises(FlowExecutionError, match="already exists"):
        engine.run(
            engine.plan(flow_spec(text="goodbye"), "qualification"),
            artifact_root=artifact_root,
            environment=ExecutionEnvironment(),
            run_id=run_id,
        )

    assert first.status == "accepted"
    assert [source.executions, transform.executions, verify.executions] == [1, 1, 1]


class TerminalAdapter(StagedAdapterFixture):
    def __init__(self, result_status: str | None = None) -> None:
        self.result_status = result_status
        self.executions = 0

    def validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        return ()

    def prepare(self, context: ActionContext) -> None:
        pass

    def execute(self, context: ActionContext) -> AdapterExecution:
        self.executions += 1
        if self.result_status is None:
            raise KeyboardInterrupt
        return AdapterExecution.succeeded()

    def collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        return CollectedActionResult(
            facts=FactSet.empty(
                context.action.fact_schema,
                source=FactSource(context.action.kind, context.node_id),
            ),
            status=str(self.result_status),
        )


class BadCollectAdapter(TerminalAdapter):
    def collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        raise FlowExecutionError("invalid collected evidence")


@pytest.mark.parametrize("result_status", ["partial", "uncertain"])
def test_non_valid_terminal_results_are_persisted(
    tmp_path: Path,
    result_status: str,
) -> None:
    registered = FlowRegistry()
    registered.register_action(
        ActionContract(kind="fake.terminal", adapters=("fake-terminal",))
    )
    registered.register_adapter("fake-terminal", TerminalAdapter(result_status))
    spec = FlowSpec(
        owner="example",
        flow_id="terminal",
        recipe_id="terminal-recipe",
        nodes=(FlowNode("terminal", "fake.terminal"),),
        targets=(FlowTarget("all", ("terminal",)),),
        action_bindings=(ActionBinding("fake.terminal", "fake-terminal"),),
    )
    engine = FlowEngine(registered)

    result = engine.run(
        engine.plan(spec, "all"),
        artifact_root=tmp_path / "artifacts",
        environment=ExecutionEnvironment(),
        run_id="e" * 32,
    )

    assert result.status == "failed"
    assert result.nodes["terminal"].result_status == result_status
    payload = json.loads(
        (result.run_root / "outputs/terminal/action_result.json").read_text()
    )
    assert payload["execution"]["status"] == "succeeded"
    assert payload["result_status"] == result_status


def test_result_collection_failure_preserves_successful_execution_status(
    tmp_path: Path,
) -> None:
    registered = FlowRegistry()
    registered.register_action(
        ActionContract(kind="fake.bad-collect", adapters=("fake-bad-collect",))
    )
    registered.register_adapter("fake-bad-collect", BadCollectAdapter("valid"))
    spec = FlowSpec(
        owner="example",
        flow_id="bad-collect",
        recipe_id="bad-collect-recipe",
        nodes=(FlowNode("collect", "fake.bad-collect"),),
        targets=(FlowTarget("all", ("collect",)),),
        action_bindings=(ActionBinding("fake.bad-collect", "fake-bad-collect"),),
    )
    engine = FlowEngine(registered)

    result = engine.run(
        engine.plan(spec, "all"),
        artifact_root=tmp_path / "artifacts",
        environment=ExecutionEnvironment(),
        run_id="7" * 32,
    )

    outcome = result.nodes["collect"]
    assert outcome.execution_status == "succeeded"
    assert outcome.result_status == "failed"
    assert "invalid collected evidence" in str(outcome.reason)


def test_flow_config_is_deeply_immutable_and_portable() -> None:
    node = FlowNode(
        "source",
        "fake.source",
        config={"nested": {"values": [1, 2]}},
    )

    with pytest.raises(TypeError):
        node.config["nested"]["values"][0] = 3  # type: ignore[index]
    with pytest.raises(FlowContractError, match="non-finite"):
        FlowNode(
            "invalid",
            "fake.source",
            config={"value": float("nan")},
        )


def test_interruption_writes_cancelled_terminal_records(tmp_path: Path) -> None:
    registered = FlowRegistry()
    registered.register_action(
        ActionContract(kind="fake.interrupt", adapters=("fake-interrupt",))
    )
    interrupted = TerminalAdapter()
    later = TerminalAdapter("valid")
    registered.register_adapter("fake-interrupt", interrupted)
    registered.register_action(
        ActionContract(kind="fake.later", adapters=("fake-later",))
    )
    registered.register_adapter("fake-later", later)
    spec = FlowSpec(
        owner="example",
        flow_id="interrupted",
        recipe_id="interrupted-recipe",
        nodes=(
            FlowNode("interrupt", "fake.interrupt"),
            FlowNode("later", "fake.later"),
        ),
        targets=(FlowTarget("all", ("interrupt", "later")),),
        action_bindings=(
            ActionBinding("fake.interrupt", "fake-interrupt"),
            ActionBinding("fake.later", "fake-later"),
        ),
    )
    engine = FlowEngine(registered)

    result = engine.run(
        engine.plan(spec, "all"),
        artifact_root=tmp_path / "artifacts",
        environment=ExecutionEnvironment(),
        run_id="f" * 32,
    )

    payload = json.loads(
        (result.run_root / "outputs/interrupt/action_result.json").read_text()
    )
    assert result.status == "failed"
    assert result.interrupted is True
    assert [interrupted.executions, later.executions] == [1, 0]
    assert result.nodes["later"].status == "blocked"
    assert payload["execution"]["status"] == "cancelled"
    assert payload["error"] == "interrupted"


def test_clean_rejects_manifest_paths_outside_the_run(tmp_path: Path) -> None:
    registered, *_ = registry()
    engine = FlowEngine(registered)
    artifact_root = tmp_path / "artifacts"
    run_id = "2" * 32
    result = engine.run(
        engine.plan(flow_spec(), "qualification"),
        artifact_root=artifact_root,
        environment=ExecutionEnvironment(),
        run_id=run_id,
    )
    manifest_path = result.run_root / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["managed_paths"].append("../../outside")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RunStoreError, match="unsafe managed path"):
        _run_store(tmp_path, artifact_root).clean(
            owner="example",
            target="fake-pipeline",
            operation="qualification",
            run_id=run_id,
        )

    assert result.run_root.is_dir()
