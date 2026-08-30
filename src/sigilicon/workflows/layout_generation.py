"""Workflow for creating a Laygo2-generated OA layout."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sigilicon.artifacts import ArtifactRecord, new_identity
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
from sigilicon.workflows.source_control import artifact_source_state


@dataclass(frozen=True)
class LayoutGenerationResult:
    attempt_dir: Path | None
    manifest_path: Path | None
    instance_count: int


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
    project_root: Path | None = None,
    *,
    project: Project | None = None,
) -> LayoutPlanningResult:
    repository = Project.bind(project=project, project_root=project_root)
    spec = load_layout_spec(spec_path, project=repository)
    return LayoutPlanningResult(spec)


def execute_layout_generation_spec(
    spec_path: Path,
    project_root: Path | None = None,
    client: Any = None,
    *,
    project: Project | None = None,
    timeout: int = 120,
) -> tuple[LayoutSpec, LayoutGenerationResult]:
    if client is None:
        raise ValueError("layout generation requires an OA client")
    repository = Project.bind(project=project, project_root=project_root)
    spec = load_layout_spec(spec_path, project=repository)
    return spec, generate_layout(spec, client, timeout=timeout)


def generate_layout(
    spec: LayoutSpec | LayoutPlanningResult,
    client: Any,
    *,
    artifact_root: Path | None = None,
    overwrite: bool = False,
    timeout: int = 120,
    disposable: bool = False,
) -> LayoutGenerationResult:
    planning = (
        spec
        if isinstance(spec, LayoutPlanningResult)
        else LayoutPlanningResult(spec)
    )
    if isinstance(spec, LayoutPlanningResult) and not disposable:
        raise ValueError("preplanned layout execution must be disposable")
    if not disposable:
        return _generate_layout_impl(
            planning,
            client,
            artifact_root=artifact_root,
            overwrite=overwrite,
            timeout=timeout,
            disposable=False,
        )
    with DisposableWork.create(prefix="sigilicon-oa-layout-") as work:
        return _generate_layout_impl(
            planning,
            client,
            artifact_root=artifact_root,
            overwrite=overwrite,
            timeout=timeout,
            disposable=True,
            _disposable_work=work,
        )


def _generate_layout_impl(
    planning: LayoutPlanningResult,
    client: Any,
    *,
    artifact_root: Path | None = None,
    overwrite: bool = False,
    timeout: int = 120,
    disposable: bool = False,
    _disposable_work: DisposableWork | None = None,
) -> LayoutGenerationResult:
    """Run layout generation under its caller-owned work scope."""

    spec = planning.spec
    execution_plan = planning.plan
    project = (
        spec.project
        if artifact_root is None
        else spec.project.with_artifact_root(artifact_root)
    )
    if disposable:
        if _disposable_work is None:
            raise RuntimeError("disposable layout generation requires a work scope")
        attempt: Any = _disposable_work
    else:
        attempt = ArtifactRecord.begin(
            project.artifacts.execution(
                owner=spec.library,
                target=spec.cell,
                flow="layout-generation",
                variant=spec.view,
                identity=new_identity(),
                artifact_kind="layout_generation",
                identity_kind="attempt_id",
            ),
            entities={"library": spec.library, "cell": spec.cell, "view": spec.view},
            operation="generate-layout",
            backend="laygo2+virtuoso-oa",
            source=artifact_source_state(spec.project_root),
        )
    if not disposable:
        attempt.copy_file(
            "inputs", ("layout.toml",), spec.path, label="canonical layout intent"
        )
        attempt.copy_file(
            "inputs",
            ("layout-generator.py",),
            spec.generator_source,
            label="design-owned layout generator source",
        )
        for index, dependency in enumerate(spec.generator_dependencies):
            attempt.copy_file(
                "inputs",
                (
                    "layout-generator-dependencies",
                    f"{index:02d}-{dependency.name}",
                ),
                dependency,
                label="design-owned layout generator dependency",
            )
        for index, (module, dependency) in enumerate(
            zip(spec.generator_modules, spec.generator_module_sources, strict=True)
        ):
            attempt.copy_file(
                "inputs",
                ("layout-generator-modules", f"{index:02d}-{dependency.name}"),
                dependency,
                label=f"installed layout generator module {module}",
            )
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
    operation = None
    oa_written = False
    failure_context = (
        attempt.failure_boundary(
            uncertainty=lambda: operation.uncertain_reason if operation else None,
            partial_failure=lambda: (
                {
                    "completed_stages": ["oa-write"],
                    "failed_stage": "oa-validation-or-workspace-audit",
                    "cell": spec.cell,
                    "view": spec.view,
                }
                if oa_written
                else None
            ),
        )
        if not disposable
        else nullcontext()
    )
    with (
        failure_context,
        workspace_operation(
            client,
            project.workspace_root,
            "generate-layout",
            policy=OperationPolicy.DIRECT_MUTATION,
        ) as operation,
        operation.view_lease(
            spec.library,
            cells=(spec.cell,),
            views=((spec.cell, spec.view),),
        ),
    ):
        if not disposable:
            operation.register_artifact(attempt)
        library_path = operation.require_project_library_target(client, spec.library)
        info = client.library.get(spec.library, timeout=30)
        if str(info.technology_library or "") != spec.pdk.oa.technology_library:
            raise RuntimeError(
                f"library {spec.library} uses technology {info.technology_library!r}, "
                f"expected {spec.pdk.oa.technology_library!r}"
            )

        def commit() -> Path:
            completion = attempt.write_json(
                "outputs",
                ("completion.json",),
                {
                    "library": spec.library,
                    "cell": spec.cell,
                    "view": spec.view,
                    "library_path": str(library_path),
                    "stage": execution_plan.stage,
                    "instance_count": len(execution_plan.instances),
                    "oa_completion_confirmed": True,
                },
                label="OA layout generation completion proof",
            )
            return attempt.succeed(
                completion_evidence=(completion,),
                details={
                    "instance_count": len(execution_plan.instances),
                    "stage": execution_plan.stage,
                },
            )

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
            oa_written = True
        validate_layout_plan(
            client,
            execution_plan,
            operation=operation,
            timeout=timeout,
        )
    if not disposable and (deferred is None or not deferred.completed):
        raise RuntimeError("layout generation completed without committing its artifact")
    return LayoutGenerationResult(
        attempt_dir=attempt.paths.root if not disposable else None,
        manifest_path=attempt.paths.manifest if not disposable else None,
        instance_count=len(execution_plan.instances),
    )
