from __future__ import annotations

import json
import inspect
from pathlib import Path

import pytest

from sigilicon.cli.flow_core import main as flow_cli_main
from sigilicon.flow import (
    ActionContext,
    ActionContract,
    AdapterExecution,
    AdapterSelection,
    ArtifactBinding,
    ArtifactPort,
    CollectedActionResult,
    ExecutionProfile,
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


class SourceAdapter:
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


class TransformAdapter:
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


class VerifyAdapter:
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
    assert registered.adapter("fixture-owner-qualification") is adapter

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
    for node in plan.topology:
        assert (result.run_root / "inputs" / node / "action_request.json").is_file()
        assert (result.run_root / "outputs" / node / "action_result.json").is_file()
        assert (result.run_root / "outputs" / node / "policy_receipt.json").is_file()

    encoded_root = str(result.run_root).encode()
    for record in result.run_root.rglob("*.json"):
        assert encoded_root not in record.read_bytes()

    restored = engine.restore_result(
        plan,
        artifact_root=tmp_path / "artifacts",
        run_id="a" * 32,
    )
    assert restored == result


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
    contract = tmp_path / "flow.toml"
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


class TerminalAdapter:
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
    contract = tmp_path / "flow.toml"
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
    profile = tmp_path / "profile.toml"
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
    catalog = tmp_path / "catalog.toml"
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
    artifact_root = tmp_path / "artifacts"
    run_id = "3" * 32
    source_args = [
        str(catalog),
        "fake-pipeline",
        "qualification",
        "--owner-root",
        str(tmp_path),
    ]

    assert (
        flow_cli_main(
            ["list", str(catalog), "--owner-root", str(tmp_path)]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)[0]["flow"] == "fake-pipeline"
    assert (
        flow_cli_main(
            [
                "show",
                str(catalog),
                "fake-pipeline",
                "--owner-root",
                str(tmp_path),
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
                "--artifact-root",
                str(artifact_root),
                "--run-id",
                run_id,
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "accepted"
    identity = [
        "--artifact-root",
        str(artifact_root),
        "example",
        "fake-pipeline",
        "qualification",
        run_id,
    ]
    assert flow_cli_main(["status", *identity]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "accepted"
    assert flow_cli_main(["clean", *identity]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "cleaned"
