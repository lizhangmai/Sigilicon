from __future__ import annotations

import json
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
    version = "1"

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
    version = "1"

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
        )


class VerifyAdapter:
    version = "1"

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
) -> FlowSpec:
    return FlowSpec(
        owner="example",
        flow_id="fake-pipeline",
        nodes=(
            FlowNode(
                node_id="source",
                action_kind="fake.source",
                config={"text": text, "qualifiers": qualifiers or {}},
            ),
            FlowNode(
                node_id="transform",
                action_kind="fake.transform",
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


def test_fake_vertical_slice_writes_stable_records(tmp_path: Path) -> None:
    registered, source, transform, verify = registry()
    engine = FlowEngine(registered)
    plan = engine.plan(flow_spec(), "qualification", fake_profile())

    assert plan.topology == ("source", "transform", "verify")
    result = engine.run(
        plan,
        artifact_root=tmp_path / "artifacts",
        run_id="a" * 32,
    )

    assert result.status == "accepted"
    assert [source.executions, transform.executions, verify.executions] == [1, 1, 1]
    assert result.nodes["verify"].policy_status == "accepted"
    for record in (
        "resolved_plan.json",
        "flow_result.json",
        "run_manifest.json",
    ):
        assert (result.run_root / record).is_file()
    for node in plan.topology:
        node_root = result.run_root / "nodes" / node
        assert (node_root / "action_request.json").is_file()
        assert (node_root / "action_result.json").is_file()
        assert (node_root / "policy_receipt.json").is_file()
        assert (node_root / "run_manifest.json").is_file()

    encoded_root = str(result.run_root).encode()
    for record in result.run_root.rglob("*.json"):
        assert encoded_root not in record.read_bytes()


def test_artifact_qualifiers_propagate_into_records_and_fingerprints(
    tmp_path: Path,
) -> None:
    registered, *_ = registry()
    engine = FlowEngine(registered)
    artifact_root = tmp_path / "artifacts"
    run_id = "9" * 32
    paper_plan = engine.plan(
        flow_spec(qualifiers={"variant": "paper_0p8v", "corner": "tt0p8v25c"}),
        "qualification",
        fake_profile(),
    )
    paper = engine.run(paper_plan, artifact_root=artifact_root, run_id=run_id)

    source_result = json.loads(
        (paper.run_root / "nodes/source/action_result.json").read_text()
    )
    transform_request = json.loads(
        (paper.run_root / "nodes/transform/action_request.json").read_text()
    )
    assert source_result["artifacts"]["source"]["qualifiers"] == {
        "corner": "tt0p8v25c",
        "variant": "paper_0p8v",
    }
    assert transform_request["inputs"]["input"]["qualifiers"] == {
        "corner": "tt0p8v25c",
        "variant": "paper_0p8v",
    }

    product_plan = engine.plan(
        flow_spec(qualifiers={"variant": "product_0p9v", "corner": "tt0p9v25c"}),
        "qualification",
        fake_profile(),
    )
    product = engine.run(
        product_plan,
        artifact_root=artifact_root,
        run_id=run_id,
        resume=True,
    )

    assert not any(outcome.reused for outcome in product.nodes.values())
    assert (
        paper.nodes["source"].execution_fingerprint
        != product.nodes["source"].execution_fingerprint
    )


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


def test_resume_reuses_exact_results_and_propagates_changed_inputs(tmp_path: Path) -> None:
    registered, source, transform, verify = registry()
    engine = FlowEngine(registered)
    artifact_root = tmp_path / "artifacts"
    run_id = "d" * 32

    first = engine.run(
        engine.plan(flow_spec(text="hello"), "qualification", fake_profile()),
        artifact_root=artifact_root,
        run_id=run_id,
    )
    resumed = engine.run(
        engine.plan(flow_spec(text="hello"), "qualification", fake_profile()),
        artifact_root=artifact_root,
        run_id=run_id,
        resume=True,
    )

    assert first.status == resumed.status == "accepted"
    assert [source.executions, transform.executions, verify.executions] == [1, 1, 1]
    assert all(node.reused for node in resumed.nodes.values())

    changed = engine.run(
        engine.plan(flow_spec(text="goodbye"), "qualification", fake_profile()),
        artifact_root=artifact_root,
        run_id=run_id,
        resume=True,
    )
    assert changed.status == "accepted"
    assert [source.executions, transform.executions, verify.executions] == [2, 2, 2]
    assert not any(node.reused for node in changed.nodes.values())


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
    version = "1"

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
        (result.run_root / "nodes/terminal/action_result.json").read_text()
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
        (result.run_root / "nodes/interrupt/action_result.json").read_text()
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
            run_id=run_id,
        )

    assert untracked.read_text(encoding="utf-8") == "owned by caller"
    untracked.unlink()
    engine.clean_run(
        artifact_root=artifact_root,
        owner="example",
        flow_id="fake-pipeline",
        run_id=run_id,
    )
    assert not result.run_root.exists()


def test_clean_accepts_a_manifest_rewritten_by_exact_resume(tmp_path: Path) -> None:
    registered, *_ = registry()
    engine = FlowEngine(registered)
    artifact_root = tmp_path / "artifacts"
    run_id = "4" * 32
    plan = engine.plan(flow_spec(), "qualification", fake_profile())
    engine.run(plan, artifact_root=artifact_root, run_id=run_id)
    result = engine.run(
        plan,
        artifact_root=artifact_root,
        run_id=run_id,
        resume=True,
    )

    engine.clean_run(
        artifact_root=artifact_root,
        owner="example",
        flow_id="fake-pipeline",
        run_id=run_id,
    )

    assert not result.run_root.exists()


def test_stale_resume_refuses_to_delete_untracked_node_content(tmp_path: Path) -> None:
    registered, *_ = registry()
    engine = FlowEngine(registered)
    artifact_root = tmp_path / "artifacts"
    run_id = "5" * 32
    first = engine.run(
        engine.plan(flow_spec(text="hello"), "qualification", fake_profile()),
        artifact_root=artifact_root,
        run_id=run_id,
    )
    untracked = first.run_root / "nodes/source/caller-owned.txt"
    untracked.write_text("preserve", encoding="utf-8")

    with pytest.raises(FlowExecutionError, match="untracked"):
        engine.run(
            engine.plan(
                flow_spec(text="changed"),
                "qualification",
                fake_profile(),
            ),
            artifact_root=artifact_root,
            run_id=run_id,
            resume=True,
        )

    assert untracked.read_text(encoding="utf-8") == "preserve"


def test_policy_change_reevaluates_facts_without_rerunning_actions(
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
    resumed = engine.run(
        engine.plan(changed_policy, "qualification", fake_profile()),
        artifact_root=artifact_root,
        run_id=run_id,
        resume=True,
    )

    assert first.status == "accepted"
    assert resumed.status == "failed"
    assert resumed.nodes["verify"].status == "rejected"
    assert [source.executions, transform.executions, verify.executions] == [1, 1, 1]
    assert all(outcome.reused for outcome in resumed.nodes.values())


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
        run_id,
    ]
    assert flow_cli_main(["status", *identity]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "accepted"
    assert flow_cli_main(["clean", *identity]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "cleaned"
