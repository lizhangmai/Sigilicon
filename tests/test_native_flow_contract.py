from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from sigilicon.domain.repository import Project
from sigilicon.flow import (
    ActionBinding,
    ActionContext,
    ActionPlan,
    AdapterResult,
    ArtifactBinding,
    EvidenceEnvelope,
    FlowEngine,
    FlowExecutionError,
    FlowNode,
    FlowSpec,
    FlowTarget,
    InputArtifact,
    ResolvedCapability,
    SourceMember,
)
from sigilicon.flow.native import (
    NATIVE_OA_ACTION_PLAN,
    NATIVE_OA_PLAN_ACTION,
    NATIVE_OA_PLAN_ADAPTER,
    NATIVE_OA_PLAN_KIND,
    NATIVE_OA_SIMULATION_ACTION,
    NATIVE_OA_SIMULATION_ADAPTER,
    XCELIUM_AMS_VERIFICATION_ACTION,
    XCELIUM_AMS_VERIFICATION_ADAPTER,
    XCELIUM_AMS_ACTION_PLAN,
    XCELIUM_ACTION_PLAN,
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


def _source_member(path: Path, *, root: Path) -> SourceMember:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("source = true\n", encoding="utf-8")
    return SourceMember(
        path.relative_to(root).as_posix(),
        root,
        "source = true\n",
        False,
        path,
    )


def _context(
    project: Project,
    action_kind: str,
    *,
    action_config: MappingProxyType,
    inputs: dict[str, InputArtifact] | None = None,
    capabilities: dict[str, ResolvedCapability] | None = None,
    bound_operations: list[object] | None = None,
    action_plan: ActionPlan | None = None,
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
        action_plan=action_plan,
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
    assert not vars(NativeOaPlanAdapter())
    assert not vars(XceliumVerificationAdapter())
    assert not vars(XceliumAmsVerificationAdapter())


def test_native_oa_vertical_slice_plans_as_one_typed_dag(tmp_path: Path) -> None:
    registry = build_flow_registry()
    registry.register_adapter(NATIVE_OA_PLAN_ADAPTER, _PlanningAdapter())
    registry.register_adapter(NATIVE_OA_SIMULATION_ADAPTER, _PlanningAdapter())
    engine = FlowEngine(registry)
    spec = FlowSpec(
        owner="native-owner",
        flow_id="native-oa-l1",
        recipe_id="native-oa-l1-recipe",
        nodes=(
            FlowNode("oa-plan", NATIVE_OA_PLAN_ACTION),
            FlowNode(
                "simulate",
                NATIVE_OA_SIMULATION_ACTION,
                {"testbench": "tb_cell", **_EVIDENCE_CONFIG},
                bindings=(ArtifactBinding("plan", "oa-plan", "plan"),),
                order_after=("oa-plan",),
            ),
        ),
        targets=(FlowTarget("simulation", ("simulate",)),),
        action_bindings=(
            ActionBinding(NATIVE_OA_PLAN_ACTION, NATIVE_OA_PLAN_ADAPTER),
            ActionBinding(
                NATIVE_OA_SIMULATION_ACTION,
                NATIVE_OA_SIMULATION_ADAPTER,
            ),
        ),
    )

    typed = ActionPlan(
        NATIVE_OA_ACTION_PLAN,
        object(),
        {},
        (_source_member(tmp_path / "oa.toml", root=tmp_path),),
    )
    plan = engine.plan(
        spec,
        "simulation",
        action_plans={"oa-plan": typed, "simulate": typed},
    )

    assert plan.topology == ("oa-plan", "simulate")
    assert plan.nodes[1].execution_capability == "mutate-workspace"


def test_native_oa_adapters_preserve_plan_and_evidence_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    plan = SimpleNamespace(
        source=SimpleNamespace(
            project=project,
            manifest_path=project.owner("native-owner").root / "oa.toml",
        ),
        cells=(object(),),
        layouts=(object(),),
        testbenches=(SimpleNamespace(cell="tb_fixture"),),
        as_dict=lambda: {"native_plan": "fixture"},
    )
    bound_operations: list[object] = []

    monkeypatch.setattr(native_flow, "OALibraryRebuildPlan", SimpleNamespace)
    monkeypatch.setattr(
        native_flow,
        "validate_oa_plan_source_members",
        lambda _plan, _members: None,
    )
    source_member = _source_member(
        plan.source.manifest_path,
        root=project.project_root,
    )
    typed = ActionPlan(
        NATIVE_OA_ACTION_PLAN,
        plan,
        plan.as_dict(),
        (source_member,),
    )

    planned = _context(
        project,
        NATIVE_OA_PLAN_ACTION,
        action_config=MappingProxyType({}),
        action_plan=typed,
    )
    plan_result = NativeOaPlanAdapter().run(planned)
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
        action_plan=typed,
    )

    def execute(
        selected_plan,
        step,
        _client,
        *,
        artifacts,
        operation_id,
        bind_operation,
        before_backend,
        timeout,
    ):
        assert selected_plan is plan
        assert step.cell == "tb_fixture"
        assert operation_id == "a" * 32
        assert timeout == 17
        before_backend()
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
        client_factory=object,
    ).run(simulation)

    assert result.collected is not None
    assert len(bound_operations) == 1
    assert result.collected.facts.as_mapping() == {
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
    assert not hasattr(result.collected, "details")
    assert payload["product_qualification_conclusion"] is False


def test_native_oa_simulation_fails_closed_on_bound_plan_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    current = SimpleNamespace(
        source=SimpleNamespace(
            project=project,
            manifest_path=project.owner("native-owner").root / "oa.toml",
        ),
        as_dict=lambda: {"source": "current"},
    )
    monkeypatch.setattr(native_flow, "OALibraryRebuildPlan", SimpleNamespace)
    monkeypatch.setattr(
        native_flow,
        "validate_oa_plan_source_members",
        lambda _plan, _members: None,
    )
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
        action_plan=ActionPlan(
            NATIVE_OA_ACTION_PLAN,
            current,
            current.as_dict(),
            (
                _source_member(
                    project.owner("native-owner").root / "oa.toml",
                    root=project.project_root,
                ),
            ),
        ),
    )

    with pytest.raises(FlowExecutionError, match="plan drifted"):
        NativeOaSimulationAdapter(
            client_factory=lambda: pytest.fail("backend must not run"),
        ).run(context)


def test_native_oa_simulation_rechecks_sources_before_backend_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    manifest = project.owner("native-owner").root / "oa.toml"
    source_member = _source_member(manifest, root=project.project_root)
    plan = SimpleNamespace(
        source=SimpleNamespace(project=project, manifest_path=manifest),
        testbenches=(SimpleNamespace(cell="tb_fixture"),),
        as_dict=lambda: {"source": "current"},
    )
    plan_path = project.artifact_root / "bound-plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan.as_dict()) + "\n", encoding="utf-8")
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
        action_plan=ActionPlan(
            NATIVE_OA_ACTION_PLAN,
            plan,
            plan.as_dict(),
            (source_member,),
        ),
    )
    monkeypatch.setattr(native_flow, "OALibraryRebuildPlan", SimpleNamespace)
    monkeypatch.setattr(
        native_flow,
        "validate_oa_plan_source_members",
        lambda _plan, _members: None,
    )
    backend_started = False

    def execute(_plan, _step, _client, *, before_backend, **_kwargs):
        nonlocal backend_started
        before_backend()
        backend_started = True
        raise AssertionError("source drift must block OA backend access")

    monkeypatch.setattr(native_flow, "execute_oa_maestro_testbench", execute)
    manifest.write_text("source = false\n", encoding="utf-8")

    with pytest.raises(FlowExecutionError, match="source changed after preflight"):
        NativeOaSimulationAdapter(client_factory=object).run(context)
    assert not backend_started


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
        contract=project.project_root / "ip/native-owner/cell.toml",
        source_records={
            project.project_root / "ip/native-owner/cell.toml": "source = true\n"
        },
        spec=SimpleNamespace(
            simulator="xcelium",
            project=project,
            owner="native-owner",
        ),
        as_dict=lambda: {"native_xcelium_field": "preserved"},
    )
    context = _context(
        project,
        XCELIUM_VERIFICATION_ACTION,
        action_config=MappingProxyType(
            {"cell": "ip/native-owner/cell.toml", **_EVIDENCE_CONFIG}
        ),
        capabilities={
            "tool.cadence-xcelium": ResolvedCapability("site.xcelium")
        },
        action_plan=ActionPlan(
            XCELIUM_ACTION_PLAN,
            plan,
            plan.as_dict(),
            (
                _source_member(
                    plan.contract,
                    root=project.project_root,
                ),
            ),
        ),
    )

    def execute_cell(selected_plan, *, artifacts, xrun, before_spawn, timeout):
        assert selected_plan is plan
        assert xrun is None
        assert timeout == 17
        before_spawn()
        summary = artifacts.write_json("outputs", ("summary.json",), {"schema": 1})
        return SimpleNamespace(
            plan=plan,
            returncode=0,
            passed=True,
            run_summary=summary,
        )

    monkeypatch.setattr(native_flow, "XceliumCellPlan", SimpleNamespace)
    monkeypatch.setattr(native_flow, "execute_xcelium_cell", execute_cell)

    result = XceliumVerificationAdapter().run(context)

    assert result.collected is not None
    assert result.collected.facts["evidence-role"] == "diagnostic"
    assert result.collected.facts["evidence-level"] == "l2"
    payload = json.loads(result.collected.artifacts[0].path.read_text(encoding="utf-8"))
    assert payload["native_xcelium_field"] == "preserved"
    assert payload["run_summary"].startswith("artifact://fixture-flow-run/outputs/")
    assert not {"run_id", "run_dir", "manifest"} & payload.keys()
    assert not hasattr(result.collected, "details")


@pytest.mark.parametrize(
    ("action_kind", "plan_kind", "contract_name", "plan_type", "adapter_type"),
    (
        (
            XCELIUM_VERIFICATION_ACTION,
            XCELIUM_ACTION_PLAN,
            "rtl.toml",
            "XceliumCellPlan",
            XceliumVerificationAdapter,
        ),
        (
            XCELIUM_AMS_VERIFICATION_ACTION,
            XCELIUM_AMS_ACTION_PLAN,
            "ams.toml",
            "XceliumAmsCellPlan",
            XceliumAmsVerificationAdapter,
        ),
    ),
)
def test_xcelium_adapters_reject_cell_configuration_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action_kind: str,
    plan_kind: str,
    contract_name: str,
    plan_type: str,
    adapter_type: type,
) -> None:
    project = _project(tmp_path)
    contract = project.project_root / "ip/native-owner" / contract_name
    plan = SimpleNamespace(
        contract=contract,
        source_records={contract: "source = true\n"},
        spec=SimpleNamespace(project=project, owner="native-owner"),
        as_dict=lambda: {"cell": contract_name},
    )
    monkeypatch.setattr(native_flow, plan_type, SimpleNamespace)
    context = _context(
        project,
        action_kind,
        action_config=MappingProxyType(
            {"cell": "ip/native-owner/other.toml", **_EVIDENCE_CONFIG}
        ),
        capabilities={
            "tool.cadence-xcelium": ResolvedCapability("site.xcelium")
        },
        action_plan=ActionPlan(
            plan_kind,
            plan,
            plan.as_dict(),
            (_source_member(contract, root=project.project_root),),
        ),
    )

    with pytest.raises(FlowExecutionError, match="cell drifted"):
        adapter_type().run(context)


@pytest.mark.parametrize(
    ("action_kind", "plan_kind", "contract_name", "plan_type", "adapter_type", "execute_name"),
    (
        (
            XCELIUM_VERIFICATION_ACTION,
            XCELIUM_ACTION_PLAN,
            "rtl.toml",
            "XceliumCellPlan",
            XceliumVerificationAdapter,
            "execute_xcelium_cell",
        ),
        (
            XCELIUM_AMS_VERIFICATION_ACTION,
            XCELIUM_AMS_ACTION_PLAN,
            "ams.toml",
            "XceliumAmsCellPlan",
            XceliumAmsVerificationAdapter,
            "execute_xcelium_ams_cell",
        ),
    ),
)
def test_xcelium_adapters_recheck_sources_at_spawn_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action_kind: str,
    plan_kind: str,
    contract_name: str,
    plan_type: str,
    adapter_type: type,
    execute_name: str,
) -> None:
    project = _project(tmp_path)
    relative_contract = f"ip/native-owner/{contract_name}"
    contract = project.project_root / relative_contract
    source_member = _source_member(contract, root=project.project_root)
    plan = SimpleNamespace(
        contract=contract,
        source_records={contract: "source = true\n"},
        spec=SimpleNamespace(project=project, owner="native-owner"),
        as_dict=lambda: {"cell": contract_name},
    )
    context = _context(
        project,
        action_kind,
        action_config=MappingProxyType(
            {"cell": relative_contract, **_EVIDENCE_CONFIG}
        ),
        capabilities={
            "tool.cadence-xcelium": ResolvedCapability("site.xcelium")
        },
        action_plan=ActionPlan(
            plan_kind,
            plan,
            plan.as_dict(),
            (source_member,),
        ),
    )
    monkeypatch.setattr(native_flow, plan_type, SimpleNamespace)
    spawned = False

    def execute_cell(_plan, *, before_spawn, **_kwargs):
        nonlocal spawned
        before_spawn()
        spawned = True
        raise AssertionError("source drift must block the backend")

    monkeypatch.setattr(native_flow, execute_name, execute_cell)
    contract.write_text("source = false\n", encoding="utf-8")

    with pytest.raises(FlowExecutionError, match="source changed after preflight"):
        adapter_type().run(context)
    assert not spawned


def test_xcelium_action_declares_rtl_tool_and_evidence_contract() -> None:
    action = build_flow_registry().action(XCELIUM_VERIFICATION_ACTION)

    assert action.kind == "verification.xcelium-rtl"
    assert action.required_capabilities == ("tool.cadence-xcelium",)
    assert action.fact_schema is not None
    assert tuple(field.name for field in action.fact_schema.fields) == (
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
        contract=project.project_root / "ip/native-owner/ams.toml",
        source_records={
            project.project_root / "ip/native-owner/ams.toml": "source = true\n"
        },
        spec=SimpleNamespace(
            simulator="xcelium-ams",
            project=project,
            owner="native-owner",
        ),
        as_dict=lambda: {"native_xcelium_ams_field": "preserved"},
    )
    context = _context(
        project,
        XCELIUM_AMS_VERIFICATION_ACTION,
        action_config=MappingProxyType(
            {"cell": "ip/native-owner/ams.toml", **_EVIDENCE_CONFIG}
        ),
        capabilities={
            "tool.cadence-xcelium": ResolvedCapability("site.xcelium")
        },
        action_plan=ActionPlan(
            XCELIUM_AMS_ACTION_PLAN,
            plan,
            plan.as_dict(),
            (
                _source_member(
                    plan.contract,
                    root=project.project_root,
                ),
            ),
        ),
    )

    def execute_cell(selected_plan, *, artifacts, xrun, before_spawn, timeout):
        assert selected_plan is plan
        assert xrun is None
        assert timeout == 17
        before_spawn()
        summary = artifacts.write_json("outputs", ("summary.json",), {"schema": 1})
        return SimpleNamespace(
            plan=plan,
            returncode=0,
            passed=True,
            run_summary=summary,
        )

    monkeypatch.setattr(native_flow, "XceliumAmsCellPlan", SimpleNamespace)
    monkeypatch.setattr(native_flow, "execute_xcelium_ams_cell", execute_cell)

    result = XceliumAmsVerificationAdapter().run(context)

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
