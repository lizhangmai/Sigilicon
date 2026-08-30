"""Workflow for creating a Laygo2-generated OA layout."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sigilicon.domain.repository import Project
from sigilicon.layout.generator import build_layout_plan
from sigilicon.layout.ir import LayoutPlan
from sigilicon.layout.spec import LayoutSpec
from sigilicon.layout.spec import load_layout_spec
from sigilicon.virtuoso.disposable import DisposableWork
from sigilicon.virtuoso.layout_generation import (
    validate_layout_plan,
    write_layout_plan,
)
from sigilicon.virtuoso.workspace import OperationPolicy, workspace_operation
from sigilicon.workflows.run_artifacts import RunArtifacts


@dataclass(frozen=True)
class LayoutGenerationResult:
    attempt_dir: Path | None
    manifest_path: Path | None
    instance_count: int
    completion_path: Path | None = None


@dataclass(frozen=True)
class LayoutPlanningResult:
    """A layout spec paired with the plan built from that exact object."""

    spec: LayoutSpec
    plan: LayoutPlan = field(init=False)

    def __post_init__(self) -> None:
        plan = build_layout_plan(self.spec)
        identity = (
            plan.library,
            plan.cell,
            plan.view,
            plan.generator,
            plan.stage,
            plan.dbu_per_micron,
        )
        expected = (
            self.spec.library,
            self.spec.cell,
            self.spec.view,
            self.spec.generator,
            self.spec.stage,
            self.spec.layout_pdk.dbu_per_micron,
        )
        if identity != expected:
            raise ValueError("layout plan identity differs from its source spec")
        object.__setattr__(self, "plan", plan)


def plan_layout_spec(
    spec_path: Path,
    *,
    project: Project,
) -> LayoutPlanningResult:
    spec = load_layout_spec(spec_path, project=project)
    return LayoutPlanningResult(spec)


def generate_layout(
    spec: LayoutSpec | LayoutPlanningResult,
    client: Any,
    *,
    overwrite: bool = False,
    timeout: int = 120,
    disposable: bool = False,
    artifacts: RunArtifacts | None = None,
    operation_id: str | None = None,
    bind_operation: Any | None = None,
) -> LayoutGenerationResult:
    planning = (
        spec
        if isinstance(spec, LayoutPlanningResult)
        else LayoutPlanningResult(spec)
    )
    if not disposable and artifacts is None:
        raise ValueError("persistent layout generation requires Flow-owned artifacts")
    if artifacts is not None and disposable:
        raise ValueError("disposable layout generation cannot persist run artifacts")
    if not disposable:
        return _generate_layout_impl(
            planning,
            client,
            overwrite=overwrite,
            timeout=timeout,
            disposable=False,
            artifacts=artifacts,
            operation_id=operation_id,
            bind_operation=bind_operation,
        )
    with DisposableWork.create(prefix="sigilicon-oa-layout-") as work:
        return _generate_layout_impl(
            planning,
            client,
            overwrite=overwrite,
            timeout=timeout,
            disposable=True,
            _disposable_work=work,
        )


def _generate_layout_impl(
    planning: LayoutPlanningResult,
    client: Any,
    *,
    overwrite: bool = False,
    timeout: int = 120,
    disposable: bool = False,
    _disposable_work: DisposableWork | None = None,
    artifacts: RunArtifacts | None = None,
    operation_id: str | None = None,
    bind_operation: Any | None = None,
) -> LayoutGenerationResult:
    """Run layout generation under its caller-owned work scope."""

    spec = planning.spec
    execution_plan = planning.plan
    if disposable:
        if _disposable_work is None:
            raise RuntimeError("disposable layout generation requires a work scope")
        attempt: Any = _disposable_work
    else:
        assert artifacts is not None
        attempt = artifacts
    if not disposable:
        attempt.write_text(
            "inputs",
            ("canonical-subckt.scs",),
            spec.source_snapshot.text,
            label="selected canonical transistor source",
        )
        for index, snapshot in enumerate(spec.source_snapshots):
            attempt.write_text(
                "inputs",
                ("canonical-source", f"{index:02d}-{snapshot.source_path.name}"),
                snapshot.text,
                label="exact canonical Spectre source",
            )
        attempt.write_json(
            "inputs",
            ("canonical-source", "provenance.json"),
            {
                "top": spec.cell,
                "sources": [
                    snapshot.source_path.relative_to(spec.project_root).as_posix()
                    for snapshot in spec.source_snapshots
                ],
            },
            label="canonical layout source provenance",
        )
        attempt.write_text(
            "inputs",
            ("layout-plan.json",),
            execution_plan.canonical_json(),
            label="stable layout plan",
        )
    with (
        workspace_operation(
            client,
            spec.project.workspace_root,
            "generate-layout",
            policy=OperationPolicy.DIRECT_MUTATION,
            operation_id=operation_id,
        ) as operation,
        operation.view_lease(
            spec.library,
            cells=(spec.cell,),
            views=((spec.cell, spec.view),),
        ),
    ):
        if not disposable:
            if not callable(bind_operation):
                raise RuntimeError("managed layout generation requires operation binding")
            bind_operation(operation)
        operation.require_project_library_target(client, spec.library)
        info = client.library.get(spec.library, timeout=30)
        if str(info.technology_library or "") != spec.pdk.oa.technology_library:
            raise RuntimeError(
                f"library {spec.library} uses technology {info.technology_library!r}, "
                f"expected {spec.pdk.oa.technology_library!r}"
            )

        def commit() -> Path:
            completion_payload = {
                "library": spec.library,
                "cell": spec.cell,
                "view": spec.view,
                "stage": execution_plan.stage,
                "instance_count": len(execution_plan.instances),
                "oa_completion_confirmed": True,
            }
            completion = attempt.write_json(
                "outputs",
                ("completion.json",),
                completion_payload,
                label="OA layout generation completion proof",
            )
            return completion

        deferred = operation.defer_commit(commit) if not disposable else None
        with operation.mutation_scope(
            spec.library,
            cells=(spec.cell,),
            views=((spec.cell, spec.view),),
            phase=f"create generated {spec.view} view",
        ):
            write_layout_plan(
                client,
                execution_plan,
                operation=operation,
                overwrite=overwrite,
                timeout=timeout,
            )
        validate_layout_plan(
            client,
            execution_plan,
            operation=operation,
            timeout=timeout,
        )
    if not disposable and (deferred is None or not deferred.completed):
        raise RuntimeError("layout generation completed without committing its artifact")
    completion_path = (
        None if disposable else attempt.path("outputs", "completion.json")
    )
    return LayoutGenerationResult(
        attempt_dir=attempt.root if not disposable else None,
        manifest_path=None if disposable else attempt.root / "run_manifest.json",
        instance_count=len(execution_plan.instances),
        completion_path=completion_path,
    )
