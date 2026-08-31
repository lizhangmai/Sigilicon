"""Project-bound Adapters for native-OA and Xcelium typed Actions."""

from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
from typing import Any

from sigilicon.domain.repository import Project
from sigilicon.flow import (
    ActionContext,
    AdapterResult,
    CollectedActionResult,
    FlowExecutionError,
    ProducedArtifact,
)
from sigilicon.flow.native import (
    NATIVE_OA_EVIDENCE_KIND,
    NATIVE_OA_PLAN_KIND,
    XCELIUM_EVIDENCE_KIND,
    XCELIUM_AMS_EVIDENCE_KIND,
)
from sigilicon.virtuoso.client import get_client
from sigilicon.workflows.project_oa import ProjectOaWorkflow
from sigilicon.workflows.oa_simulation import execute_oa_maestro_testbench
from sigilicon.workflows.run_artifacts import FlowRunArtifacts
from sigilicon.workflows.source_control import artifact_source_state
from sigilicon.workflows.xcelium import execute_xcelium_cell, plan_xcelium_cell
from sigilicon.workflows.xcelium_ams import (
    execute_xcelium_ams_cell,
    plan_xcelium_ams_cell,
)


def _require_bound_project(
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
        raise FlowExecutionError("Action project owner scope drift")
    return project


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


class NativeOaPlanAdapter:
    def __init__(self, project: Project, owner: str) -> None:
        self._project = project
        self._owner = project.owner(owner).name

    def run(self, context: ActionContext) -> AdapterResult:
        project = _require_bound_project(context, self._project, self._owner)
        plan = ProjectOaWorkflow(project, self._owner).plan()
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
                facts={
                    "source-plan-valid": True,
                    "source-cell-count": len(plan.cells),
                    "source-layout-count": len(plan.layouts),
                    "source-testbench-count": len(plan.testbenches),
                },
            )
        )


class NativeOaSimulationAdapter:
    def __init__(
        self,
        project: Project,
        owner: str,
        *,
        client_factory: Callable[[], Any] = get_client,
    ) -> None:
        self._project = project
        self._owner = project.owner(owner).name
        self._client_factory = client_factory

    def run(self, context: ActionContext) -> AdapterResult:
        project = _require_bound_project(context, self._project, self._owner)
        workflow = ProjectOaWorkflow(project, self._owner)
        plan = workflow.plan()
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
            artifacts=FlowRunArtifacts(
                context,
                "evidence",
                artifact_source_state(project.project_root),
            ),
            operation_id=context.operation_id,
            bind_operation=context.bind_workspace_operation,
            timeout=int(context.adapter_config.get("timeout_seconds", 600)),
        )
        payload = result.as_dict()
        payload.update(metadata)
        payload["product_qualification_conclusion"] = False
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
                facts={
                    "execution-completed": True,
                    "native-evidence-status": result.evidence.status,
                    "evidence-role": metadata["evidence_role"],
                    "evidence-level": metadata["evidence_level"],
                    "evidence-scope": metadata["evidence_scope"],
                    "product-qualification-conclusion": False,
                },
                details={"product_qualification_conclusion": False},
            )
        )


class XceliumVerificationAdapter:
    def __init__(self, project: Project, owner: str) -> None:
        self._project = project
        self._owner = project.owner(owner).name

    def run(self, context: ActionContext) -> AdapterResult:
        project = _require_bound_project(context, self._project, self._owner)
        capability = context.capabilities["tool.cadence-xcelium"]
        metadata = _evidence_metadata(context)
        plan = plan_xcelium_cell(
            Path(_text_config(context, "cell")), project=project
        )
        result = execute_xcelium_cell(
            plan,
            artifacts=FlowRunArtifacts(
                context,
                "evidence",
                artifact_source_state(project.project_root),
            ),
            xrun=capability.executable,
            timeout=int(context.adapter_config.get("timeout_seconds", 600)),
        )
        payload = {
            **result.plan.as_dict(),
            "executed": True,
            "returncode": result.returncode,
            "passed": result.passed,
            "run_summary": _artifact_reference(result.run_summary, project),
            **metadata,
            "product_qualification_conclusion": False,
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
                facts={
                    "passed": result.passed,
                    "simulator": result.plan.spec.simulator,
                    "evidence-role": metadata["evidence_role"],
                    "evidence-level": metadata["evidence_level"],
                    "evidence-scope": metadata["evidence_scope"],
                    "product-qualification-conclusion": False,
                },
                details={"product_qualification_conclusion": False},
            )
        )


class XceliumAmsVerificationAdapter:
    """Execute one owner-cataloged mixed-signal verification cell."""

    def __init__(self, project: Project, owner: str) -> None:
        self._project = project
        self._owner = project.owner(owner).name

    def run(self, context: ActionContext) -> AdapterResult:
        project = _require_bound_project(context, self._project, self._owner)
        capability = context.capabilities["tool.cadence-xcelium"]
        metadata = _evidence_metadata(context)
        plan = plan_xcelium_ams_cell(
            Path(_text_config(context, "cell")), project=project
        )
        result = execute_xcelium_ams_cell(
            plan,
            artifacts=FlowRunArtifacts(
                context,
                "evidence",
                artifact_source_state(project.project_root),
            ),
            xrun=capability.executable,
            timeout=int(context.adapter_config.get("timeout_seconds", 600)),
        )
        payload = {
            **result.plan.as_dict(),
            "executed": True,
            "returncode": result.returncode,
            "passed": result.passed,
            "run_summary": _artifact_reference(result.run_summary, project),
            **metadata,
            "product_qualification_conclusion": False,
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
                facts={
                    "passed": result.passed,
                    "simulator": result.plan.spec.simulator,
                    "evidence-role": metadata["evidence_role"],
                    "evidence-level": metadata["evidence_level"],
                    "evidence-scope": metadata["evidence_scope"],
                    "product-qualification-conclusion": False,
                },
                details={"product_qualification_conclusion": False},
            )
        )


__all__ = [
    "NativeOaPlanAdapter",
    "NativeOaSimulationAdapter",
    "XceliumVerificationAdapter",
    "XceliumAmsVerificationAdapter",
]
