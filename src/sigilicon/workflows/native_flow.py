"""Stateless Adapters for explicitly planned native-OA and Xcelium Actions."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import json
from pathlib import Path
from typing import Any

from sigilicon.domain.repository import Project, RepositoryOwner
from sigilicon.flow import (
    ActionPlan,
    ActionContext,
    AdapterResult,
    CollectedActionResult,
    FlowExecutionError,
    ProducedArtifact,
    FlowNode,
    SourceMember,
)
from sigilicon.flow.evidence import FactSet, FactSource
from sigilicon.flow.native import (
    NATIVE_OA_ACTION_PLAN,
    NATIVE_OA_EVIDENCE_KIND,
    NATIVE_OA_PLAN_KIND,
    NATIVE_OA_PLAN_ADAPTER,
    NATIVE_OA_PLAN_ACTION,
    NATIVE_OA_SIMULATION_ADAPTER,
    NATIVE_OA_SIMULATION_ACTION,
    XCELIUM_ACTION_PLAN,
    XCELIUM_AMS_ACTION_PLAN,
    XCELIUM_AMS_VERIFICATION_ACTION,
    XCELIUM_AMS_VERIFICATION_ADAPTER,
    XCELIUM_EVIDENCE_KIND,
    XCELIUM_AMS_EVIDENCE_KIND,
    XCELIUM_VERIFICATION_ACTION,
    XCELIUM_VERIFICATION_ADAPTER,
)
from sigilicon.flow.registry import FlowRegistry
from sigilicon.flow.serialization import json_value
from sigilicon.flow.source_assets import source_member_matches
from sigilicon.virtuoso.client import get_client
from sigilicon.workflows.oa_library import (
    OALibraryRebuildPlan,
    oa_plan_source_paths,
    validate_oa_plan_source_members,
)
from sigilicon.workflows.oa_simulation import execute_oa_maestro_testbench
from sigilicon.workflows.run_artifacts import RunArtifacts
from sigilicon.workflows.source_control import artifact_source_state
from sigilicon.workflows.project_oa import ProjectOaWorkflow
from sigilicon.workflows.source_closure import project_source_members
from sigilicon.workflows.xcelium import (
    XceliumCellPlan,
    execute_xcelium_cell,
    plan_xcelium_cell,
)
from sigilicon.workflows.xcelium_ams import (
    XceliumAmsCellPlan,
    execute_xcelium_ams_cell,
    plan_xcelium_ams_cell,
)


def _require_planned_project(
    context: ActionContext,
    project: Project,
    owner_name: str,
) -> Project:
    scope = context.require_project_scope()
    owner = project.owner(owner_name)
    if (
        scope.owner != owner.name
        or scope.owner_root != owner.root
        or scope.project.project_root != project.project_root
        or scope.project.artifact_root != project.artifact_root
    ):
        raise FlowExecutionError("Action Plan project owner scope drift")
    return project


def _require_record(context: ActionContext, record: dict[str, object]) -> None:
    assert context.action_plan is not None
    if json_value(context.action_plan.record) != record:
        raise FlowExecutionError("typed Action Plan record drift")


def _require_action_plan_sources(context: ActionContext) -> None:
    """Recheck the exact source bytes immediately before backend access."""

    assert context.action_plan is not None
    if not context.action_plan.sources:
        raise FlowExecutionError("typed Action Plan has no explicit sources")
    try:
        exact = all(
            source_member_matches(member)
            for member in context.action_plan.sources
        )
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        raise FlowExecutionError(
            f"typed Action Plan source changed after preflight: {exc}"
        ) from exc
    if not exact:
        raise FlowExecutionError("typed Action Plan source changed after preflight")


def _require_oa_plan_sources(
    context: ActionContext,
    plan: OALibraryRebuildPlan,
) -> None:
    assert context.action_plan is not None
    try:
        validate_oa_plan_source_members(plan, context.action_plan.sources)
    except ValueError as exc:
        raise FlowExecutionError(str(exc)) from exc
    _require_action_plan_sources(context)


def _require_typed_source_records(
    context: ActionContext,
    records: Mapping[Path, str],
    *,
    label: str,
) -> None:
    assert context.action_plan is not None
    provided = {
        member.location: member.record_text
        for member in context.action_plan.sources
    }
    if not records or any(
        provided.get(Path(path).resolve()) != record
        for path, record in records.items()
    ):
        raise FlowExecutionError(f"typed {label} plan source closure drift")
    _require_action_plan_sources(context)


def _require_cell_config(
    context: ActionContext,
    *,
    contract: Path,
    project: Project,
) -> None:
    configured = _text_config(context, "cell")
    relative = Path(configured)
    root = project.project_root
    if relative.is_absolute():
        raise FlowExecutionError("Xcelium Action cell must be project-relative")
    selected = (root / relative).resolve()
    if not selected.is_relative_to(root) or selected != contract.resolve():
        raise FlowExecutionError("Xcelium Action cell drifted from typed plan")


def _artifact_reference(path: Path, project: Project) -> str:
    resolved = path.resolve()
    for label, root in (
        ("artifact", project.artifact_root),
        ("project", project.project_root),
    ):
        if resolved.is_relative_to(root):
            return f"{label}://{resolved.relative_to(root).as_posix()}"
    raise FlowExecutionError("nested workflow artifact escaped project roots")


def _text_config(context: ActionContext, name: str) -> str:
    value = context.action_config.get(name)
    if not isinstance(value, str) or not value:
        raise FlowExecutionError(f"Action config {name!r} must be non-empty text")
    return value


def _evidence_metadata(context: ActionContext) -> dict[str, str]:
    evidence = context.require_evidence()
    return {
        "evidence_role": evidence.role,
        "evidence_level": evidence.level,
        "evidence_scope": evidence.scope,
    }


def _fact_set(
    context: ActionContext,
    values: Mapping[str, object],
) -> FactSet:
    """Project one typed observation through the Action's declared schema."""

    schema = context.action.fact_schema
    if schema is None:
        raise FlowExecutionError(f"Action {context.node_id!r} has no fact schema")
    return FactSet(
        schema,
        values,
        FactSource(context.action.kind, context.node_id),
    )


def _planned_product_conclusion(context: ActionContext) -> bool:
    """Keep product authority in the planned envelope, never backend JSON."""

    context.require_evidence()
    # EvidenceEnvelope currently carries classification, not qualification
    # authority.  Until a plan explicitly grants that authority, the safe
    # projection is false for every native action.
    return False


class NativeOaPlanAdapter:
    def run(self, context: ActionContext) -> AdapterResult:
        plan = context.require_action_plan(
            NATIVE_OA_ACTION_PLAN,
            OALibraryRebuildPlan,
        )
        _require_record(context, plan.as_dict())
        _require_oa_plan_sources(context, plan)
        project = plan.source.project
        owner = project.require_owner(plan.source.manifest_path).name
        _require_planned_project(context, project, owner)
        output = context.output_path("plan", "oa-assembly-plan.json")
        output.write_text(
            json.dumps(plan.as_dict(), indent=2) + "\n",
            encoding="utf-8",
        )
        return AdapterResult.succeeded(
            CollectedActionResult(
                artifacts=(
                    ProducedArtifact("plan", NATIVE_OA_PLAN_KIND, output),
                ),
                facts=_fact_set(context, {
                    "source-plan-valid": True,
                    "source-cell-count": len(plan.cells),
                    "source-layout-count": len(plan.layouts),
                    "source-testbench-count": len(plan.testbenches),
                }),
            )
        )


class NativeOaSimulationAdapter:
    def __init__(
        self,
        *,
        client_factory: Callable[[], Any] = get_client,
    ) -> None:
        self._client_factory = client_factory

    def run(self, context: ActionContext) -> AdapterResult:
        plan = context.require_action_plan(
            NATIVE_OA_ACTION_PLAN,
            OALibraryRebuildPlan,
        )
        _require_record(context, plan.as_dict())
        project = plan.source.project
        owner = project.require_owner(plan.source.manifest_path).name
        _require_planned_project(context, project, owner)
        try:
            bound_plan = json.loads(
                context.input("plan").path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise FlowExecutionError(
                f"cannot read bound OA assembly plan: {exc}"
            ) from exc
        if bound_plan != plan.as_dict():
            raise FlowExecutionError("bound OA assembly plan drifted from owner source")
        metadata = _evidence_metadata(context)
        testbench = _text_config(context, "testbench")
        matches = tuple(step for step in plan.testbenches if step.cell == testbench)
        if len(matches) != 1:
            raise FlowExecutionError(
                f"unknown OA testbench in assembly: {testbench}"
            )
        if context.operation_id is None:
            raise FlowExecutionError("native OA simulation has no managed operation")
        result = execute_oa_maestro_testbench(
            plan,
            matches[0],
            self._client_factory(),
            artifacts=RunArtifacts.from_action_context(
                context,
                "evidence",
                artifact_source_state(project.project_root),
            ),
            operation_id=context.operation_id,
            bind_operation=context.bind_workspace_operation,
            timeout=int(context.adapter_config.get("timeout_seconds", 600)),
            before_backend=lambda: _require_oa_plan_sources(context, plan),
        )
        payload = result.as_dict()
        payload.update(metadata)
        product_conclusion = _planned_product_conclusion(context)
        payload["product_qualification_conclusion"] = product_conclusion
        for field, path in (
            ("elaborated_netlist", result.elaborated_netlist),
            ("result_database_export", result.result_database_export),
            ("normalized_result_database", result.normalized_result_database),
            ("run_summary", result.run_summary),
        ):
            payload[field] = _artifact_reference(path, project)
        output = context.output_path("evidence", "maestro-evidence.json")
        output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return AdapterResult.succeeded(
            CollectedActionResult(
                artifacts=(
                    ProducedArtifact(
                        "evidence",
                        NATIVE_OA_EVIDENCE_KIND,
                        output,
                    ),
                ),
                facts=_fact_set(context, {
                    "native-evidence-status": result.evidence.status,
                    "evidence-role": metadata["evidence_role"],
                    "evidence-level": metadata["evidence_level"],
                    "evidence-scope": metadata["evidence_scope"],
                    "product-qualification-conclusion": product_conclusion,
                }),
            )
        )


class XceliumVerificationAdapter:
    def run(self, context: ActionContext) -> AdapterResult:
        plan = context.require_action_plan(
            XCELIUM_ACTION_PLAN,
            XceliumCellPlan,
        )
        _require_record(context, plan.as_dict())
        project = plan.spec.project
        _require_planned_project(context, project, plan.spec.owner)
        _require_cell_config(
            context,
            contract=plan.contract,
            project=project,
        )
        capability = context.capabilities["tool.cadence-xcelium"]
        metadata = _evidence_metadata(context)
        product_conclusion = _planned_product_conclusion(context)
        result = execute_xcelium_cell(
            plan,
            artifacts=RunArtifacts.from_action_context(
                context,
                "evidence",
                artifact_source_state(project.project_root),
            ),
            xrun=capability.executable,
            timeout=int(context.adapter_config.get("timeout_seconds", 600)),
            before_spawn=lambda: _require_typed_source_records(
                context,
                plan.source_records,
                label="Xcelium",
            ),
        )
        payload = {
            **result.plan.as_dict(),
            "executed": True,
            "returncode": result.returncode,
            "passed": result.passed,
            "run_summary": _artifact_reference(result.run_summary, project),
            **metadata,
            "product_qualification_conclusion": product_conclusion,
        }
        output = context.output_path("evidence", "xcelium-evidence.json")
        output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return AdapterResult.succeeded(
            CollectedActionResult(
                artifacts=(
                    ProducedArtifact(
                        "evidence",
                        XCELIUM_EVIDENCE_KIND,
                        output,
                    ),
                ),
                facts=_fact_set(context, {
                    "passed": result.passed,
                    "simulator": result.plan.spec.simulator,
                    "evidence-role": metadata["evidence_role"],
                    "evidence-level": metadata["evidence_level"],
                    "evidence-scope": metadata["evidence_scope"],
                    "product-qualification-conclusion": product_conclusion,
                }),
            )
        )


class XceliumAmsVerificationAdapter:
    """Execute one owner-cataloged mixed-signal verification cell."""

    def run(self, context: ActionContext) -> AdapterResult:
        plan = context.require_action_plan(
            XCELIUM_AMS_ACTION_PLAN,
            XceliumAmsCellPlan,
        )
        _require_record(context, plan.as_dict())
        project = plan.spec.project
        _require_planned_project(context, project, plan.spec.owner)
        _require_cell_config(
            context,
            contract=plan.contract,
            project=project,
        )
        capability = context.capabilities["tool.cadence-xcelium"]
        metadata = _evidence_metadata(context)
        product_conclusion = _planned_product_conclusion(context)
        result = execute_xcelium_ams_cell(
            plan,
            artifacts=RunArtifacts.from_action_context(
                context,
                "evidence",
                artifact_source_state(project.project_root),
            ),
            xrun=capability.executable,
            timeout=int(context.adapter_config.get("timeout_seconds", 600)),
            before_spawn=lambda: _require_typed_source_records(
                context,
                plan.source_records,
                label="Xcelium AMS",
            ),
        )
        payload = {
            **result.plan.as_dict(),
            "executed": True,
            "returncode": result.returncode,
            "passed": result.passed,
            "run_summary": _artifact_reference(result.run_summary, project),
            **metadata,
            "product_qualification_conclusion": product_conclusion,
        }
        output = context.output_path("evidence", "xcelium-ams-evidence.json")
        output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return AdapterResult.succeeded(
            CollectedActionResult(
                artifacts=(
                    ProducedArtifact(
                        "evidence",
                        XCELIUM_AMS_EVIDENCE_KIND,
                        output,
                    ),
                ),
                facts=_fact_set(context, {
                    "passed": result.passed,
                    "simulator": result.plan.spec.simulator,
                    "evidence-role": metadata["evidence_role"],
                    "evidence-level": metadata["evidence_level"],
                    "evidence-scope": metadata["evidence_scope"],
                    "product-qualification-conclusion": product_conclusion,
                }),
            )
        )


def _cell_contract(
    node: FlowNode,
    project: Project,
    owner: RepositoryOwner,
    label: str,
) -> Path:
    cell = node.config.get("cell")
    if not isinstance(cell, str) or not cell:
        raise ValueError(f"{label} Action requires a cell contract")
    path, _relative = project.resolve_owner_file(
        owner,
        cell,
        f"{label} Action cell",
    )
    return path


def _xcelium_sources(
    project: Project,
    plan: XceliumCellPlan,
) -> tuple[SourceMember, ...]:
    records = {
        Path(path).resolve(): record
        for path, record in plan.source_records.items()
    }
    model_set = getattr(plan, "model_set", None)
    external_roots = (
        ()
        if model_set is None
        else (("platform-model", model_set.file.parent.resolve()),)
    )
    return project_source_members(
        project,
        set(records),
        label="Xcelium",
        records=records,
        external_roots=external_roots,
    )


def install_native_flow(
    registry: FlowRegistry,
    project: Project,
    owner: RepositoryOwner,
    *,
    client_factory: Callable[[], Any],
) -> None:
    """Install native OA/Xcelium planners and lazy Adapters as one module."""

    registry.register_adapter_factory(NATIVE_OA_PLAN_ADAPTER, NativeOaPlanAdapter)
    registry.register_adapter_factory(
        NATIVE_OA_SIMULATION_ADAPTER,
        lambda: NativeOaSimulationAdapter(client_factory=client_factory),
    )
    registry.register_adapter_factory(
        XCELIUM_VERIFICATION_ADAPTER,
        XceliumVerificationAdapter,
    )
    registry.register_adapter_factory(
        XCELIUM_AMS_VERIFICATION_ADAPTER,
        XceliumAmsVerificationAdapter,
    )

    oa_action_plan: ActionPlan | None = None

    def plan_oa(_node: FlowNode) -> ActionPlan:
        nonlocal oa_action_plan
        if oa_action_plan is None:
            planned = ProjectOaWorkflow(project, owner.name).plan()
            sources = project_source_members(
                project,
                set(oa_plan_source_paths(planned)),
                label="native OA",
            )
            validate_oa_plan_source_members(planned, sources)
            oa_action_plan = ActionPlan(
                NATIVE_OA_ACTION_PLAN,
                planned,
                planned.as_dict(),
                sources,
            )
        return oa_action_plan

    def plan_xcelium(node: FlowNode) -> ActionPlan:
        planned = plan_xcelium_cell(
            _cell_contract(node, project, owner, "Xcelium"),
            project=project,
        )
        return ActionPlan(
            XCELIUM_ACTION_PLAN,
            planned,
            planned.as_dict(),
            _xcelium_sources(project, planned),
        )

    def plan_xcelium_ams(node: FlowNode) -> ActionPlan:
        planned = plan_xcelium_ams_cell(
            _cell_contract(node, project, owner, "Xcelium AMS"),
            project=project,
        )
        return ActionPlan(
            XCELIUM_AMS_ACTION_PLAN,
            planned,
            planned.as_dict(),
            _xcelium_sources(project, planned),
        )

    registry.register_action_planner(NATIVE_OA_PLAN_ACTION, plan_oa)
    registry.register_action_planner(NATIVE_OA_SIMULATION_ACTION, plan_oa)
    registry.register_action_planner(XCELIUM_VERIFICATION_ACTION, plan_xcelium)
    registry.register_action_planner(
        XCELIUM_AMS_VERIFICATION_ACTION,
        plan_xcelium_ams,
    )


__all__ = [
    "NativeOaPlanAdapter",
    "NativeOaSimulationAdapter",
    "XceliumVerificationAdapter",
    "XceliumAmsVerificationAdapter",
    "install_native_flow",
]
