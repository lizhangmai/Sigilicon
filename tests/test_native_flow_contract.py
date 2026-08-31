from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from sigilicon.domain.repository import Project
from sigilicon.flow import (
    ActionContext,
    AdapterResult,
    AdapterSelection,
    ArtifactBinding,
    ExecutionProfile,
    EvidenceEnvelope,
    FlowEngine,
    FlowExecutionError,
    FlowNode,
    FlowSpec,
    FlowTarget,
    InputArtifact,
    ResolvedCapability,
)
from sigilicon.flow.native import (
    NATIVE_OA_PLAN_ACTION,
    NATIVE_OA_PLAN_ADAPTER,
    NATIVE_OA_PLAN_KIND,
    NATIVE_OA_SIMULATION_ACTION,
    NATIVE_OA_SIMULATION_ADAPTER,
    XCELIUM_AMS_VERIFICATION_ACTION,
    XCELIUM_AMS_VERIFICATION_ADAPTER,
    XCELIUM_VERIFICATION_ACTION,
    XCELIUM_VERIFICATION_ADAPTER,
)
from sigilicon.workflows import native_flow
from sigilicon.workflows.builtin import build_flow_registry
from sigilicon.workflows.native_flow import (
    NativeOaPlanAdapter,
    NativeOaSimulationAdapter,
    XceliumAmsVerificationAdapter,
    XceliumVerificationAdapter,
)

from conftest import write_component_owner, write_project_context


_EVIDENCE_CONFIG = MappingProxyType(
    {
        "evidence_role": "diagnostic",
        "evidence_level": "l2",
        "evidence_scope": "fixture",
    }
)


def _project(tmp_path: Path) -> Project:
    contract = write_project_context(tmp_path)
    write_component_owner(tmp_path, "native-owner", filesets={})
    return Project.from_file(contract)


def _context(
    project: Project,
    action_kind: str,
    *,
    action_config: MappingProxyType,
    inputs: dict[str, InputArtifact] | None = None,
    capabilities: dict[str, ResolvedCapability] | None = None,
    bound_operations: list[object] | None = None,
) -> ActionContext:
    run_root = project.artifact_root / "fixture-flow-run"
    work_root = run_root / "work" / action_kind
    output_root = run_root / "outputs" / action_kind
    log_root = run_root / "logs" / action_kind
    for root in (work_root, output_root, log_root):
        root.mkdir(parents=True, exist_ok=True)
    operation_id = "a" * 32
    return ActionContext(
        node_id=action_kind,
        action=build_flow_registry().action(action_kind),
        run_root=run_root,
        work_root=work_root,
        output_root=output_root,
        log_root=log_root,
        inputs=MappingProxyType(inputs or {}),
        action_config=action_config,
        adapter_config=MappingProxyType({"timeout_seconds": 17}),
        capabilities=MappingProxyType(capabilities or {}),
        platform_assets=MappingProxyType({}),
        evidence=EvidenceEnvelope.from_action_config(action_config),
        project_scope=project.scope("native-owner"),
        operation_id=operation_id,
        _bind_workspace_operation=(
            bound_operations.append
            if bound_operations is not None
            else lambda _operation: None
        ),
    )


class _PlanningAdapter:
    def run(self, _context: ActionContext) -> AdapterResult:
        return AdapterResult.succeeded()


def test_native_adapters_keep_one_run_operation_and_require_project_binding() -> None:
    registry = build_flow_registry()

    for name, implementation in (
        (NATIVE_OA_PLAN_ADAPTER, NativeOaPlanAdapter),
        (NATIVE_OA_SIMULATION_ADAPTER, NativeOaSimulationAdapter),
        (XCELIUM_VERIFICATION_ADAPTER, XceliumVerificationAdapter),
        (XCELIUM_AMS_VERIFICATION_ADAPTER, XceliumAmsVerificationAdapter),
    ):
        assert not registry.has_adapter(name)
        assert callable(implementation.run)
        assert not hasattr(implementation, "validate_inputs")
        assert not hasattr(implementation, "prepare")
        assert not hasattr(implementation, "execute")
        assert not hasattr(implementation, "collect_result")


def test_native_oa_vertical_slice_plans_as_one_typed_dag() -> None:
    registry = build_flow_registry()
    registry.register_adapter(NATIVE_OA_PLAN_ADAPTER, _PlanningAdapter())
    registry.register_adapter(NATIVE_OA_SIMULATION_ADAPTER, _PlanningAdapter())
    engine = FlowEngine(registry)
    spec = FlowSpec(
        "native-owner",
        "native-oa-l1",
        (
            FlowNode("oa-plan", NATIVE_OA_PLAN_ACTION),
            FlowNode(
                "simulate",
                NATIVE_OA_SIMULATION_ACTION,
                {"testbench": "tb_cell", **_EVIDENCE_CONFIG},
                bindings=(ArtifactBinding("plan", "oa-plan", "plan"),),
                order_after=("oa-plan",),
            ),
        ),
        (FlowTarget("simulation", ("simulate",)),),
    )
    profile = ExecutionProfile(
        "native-owner",
        "cadence",
        (
            AdapterSelection(NATIVE_OA_PLAN_ACTION, NATIVE_OA_PLAN_ADAPTER),
            AdapterSelection(
                NATIVE_OA_SIMULATION_ACTION,
                NATIVE_OA_SIMULATION_ADAPTER,
            ),
        ),
    )

    plan = engine.plan(spec, "simulation", profile)

    assert plan.topology == ("oa-plan", "simulate")
    assert plan.nodes[1].execution_capability == "mutate-workspace"


def test_native_oa_adapters_preserve_plan_and_evidence_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    plan = SimpleNamespace(
        cells=(object(),),
        layouts=(object(),),
        testbenches=(SimpleNamespace(cell="tb_fixture"),),
        as_dict=lambda: {"native_plan": "fixture"},
    )
    bound_operations: list[object] = []

    class Workflow:
        def __init__(self, selected: Project, owner: str) -> None:
            assert selected is project
            assert owner == "native-owner"

        def plan(self):
            return plan

    monkeypatch.setattr(native_flow, "ProjectOaWorkflow", Workflow)

    planned = _context(
        project,
        NATIVE_OA_PLAN_ACTION,
        action_config=MappingProxyType({}),
    )
    plan_result = NativeOaPlanAdapter(project, "native-owner").run(planned)
    assert plan_result.collected is not None
    plan_artifact = plan_result.collected.artifacts[0]
    assert json.loads(plan_artifact.path.read_text(encoding="utf-8")) == plan.as_dict()

    simulation = _context(
        project,
        NATIVE_OA_SIMULATION_ACTION,
        action_config=MappingProxyType(
            {"testbench": "tb_fixture", **_EVIDENCE_CONFIG}
        ),
        inputs={
            "plan": InputArtifact(
                "plan",
                NATIVE_OA_PLAN_KIND,
                plan_artifact.path,
                "oa-plan",
            )
        },
        bound_operations=bound_operations,
    )

    def execute(
        selected_plan,
        step,
        _client,
        *,
        artifacts,
        operation_id,
        bind_operation,
        timeout,
    ):
        assert selected_plan is plan
        assert step.cell == "tb_fixture"
        assert operation_id == "a" * 32
        assert timeout == 17
        operation = SimpleNamespace(operation_id=operation_id)
        bind_operation(operation)
        elaborated = artifacts.write_text(
            "outputs", ("netlist.scs",), "simulator lang=spectre\n"
        )
        rdb_export = artifacts.write_text("outputs", ("rdb.tsv",), "fixture\n")
        normalized = artifacts.write_json("outputs", ("rdb.json",), {"schema": 1})
        summary = artifacts.write_json("outputs", ("summary.json",), {"schema": 1})
        return SimpleNamespace(
            elaborated_netlist=elaborated,
            result_database_export=rdb_export,
            normalized_result_database=normalized,
            run_summary=summary,
            evidence=SimpleNamespace(status="not_evaluated"),
            as_dict=lambda: {"native_maestro_field": "preserved"},
        )

    monkeypatch.setattr(native_flow, "execute_oa_maestro_testbench", execute)
    result = NativeOaSimulationAdapter(
        project,
        "native-owner",
        client_factory=object,
    ).run(simulation)

    assert result.collected is not None
    assert len(bound_operations) == 1
    assert result.collected.facts == {
        "execution-completed": True,
        "native-evidence-status": "not_evaluated",
        "evidence-role": "diagnostic",
        "evidence-level": "l2",
        "evidence-scope": "fixture",
        "product-qualification-conclusion": False,
    }
    payload = json.loads(result.collected.artifacts[0].path.read_text(encoding="utf-8"))
    assert payload["native_maestro_field"] == "preserved"
    assert payload["run_summary"].startswith("artifact://fixture-flow-run/outputs/")
    assert not {"run_id", "run_dir", "manifest"} & payload.keys()
    assert "nested_run_id" not in result.collected.details
    assert payload["product_qualification_conclusion"] is False


def test_native_oa_simulation_fails_closed_on_bound_plan_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    current = SimpleNamespace(as_dict=lambda: {"source": "current"})

    class Workflow:
        def __init__(self, _project: Project, _owner: str) -> None:
            pass

        def plan(self):
            return current

    monkeypatch.setattr(native_flow, "ProjectOaWorkflow", Workflow)
    plan_path = project.artifact_root / "stale-plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text('{"source": "stale"}\n', encoding="utf-8")
    context = _context(
        project,
        NATIVE_OA_SIMULATION_ACTION,
        action_config=MappingProxyType(
            {"testbench": "tb_fixture", **_EVIDENCE_CONFIG}
        ),
        inputs={
            "plan": InputArtifact(
                "plan",
                NATIVE_OA_PLAN_KIND,
                plan_path,
                "oa-plan",
            )
        },
    )

    with pytest.raises(FlowExecutionError, match="plan drifted"):
        NativeOaSimulationAdapter(
            project,
            "native-owner",
            client_factory=lambda: pytest.fail("backend must not run"),
        ).run(context)


def test_xcelium_adapter_preserves_native_payload_and_owner_evidence_role(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    context = _context(
        project,
        XCELIUM_VERIFICATION_ACTION,
        action_config=MappingProxyType(
            {"cell": "ip/native/cell.toml", **_EVIDENCE_CONFIG}
        ),
        capabilities={
            "tool.cadence-xcelium": ResolvedCapability("site.xcelium")
        },
    )
    plan = SimpleNamespace(
        spec=SimpleNamespace(simulator="xcelium"),
        as_dict=lambda: {"native_xcelium_field": "preserved"},
    )

    def plan_cell(path: Path, *, project: Project):
        assert path == Path("ip/native/cell.toml")
        assert project is not None
        return plan

    def execute_cell(selected_plan, *, artifacts, xrun, timeout):
        assert selected_plan is plan
        assert xrun is None
        assert timeout == 17
        summary = artifacts.write_json("outputs", ("summary.json",), {"schema": 1})
        return SimpleNamespace(
            plan=plan,
            returncode=0,
            passed=True,
            run_summary=summary,
        )

    monkeypatch.setattr(native_flow, "plan_xcelium_cell", plan_cell)
    monkeypatch.setattr(native_flow, "execute_xcelium_cell", execute_cell)

    result = XceliumVerificationAdapter(project, "native-owner").run(context)

    assert result.collected is not None
    assert result.collected.facts["evidence-role"] == "diagnostic"
    assert result.collected.facts["evidence-level"] == "l2"
    payload = json.loads(result.collected.artifacts[0].path.read_text(encoding="utf-8"))
    assert payload["native_xcelium_field"] == "preserved"
    assert payload["run_summary"].startswith("artifact://fixture-flow-run/outputs/")
    assert not {"run_id", "run_dir", "manifest"} & payload.keys()
    assert "nested_run_id" not in result.collected.details


def test_xcelium_action_declares_rtl_tool_and_evidence_contract() -> None:
    action = build_flow_registry().action(XCELIUM_VERIFICATION_ACTION)

    assert action.kind == "verification.xcelium-rtl"
    assert action.required_capabilities == ("tool.cadence-xcelium",)
    assert action.facts == (
        "passed",
        "simulator",
        "evidence-role",
        "evidence-level",
        "evidence-scope",
        "product-qualification-conclusion",
    )
    assert action.output("evidence").kind == "evidence.xcelium-rtl-verification"


def test_xcelium_ams_adapter_uses_same_flow_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    context = _context(
        project,
        XCELIUM_AMS_VERIFICATION_ACTION,
        action_config=MappingProxyType(
            {"cell": "ip/native/ams.toml", **_EVIDENCE_CONFIG}
        ),
        capabilities={
            "tool.cadence-xcelium": ResolvedCapability("site.xcelium")
        },
    )
    plan = SimpleNamespace(
        spec=SimpleNamespace(simulator="xcelium-ams"),
        as_dict=lambda: {"native_xcelium_ams_field": "preserved"},
    )

    def plan_cell(path: Path, *, project: Project):
        assert path == Path("ip/native/ams.toml")
        assert project is not None
        return plan

    def execute_cell(selected_plan, *, artifacts, xrun, timeout):
        assert selected_plan is plan
        assert xrun is None
        assert timeout == 17
        summary = artifacts.write_json("outputs", ("summary.json",), {"schema": 1})
        return SimpleNamespace(
            plan=plan,
            returncode=0,
            passed=True,
            run_summary=summary,
        )

    monkeypatch.setattr(native_flow, "plan_xcelium_ams_cell", plan_cell)
    monkeypatch.setattr(native_flow, "execute_xcelium_ams_cell", execute_cell)

    result = XceliumAmsVerificationAdapter(project, "native-owner").run(context)

    assert result.collected is not None
    assert result.collected.facts["simulator"] == "xcelium-ams"
    payload = json.loads(result.collected.artifacts[0].path.read_text(encoding="utf-8"))
    assert payload["native_xcelium_ams_field"] == "preserved"
    assert payload["run_summary"].startswith("artifact://fixture-flow-run/outputs/")
    assert not {"run_id", "run_dir", "manifest"} & payload.keys()


def test_xcelium_ams_action_declares_mixed_signal_contract() -> None:
    action = build_flow_registry().action(XCELIUM_AMS_VERIFICATION_ACTION)

    assert action.kind == "verification.xcelium-ams"
    assert action.required_capabilities == ("tool.cadence-xcelium",)
    assert action.output("evidence").kind == "evidence.xcelium-ams-verification"
