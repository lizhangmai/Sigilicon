from __future__ import annotations

import json
import inspect
from pathlib import Path

import pytest

from sigilicon.cli.flow_core import main as flow_cli_main
from sigilicon.flow import (
    ActionContext,
    ActionContract,
    ActionConfiguration,
    AdapterExecution,
    AdapterConfiguration,
    AdapterSelection,
    ArtifactBinding,
    ArtifactPort,
    CollectedActionResult,
    ExecutionProfile,
    EvidenceEnvelope,
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
    fake_profile,
    load_flow_contract,
)
from sigilicon.virtuoso.operation_journal import write_operation_incident

from conftest import (
    StagedAdapterFixture,
    write_component_owner,
    write_fake_flow_extension,
    write_project_context,
)


class SourceAdapter(StagedAdapterFixture):
    def __init__(self) -> None:
        self.executions = 0
        self.last_evidence: EvidenceEnvelope | None = None

    def validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        return ()

    def prepare(self, context: ActionContext) -> None:
        pass

    def execute(self, context: ActionContext) -> AdapterExecution:
        self.executions += 1
        self.last_evidence = context.evidence
        output = context.output_path("source", "value.txt")
        output.write_text(str(context.action_config["text"]), encoding="utf-8")
        if context.action_config.get("internal_symlink"):
            target = context.work_root / "target.txt"
            target.write_text("managed target\n", encoding="utf-8")
            (context.work_root / "link.txt").symlink_to(target.name)
        return AdapterExecution.succeeded()

    def collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        return CollectedActionResult(
            artifacts=(
                ProducedArtifact(
                    role="source",
                    kind="text.plain",
                    path=context.output_path("source", "value.txt"),
                    qualifiers=context.action_config.get("qualifiers", {}),
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
            artifacts=(
                ProducedArtifact(
                    role="transformed",
                    kind="text.plain",
                    path=output,
                    qualifiers=context.input("input").qualifiers,
                ),
            ),
            facts={"length": len(output.read_text(encoding="utf-8"))},
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
        return AdapterExecution.succeeded(details={"accepted": value == expected})

    def collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        return CollectedActionResult(
            artifacts=(
                ProducedArtifact(
                    role="report",
                    kind="report.text",
                    path=context.output_path("report", "verification.txt"),
                ),
            ),
            facts={"accepted": bool(execution.details["accepted"])},
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
            facts=("length",),
            adapters=("fake-transform",),
        )
    )
    result.register_action(
        ActionContract(
            kind="fake.verify",
            inputs=(ArtifactPort("candidate", "text.plain"),),
            outputs=(ArtifactPort("report", "report.text"),),
            facts=("accepted",),
            adapters=("fake-verify",),
        )
    )
    result.register_adapter("fake-source", source)
    result.register_adapter("fake-transform", transform)
    result.register_adapter("fake-verify", verify)
    return result, source, transform, verify


def flow_spec(
    *,
    text: str = "hello",
    qualifiers: dict[str, str] | None = None,
    transform_policy: str | None = None,
    diagnostic_binding: bool = False,
    internal_symlink: bool = False,
    foreign_evidence: bool = False,
) -> FlowSpec:
    return FlowSpec(
        owner="example",
        flow_id="fake-pipeline",
        nodes=(
            FlowNode(
                node_id="source",
                action_kind="fake.source",
                config={
                    "text": text,
                    "qualifiers": qualifiers or {},
                    "internal_symlink": internal_symlink,
                },
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
        nodes=(
            FlowNode(
                node_id="source",
                action_kind="fake.wrong-source",
                config={"text": "hello"},
            ),
            *invalid.nodes[1:],
        ),
        targets=invalid.targets,
    )

    with pytest.raises(FlowContractError, match="netlist.verilog.*text.plain"):
        FlowEngine(registered).plan(
            invalid,
            "qualification",
            ExecutionProfile(
                owner="example",
                profile_id="fake",
                selections=(
                    AdapterSelection("fake.wrong-source", "fake-source"),
                    AdapterSelection("fake.transform", "fake-transform"),
                    AdapterSelection("fake.verify", "fake-verify"),
                ),
            ),
        )

    assert not (tmp_path / "artifacts").exists()


def test_plan_validates_only_the_selected_target_closure() -> None:
    registered, *_ = registry()
    spec = flow_spec()
    spec = FlowSpec(
        owner=spec.owner,
        flow_id=spec.flow_id,
        nodes=(
            *spec.nodes,
            FlowNode(
                node_id="unselected-external-tool",
                action_kind="unavailable.external-tool",
            ),
        ),
        targets=spec.targets,
        policies=spec.policies,
    )

    plan = FlowEngine(registered).plan(spec, "qualification", fake_profile())

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
        nodes=(
            FlowNode(
                node_id="selected-external-tool",
                action_kind="unavailable.external-tool",
            ),
        ),
        targets=(FlowTarget("all", ("selected-external-tool",)),),
    )

    with pytest.raises(FlowContractError, match="unknown Action"):
        FlowEngine(registered).plan(spec, "all", fake_profile())


def test_plan_compiles_typed_config_and_evidence_envelope(tmp_path: Path) -> None:
    registered, source_adapter, *_ = registry()
    spec = flow_spec()
    source = spec.node("source")
    typed_spec = FlowSpec(
        owner=spec.owner,
        flow_id=spec.flow_id,
        nodes=(
            FlowNode(
                node_id=source.node_id,
                action_kind=source.action_kind,
                config={
                    **source.config,
                    "evidence_role": "diagnostic",
                    "evidence_level": "l1",
                    "evidence_scope": "source-contract",
                },
            ),
            *spec.nodes[1:],
        ),
        targets=spec.targets,
        policies=spec.policies,
    )

    plan = FlowEngine(registered).plan(
        typed_spec,
        "qualification",
        fake_profile(),
    )
    planned = plan.planned_node("source")

    assert isinstance(planned.action_config, ActionConfiguration)
    assert planned.action_config.action_kind == "fake.source"
    assert isinstance(planned.adapter_config, AdapterConfiguration)
    assert planned.adapter_config.adapter == "fake-source"
    assert planned.evidence == EvidenceEnvelope(
        role="diagnostic",
        level="l1",
        scope="source-contract",
    )
    assert FlowEngine(registered).plan_record(plan)["nodes"][0]["evidence"] == {
        "role": "diagnostic",
        "level": "l1",
        "scope": "source-contract",
    }

    FlowEngine(registered).run(
        plan,
        artifact_root=tmp_path / "artifacts",
        run_id="evidence-context",
    )

    assert source_adapter.last_evidence is planned.evidence


@pytest.mark.parametrize(
    "config, message",
    (
        ({"evidence_role": "diagnostic"}, "missing fields"),
        (
            {
                "evidence_role": "observation",
                "evidence_level": "l1",
                "evidence_scope": "source-contract",
            },
            "unsupported evidence role",
        ),
        (
            {
                "evidence_role": "diagnostic",
                "evidence_level": "cell",
                "evidence_scope": "source-contract",
            },
            "unsupported evidence level",
        ),
    ),
)
def test_plan_rejects_invalid_evidence_envelopes(
    config: dict[str, str],
    message: str,
) -> None:
    registered, *_ = registry()
    spec = FlowSpec(
        owner="example",
        flow_id="invalid-evidence",
        nodes=(FlowNode("source", "fake.source", {"text": "x", **config}),),
        targets=(FlowTarget("all", ("source",)),),
    )
    profile = ExecutionProfile(
        owner="example",
        profile_id="fake",
        selections=(AdapterSelection("fake.source", "fake-source"),),
    )

    with pytest.raises(FlowContractError, match=message):
        FlowEngine(registered).plan(spec, "all", profile)


def test_registry_adds_owner_adapter_only_to_an_extensible_action() -> None:
    registered = FlowRegistry()
    registered.register_action(
        ActionContract(
            kind="fake.owner-qualified",
            adapter_extensible=True,
        )
    )
    adapter = SourceAdapter()

    registered.register_action_adapter(
        "fake.owner-qualified",
        "fixture-owner-qualification",
        adapter,
    )

    assert registered.action("fake.owner-qualified").adapters == (
        "fixture-owner-qualification",
    )
    assert callable(registered.adapter("fixture-owner-qualification").run)

    registered.register_action(
        ActionContract(
            kind="fake.closed",
            adapters=("fake-source",),
        )
    )
    with pytest.raises(FlowContractError, match="does not accept Adapter extensions"):
        registered.register_action_adapter(
            "fake.closed",
            "fixture-closed",
            SourceAdapter(),
        )


def test_adapter_factory_is_materialized_only_when_a_plan_runs(tmp_path: Path) -> None:
    materialized: list[SourceAdapter] = []
    registered = FlowRegistry()
    registered.register_action(
        ActionContract(
            kind="fake.source",
            outputs=(ArtifactPort("source", "text.plain"),),
            adapters=("lazy-source",),
        )
    )

    def create_adapter() -> SourceAdapter:
        adapter = SourceAdapter()
        materialized.append(adapter)
        return adapter

    registered.register_adapter_factory("lazy-source", create_adapter)
    engine = FlowEngine(registered)
    spec = FlowSpec(
        owner="example",
        flow_id="lazy-adapter",
        nodes=(FlowNode("source", "fake.source", {"text": "hello"}),),
        targets=(FlowTarget("all", ("source",)),),
    )
    profile = ExecutionProfile(
        owner="example",
        profile_id="lazy",
        selections=(AdapterSelection("fake.source", "lazy-source"),),
    )

    plan = engine.plan(spec, "all", profile)

    assert materialized == []
    engine.run(plan, artifact_root=tmp_path / "artifacts", run_id="1" * 32)
    assert len(materialized) == 1



def test_fake_vertical_slice_writes_stable_records(tmp_path: Path) -> None:
    registered, source, transform, verify = registry()
    engine = FlowEngine(registered)
    plan = engine.plan(flow_spec(), "qualification", fake_profile())

    assert plan.topology == ("source", "transform", "verify")
    assert all(
        "design_campaign_iteration" not in node
        for node in engine.plan_record(plan)["nodes"]
    )
    assert "resume" not in inspect.signature(engine.run).parameters
    result = engine.run(
        plan,
        artifact_root=tmp_path / "artifacts",
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


@pytest.mark.parametrize("tamper", (None, "wrong-identity", "symlink"))
def test_flow_action_backlinks_a_workspace_incident(
    tmp_path: Path,
    tamper: str | None,
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
            "example",
            "incident-flow",
            (FlowNode("source", "fake.incident", {"text": "unused"}),),
            (FlowTarget("all", ("source",)),),
        ),
        "all",
        ExecutionProfile(
            "example",
            "incident",
            (AdapterSelection("fake.incident", "fake-incident"),),
        ),
    )

    result = engine.run(
        plan,
        artifact_root=tmp_path / "artifacts",
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
    operation = json.loads(
        (result.run_root / "inputs/source/operation.json").read_text(
            encoding="utf-8"
        )
    )
    assert operation == {
        "contract_kind": "flow-action-operation",
        "incident_reference": outcome.incident_reference,
        "node": "source",
        "operation_id": outcome.operation_id,
        "run_id": result.run_id,
        "schema": 1,
    }
    incident_path = tmp_path / "artifacts" / str(outcome.incident_reference)
    if tamper == "wrong-identity":
        incident = json.loads(incident_path.read_text(encoding="utf-8"))
        incident["operation_id"] = "c" * 32
        incident_path.write_text(
            json.dumps(incident, indent=2) + "\n",
            encoding="utf-8",
        )
    elif tamper == "symlink":
        saved = incident_path.with_name("saved-incident.json")
        incident_path.rename(saved)
        incident_path.symlink_to(saved.name)
    if tamper is None:
        assert engine.restore_result(
            plan,
            artifact_root=tmp_path / "artifacts",
            run_id=result.run_id,
        ) == result
    else:
        with pytest.raises(FlowExecutionError, match="operation incident"):
            engine.restore_result(
                plan,
                artifact_root=tmp_path / "artifacts",
                run_id=result.run_id,
            )


def test_flow_passes_declared_extensions_without_interpreting_the_payload(
    tmp_path: Path,
) -> None:
    class ExtensionAdapter(SourceAdapter):
        accepted_extensions = ("opaque_hint",)

        def __init__(self) -> None:
            super().__init__()
            self.received = None

        def execute(self, context: ActionContext) -> AdapterExecution:
            self.received = context.extensions["opaque_hint"]
            return super().execute(context)

    adapter = ExtensionAdapter()
    registered = FlowRegistry()
    registered.register_action(
        ActionContract(
            "fake.extended-source",
            outputs=(ArtifactPort("source", "text.plain"),),
            adapters=("fake-extended-source",),
            accepted_extensions=("opaque_hint",),
        )
    )
    registered.register_adapter("fake-extended-source", adapter)
    engine = FlowEngine(registered)
    plan = engine.plan(
        FlowSpec(
            "example",
            "extended-flow",
            (
                FlowNode(
                    "source",
                    "fake.extended-source",
                    {"text": "hello"},
                    extensions={"opaque_hint": {"iteration": 2}},
                ),
            ),
            (FlowTarget("all", ("source",)),),
        ),
        "all",
        ExecutionProfile(
            "example",
            "extended",
            (AdapterSelection("fake.extended-source", "fake-extended-source"),),
        ),
    )

    assert engine.plan_record(plan)["nodes"][0]["opaque_hint"] == {
        "iteration": 2
    }
    result = engine.run(
        plan,
        artifact_root=tmp_path / "artifacts",
        run_id="e" * 32,
    )
    request = json.loads(
        (result.run_root / "inputs/source/action_request.json").read_text(
            encoding="utf-8"
        )
    )
    assert request["opaque_hint"] == {"iteration": 2}
    assert adapter.received == {"iteration": 2}


def test_flow_rejects_extensions_not_declared_by_action_or_adapter() -> None:
    accepted_adapter = SourceAdapter()
    accepted_adapter.accepted_extensions = ("opaque_hint",)
    action_rejects = FlowRegistry()
    action_rejects.register_action(
        ActionContract(
            "fake.source",
            outputs=(ArtifactPort("source", "text.plain"),),
            adapters=("fake-source",),
        )
    )
    action_rejects.register_adapter("fake-source", accepted_adapter)
    spec = FlowSpec(
        "example",
        "extended-flow",
        (
            FlowNode(
                "source",
                "fake.source",
                {"text": "hello"},
                extensions={"opaque_hint": {}},
            ),
        ),
        (FlowTarget("all", ("source",)),),
    )
    profile = ExecutionProfile(
        "example",
        "extended",
        (AdapterSelection("fake.source", "fake-source"),),
    )

    with pytest.raises(FlowContractError, match="Action does not accept"):
        FlowEngine(action_rejects).plan(spec, "all", profile)

    adapter_rejects = FlowRegistry()
    adapter_rejects.register_action(
        ActionContract(
            "fake.source",
            outputs=(ArtifactPort("source", "text.plain"),),
            adapters=("fake-source",),
            accepted_extensions=("opaque_hint",),
        )
    )
    adapter_rejects.register_adapter("fake-source", SourceAdapter())
    with pytest.raises(FlowContractError, match="Adapter does not consume"):
        FlowEngine(adapter_rejects).plan(spec, "all", profile)


def test_restore_compares_the_exact_plan_not_only_its_semantic_id(tmp_path: Path) -> None:
    registered, *_ = registry()
    engine = FlowEngine(registered)
    first = engine.plan(flow_spec(), "qualification", fake_profile())
    changed_spec = FlowSpec(
        first.spec.owner,
        first.spec.flow_id,
        tuple(
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
        first.spec.targets,
        first.spec.policies,
    )
    changed = engine.plan(changed_spec, "qualification", fake_profile())
    assert engine.plan_id(first) == engine.plan_id(changed)
    assert engine.plan_record(first) != engine.plan_record(changed)
    engine.run(first, artifact_root=tmp_path / "artifacts", run_id="record-restore")

    with pytest.raises(FlowExecutionError, match="Plan record drift"):
        engine.restore_result(
            changed,
            artifact_root=tmp_path / "artifacts",
            run_id="record-restore",
        )


def test_restore_rejects_action_operation_record_drift(tmp_path: Path) -> None:
    registered, *_ = registry()
    engine = FlowEngine(registered)
    plan = engine.plan(flow_spec(), "qualification", fake_profile())
    result = engine.run(
        plan,
        artifact_root=tmp_path / "artifacts",
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


def test_restore_rejects_legacy_flow_result_schema(tmp_path: Path) -> None:
    registered, *_ = registry()
    engine = FlowEngine(registered)
    plan = engine.plan(flow_spec(), "qualification", fake_profile())
    result = engine.run(
        plan,
        artifact_root=tmp_path / "artifacts",
        run_id="legacy-result",
    )
    result_path = result.run_root / "outputs/flow_result.json"
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    payload["schema"] = 1
    result_path.write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(FlowExecutionError, match="unsupported.*schema"):
        engine.restore_result(
            plan,
            artifact_root=tmp_path / "artifacts",
            run_id=result.run_id,
        )


def test_flow_run_manifest_owns_internal_tool_symlinks_by_lexical_path(
    tmp_path: Path,
) -> None:
    registered, *_ = registry()
    engine = FlowEngine(registered)
    artifact_root = tmp_path / "artifacts"
    run_id = "9" * 32
    result = engine.run(
        engine.plan(
            flow_spec(internal_symlink=True),
            "qualification",
            fake_profile(),
        ),
        artifact_root=artifact_root,
        run_id=run_id,
    )
    manifest = json.loads((result.run_root / "run_manifest.json").read_text())

    assert "work/source/link.txt" in manifest["managed_paths"]
    assert len(manifest["managed_paths"]) == len(set(manifest["managed_paths"]))
    engine.clean_run(
        artifact_root=artifact_root,
        owner="example",
        flow_id="fake-pipeline",
        target="qualification",
        run_id=run_id,
    )
    assert not result.run_root.exists()


def test_node_cannot_claim_another_nodes_file_as_evidence(tmp_path: Path) -> None:
    registered, *_ = registry()
    engine = FlowEngine(registered)
    result = engine.run(
        engine.plan(
            flow_spec(foreign_evidence=True),
            "qualification",
            fake_profile(),
        ),
        artifact_root=tmp_path / "artifacts",
        run_id="8" * 32,
    )

    assert result.status == "failed"
    assert result.nodes["transform"].result_status == "failed"
    assert "Evidence is not a managed regular file" in str(
        result.nodes["transform"].reason
    )


def test_artifact_qualifiers_propagate_without_derived_identities(
    tmp_path: Path,
) -> None:
    registered, *_ = registry()
    engine = FlowEngine(registered)
    artifact_root = tmp_path / "artifacts"
    first_plan = engine.plan(
        flow_spec(qualifiers={"variant": "variant_a", "corner": "nominal_a"}),
        "qualification",
        fake_profile(),
    )
    first_run = engine.run(first_plan, artifact_root=artifact_root, run_id="9" * 32)

    source_result = json.loads(
        (first_run.run_root / "outputs/source/action_result.json").read_text()
    )
    transform_request = json.loads(
        (first_run.run_root / "inputs/transform/action_request.json").read_text()
    )
    assert source_result["artifacts"]["source"]["qualifiers"] == {
        "corner": "nominal_a",
        "variant": "variant_a",
    }
    assert transform_request["inputs"]["input"]["qualifiers"] == {
        "corner": "nominal_a",
        "variant": "variant_a",
    }

    product_plan = engine.plan(
        flow_spec(qualifiers={"variant": "variant_b", "corner": "nominal_b"}),
        "qualification",
        fake_profile(),
    )
    product = engine.run(
        product_plan,
        artifact_root=artifact_root,
        run_id="8" * 32,
    )

    assert product.nodes["source"].artifacts["source"].qualifiers == {
        "corner": "nominal_b",
        "variant": "variant_b",
    }


def test_diagnostic_binding_can_consume_valid_rejected_artifact(tmp_path: Path) -> None:
    registered, _source, transform, verify = registry()
    engine = FlowEngine(registered)
    diagnostic = engine.run(
        engine.plan(
            flow_spec(transform_policy="long-text", diagnostic_binding=True),
            "qualification",
            fake_profile(),
        ),
        artifact_root=tmp_path / "artifacts",
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
            fake_profile(),
        ),
        artifact_root=tmp_path / "artifacts",
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
        engine.plan(flow_spec(text="hello"), "qualification", fake_profile()),
        artifact_root=artifact_root,
        run_id=run_id,
    )
    with pytest.raises(FlowExecutionError, match="already exists"):
        engine.run(
            engine.plan(flow_spec(text="goodbye"), "qualification", fake_profile()),
            artifact_root=artifact_root,
            run_id=run_id,
        )

    assert first.status == "accepted"
    assert [source.executions, transform.executions, verify.executions] == [1, 1, 1]


def test_flow_contract_loader_supports_only_the_current_schema(tmp_path: Path) -> None:
    owner_root = tmp_path / "ip/example"
    owner_root.mkdir(parents=True)
    contract = owner_root / "flow.toml"
    contract.write_text(
        '''schema = 2
contract_kind = "flow"
path_scope = "owner"
owner = "example"
name = "pipeline"

[[nodes]]
id = "source"
action = "fake.source"

[[targets]]
name = "all"
goals = ["source"]
''',
        encoding="utf-8",
    )

    with pytest.raises(FlowContractError, match="schema 1"):
        load_flow_contract(contract)


def test_flow_contract_loads_owner_policy_without_a_runtime_registry(
    tmp_path: Path,
) -> None:
    contract = tmp_path / "flow.toml"
    contract.write_text(
        '''schema = 1
contract_kind = "flow"
path_scope = "owner"
owner = "example"
name = "pipeline"

[[nodes]]
id = "verify"
action = "fake.verify"
policy = "accepted"

[[targets]]
name = "all"
goals = ["verify"]

[[policies]]
id = "accepted"

[[policies.checks]]
id = "accepted"
fact = "accepted"
operator = "equals"
expected = true
''',
        encoding="utf-8",
    )

    loaded = load_flow_contract(contract)

    assert loaded.policy("accepted").checks[0].expected is True


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
        return CollectedActionResult(status=str(self.result_status))


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
        nodes=(FlowNode("terminal", "fake.terminal"),),
        targets=(FlowTarget("all", ("terminal",)),),
    )
    engine = FlowEngine(registered)
    profile = ExecutionProfile(
        "example",
        "terminal",
        (AdapterSelection("fake.terminal", "fake-terminal"),),
    )

    result = engine.run(
        engine.plan(spec, "all", profile),
        artifact_root=tmp_path / "artifacts",
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
        nodes=(FlowNode("collect", "fake.bad-collect"),),
        targets=(FlowTarget("all", ("collect",)),),
    )
    engine = FlowEngine(registered)
    profile = ExecutionProfile(
        "example",
        "bad-collect",
        (AdapterSelection("fake.bad-collect", "fake-bad-collect"),),
    )

    result = engine.run(
        engine.plan(spec, "all", profile),
        artifact_root=tmp_path / "artifacts",
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
        nodes=(
            FlowNode("interrupt", "fake.interrupt"),
            FlowNode("later", "fake.later"),
        ),
        targets=(FlowTarget("all", ("interrupt", "later")),),
    )
    engine = FlowEngine(registered)
    profile = ExecutionProfile(
        "example",
        "interrupted",
        (
            AdapterSelection("fake.interrupt", "fake-interrupt"),
            AdapterSelection("fake.later", "fake-later"),
        ),
    )

    result = engine.run(
        engine.plan(spec, "all", profile),
        artifact_root=tmp_path / "artifacts",
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


def test_progress_interruption_writes_cancelled_terminal_records(
    tmp_path: Path,
) -> None:
    registered = FlowRegistry()
    registered.register_action(
        ActionContract(kind="fake.interrupt", adapters=("fake-interrupt",))
    )
    adapter = TerminalAdapter("valid")
    registered.register_adapter("fake-interrupt", adapter)
    spec = FlowSpec(
        owner="example",
        flow_id="progress-interrupted",
        nodes=(FlowNode("interrupt", "fake.interrupt"),),
        targets=(FlowTarget("all", ("interrupt",)),),
    )
    engine = FlowEngine(registered)
    profile = ExecutionProfile(
        "example",
        "progress-interrupted",
        (AdapterSelection("fake.interrupt", "fake-interrupt"),),
    )

    def interrupt_current_node(progress: FlowProgress) -> None:
        if progress.current_node == "interrupt":
            raise KeyboardInterrupt

    result = engine.run(
        engine.plan(spec, "all", profile),
        artifact_root=tmp_path / "artifacts",
        run_id="e" * 32,
        progress=interrupt_current_node,
    )

    action_payload = json.loads(
        (result.run_root / "outputs/interrupt/action_result.json").read_text()
    )
    flow_payload = json.loads(
        (result.run_root / "outputs/flow_result.json").read_text()
    )
    assert adapter.executions == 0
    assert result.status == "failed"
    assert result.interrupted is True
    assert action_payload["execution"]["status"] == "cancelled"
    assert action_payload["error"] == "interrupted"
    assert flow_payload["interrupted"] is True
    assert flow_payload["nodes"]["interrupt"]["execution_status"] == "cancelled"


def test_clean_is_manifest_driven_and_refuses_untracked_paths(tmp_path: Path) -> None:
    registered, *_ = registry()
    engine = FlowEngine(registered)
    artifact_root = tmp_path / "artifacts"
    run_id = "1" * 32
    result = engine.run(
        engine.plan(flow_spec(), "qualification", fake_profile()),
        artifact_root=artifact_root,
        run_id=run_id,
    )
    untracked = result.run_root / "do-not-delete.txt"
    untracked.write_text("owned by caller", encoding="utf-8")

    with pytest.raises(FlowExecutionError, match="untracked"):
        engine.clean_run(
            artifact_root=artifact_root,
            owner="example",
            flow_id="fake-pipeline",
            target="qualification",
            run_id=run_id,
        )

    assert untracked.read_text(encoding="utf-8") == "owned by caller"
    untracked.unlink()
    engine.clean_run(
        artifact_root=artifact_root,
        owner="example",
        flow_id="fake-pipeline",
        target="qualification",
        run_id=run_id,
    )
    assert not result.run_root.exists()


def test_policy_change_requires_a_new_run(
    tmp_path: Path,
) -> None:
    registered, source, transform, verify = registry()
    engine = FlowEngine(registered)
    artifact_root = tmp_path / "artifacts"
    run_id = "6" * 32
    original = flow_spec()
    first = engine.run(
        engine.plan(original, "qualification", fake_profile()),
        artifact_root=artifact_root,
        run_id=run_id,
    )
    changed_policy = FlowSpec(
        owner=original.owner,
        flow_id=original.flow_id,
        nodes=original.nodes,
        targets=original.targets,
        policies=(
            PolicySpec(
                policy_id="verify-accepted",
                checks=(PolicyCheck("accepted", "accepted", "equals", False),),
            ),
        ),
    )
    changed = engine.run(
        engine.plan(changed_policy, "qualification", fake_profile()),
        artifact_root=artifact_root,
        run_id="7" * 32,
    )

    assert first.status == "accepted"
    assert changed.status == "failed"
    assert changed.nodes["verify"].status == "rejected"
    assert [source.executions, transform.executions, verify.executions] == [2, 2, 2]


def test_clean_rejects_manifest_paths_outside_the_run(tmp_path: Path) -> None:
    registered, *_ = registry()
    engine = FlowEngine(registered)
    artifact_root = tmp_path / "artifacts"
    run_id = "2" * 32
    result = engine.run(
        engine.plan(flow_spec(), "qualification", fake_profile()),
        artifact_root=artifact_root,
        run_id=run_id,
    )
    manifest_path = result.run_root / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["managed_paths"].append("../../outside")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(FlowExecutionError, match="unsafe managed path"):
        engine.clean_run(
            artifact_root=artifact_root,
            owner="example",
            flow_id="fake-pipeline",
            target="qualification",
            run_id=run_id,
        )

    assert result.run_root.is_dir()


def test_public_flow_cli_plans_runs_reads_and_cleans_fake_flow(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    owner_root = tmp_path / "ip/example"
    owner_root.mkdir(parents=True)
    contract = owner_root / "flow.toml"
    contract.write_text(
        '''schema = 1
contract_kind = "flow"
path_scope = "owner"
owner = "example"
name = "fake-pipeline"

[[nodes]]
id = "source"
action = "fake.source"
config = { text = "hello" }

[[nodes]]
id = "transform"
action = "fake.transform"

[[nodes.bindings]]
input = "input"
producer = "source"
output = "source"

[[nodes]]
id = "verify"
action = "fake.verify"
policy = "verify-accepted"
config = { expected = "HELLO" }

[[nodes.bindings]]
input = "candidate"
producer = "transform"
output = "transformed"

[[targets]]
name = "qualification"
goals = ["verify"]

[[policies]]
id = "verify-accepted"

[[policies.checks]]
id = "accepted"
fact = "accepted"
operator = "equals"
expected = true
''',
        encoding="utf-8",
    )
    profile = owner_root / "profile.toml"
    profile.write_text(
        '''schema = 1
contract_kind = "execution-profile"
path_scope = "owner"
owner = "example"
name = "fake"

[actions."fake.source"]
adapter = "fake-source"

[actions."fake.transform"]
adapter = "fake-transform"

[actions."fake.verify"]
adapter = "fake-verify"
''',
        encoding="utf-8",
    )
    catalog = owner_root / "catalog.toml"
    catalog.write_text(
        '''schema = 1
contract_kind = "flow-catalog"
path_scope = "owner"
owner = "example"

[flows.fake-pipeline]
contract = "flow.toml"
default_profile = "fake"

[flows.fake-pipeline.profiles]
fake = "profile.toml"
''',
        encoding="utf-8",
    )
    extension = write_fake_flow_extension(tmp_path, "example")
    write_component_owner(
        tmp_path,
        "example",
        filesets={
            "flow": (
                "ip/example/catalog.toml",
                "ip/example/flow.toml",
                "ip/example/profile.toml",
                extension.relative_to(tmp_path).as_posix(),
            )
        },
    )
    artifact_root = tmp_path / "artifacts"
    run_id = "3" * 32
    source_args = [
        "--project-root",
        str(tmp_path),
        "--owner",
        "example",
        "--flow",
        "fake-pipeline",
        "--target",
        "qualification",
    ]

    assert (
        flow_cli_main(
            [
                "list",
                "--project-root",
                str(tmp_path),
                "--owner",
                "example",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)[0]["flow"] == "fake-pipeline"
    assert (
        flow_cli_main(
            [
                "show",
                "--project-root",
                str(tmp_path),
                "--owner",
                "example",
                "--flow",
                "fake-pipeline",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["execution_profile"] == "fake"

    assert flow_cli_main(["plan", *source_args]) == 0
    plan_payload = json.loads(capsys.readouterr().out)
    assert plan_payload["topology"] == ["source", "transform", "verify"]
    assert not artifact_root.exists()
    assert flow_cli_main(["graph", *source_args]) == 0
    assert '"transform" -> "verify"' in capsys.readouterr().out
    assert flow_cli_main(["preflight", *source_args]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready"
    assert not artifact_root.exists()

    assert (
        flow_cli_main(
            [
                "run",
                *source_args,
                "--run-id",
                run_id,
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "accepted"
    identity = [
        "--project-root",
        str(tmp_path),
        "example",
        "fake-pipeline",
        "qualification",
        run_id,
    ]
    assert flow_cli_main(["status", *identity]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "accepted"
    assert flow_cli_main(["clean", *identity]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "cleaned"


def test_public_flow_cli_reports_missing_owner_catalog_as_contract_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_project_context(tmp_path)
    write_component_owner(tmp_path, "example", filesets={})

    assert (
        flow_cli_main(
            [
                "list",
                "--project-root",
                str(tmp_path),
                "--owner",
                "example",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert "must select exactly one Flow Catalog" in captured.err
    assert "Sigilicon defect" not in captured.err
