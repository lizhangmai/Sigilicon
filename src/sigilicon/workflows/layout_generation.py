"""Workflow for creating a Laygo2-generated OA layout."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sigilicon.artifacts import ArtifactRecord, new_identity
from sigilicon.layout.generator import build_layout_plan
from sigilicon.layout.ir import LayoutPlan
from sigilicon.layout.spec import LayoutSpec
from sigilicon.layout.spec import load_layout_spec
from sigilicon.paths import ProjectContext
from sigilicon.virtuoso.disposable import DisposableWork
from sigilicon.virtuoso.layout_generation import (
    delete_generated_layout_view,
    validate_layout_plan,
    validate_layout_view_absent,
    write_layout_plan,
)
from sigilicon.virtuoso.provenance import oa_view_digest
from sigilicon.virtuoso.workspace import OperationPolicy, workspace_operation
from sigilicon.workflows.source_control import inspect_source_state


@dataclass(frozen=True)
class LayoutGenerationResult:
    attempt_dir: Path | None
    manifest_path: Path | None
    fingerprint: str
    instance_count: int


@dataclass(frozen=True)
class LayoutPlanningResult:
    spec: LayoutSpec
    plan: LayoutPlan


def rollback_generated_layout(
    spec: LayoutSpec,
    expected_fingerprint: str,
    client: Any,
    *,
    artifact_root: Path | None = None,
    timeout: int = 60,
) -> Path:
    """Delete exactly one generated pilot view after matching its provenance."""

    paths = ProjectContext.from_project_root(spec.project_root, artifact_root=artifact_root)
    source_state = inspect_source_state(spec.project_root)
    attempt = ArtifactRecord.begin(
        paths.artifacts.layout_generation_attempt(
            spec.library, spec.cell, spec.view, new_identity()
        ),
        entities={"library": spec.library, "cell": spec.cell, "view": spec.view},
        operation="rollback-generated-layout",
        backend="virtuoso-oa",
        source_fingerprint=spec.source_fingerprint,
        run_fingerprint=expected_fingerprint,
    )
    attempt.write_json(
        "inputs",
        ("source-state.json",),
        source_state.as_dict(),
        label="source-state snapshot at generated-layout rollback start",
    )
    attempt.write_json(
        "inputs",
        ("rollback-target.json",),
        {
            "library": spec.library,
            "cell": spec.cell,
            "view": spec.view,
            "expected_fingerprint": expected_fingerprint,
            "expected_stage": spec.stage,
        },
        label="exact generated layout rollback target",
    )
    operation = None
    deleted = False
    with (
        attempt.failure_boundary(
            uncertainty=lambda: operation.uncertain_reason if operation else None,
            partial_failure=lambda: (
                {
                    "completed_stages": ["oa-delete"],
                    "failed_stage": "absence-validation-or-workspace-audit",
                    "cell": spec.cell,
                    "view": spec.view,
                }
                if deleted
                else None
            ),
        ),
        workspace_operation(
            client,
            paths.workspace_root,
            "rollback-generated-layout",
            policy=OperationPolicy.DIRECT_MUTATION,
        ) as operation,
        operation.view_lease(
            spec.library,
            cells=(spec.cell,),
            views=((spec.cell, spec.view),),
        ),
    ):
        operation.register_artifact(attempt)
        operation.require_project_library_target(client, spec.library)

        def commit() -> Path:
            completion = attempt.write_json(
                "evidence",
                ("completion.json",),
                {
                    "library": spec.library,
                    "cell": spec.cell,
                    "deleted_view": spec.view,
                    "matched_fingerprint": expected_fingerprint,
                    "matched_stage": spec.stage,
                    "absence_confirmed": True,
                },
                label="generated layout rollback proof",
            )
            return attempt.succeed(
                completion_evidence=(completion,),
                details={"deleted_view": spec.view},
            )

        deferred = operation.defer_commit(commit)
        with operation.mutation_scope(
            spec.library,
            cells=(spec.cell,),
            views=((spec.cell, spec.view),),
            phase=f"delete generated {spec.view} view",
        ):
            delete_generated_layout_view(
                client,
                library=spec.library,
                cell=spec.cell,
                view=spec.view,
                expected_fingerprint=expected_fingerprint,
                expected_stage=spec.stage,
                operation=operation,
                timeout=timeout,
            )
            deleted = True
        validate_layout_view_absent(
            client,
            library=spec.library,
            cell=spec.cell,
            view=spec.view,
            operation=operation,
            timeout=timeout,
        )
    if not deferred.completed:
        raise RuntimeError("layout rollback completed without committing its artifact")
    return attempt.paths.manifest


def plan_layout_spec(spec_path: Path, project_root: Path) -> LayoutPlanningResult:
    spec = load_layout_spec(spec_path, project_root=project_root)
    return LayoutPlanningResult(spec=spec, plan=build_layout_plan(spec))


def execute_layout_generation_spec(
    spec_path: Path,
    project_root: Path,
    client: Any,
    *,
    timeout: int = 120,
) -> tuple[LayoutSpec, LayoutGenerationResult]:
    spec = load_layout_spec(spec_path, project_root=project_root)
    return spec, generate_layout(spec, client, timeout=timeout)


def generate_layout(
    spec: LayoutSpec,
    client: Any,
    *,
    artifact_root: Path | None = None,
    overwrite: bool = False,
    timeout: int = 120,
    disposable: bool = False,
) -> LayoutGenerationResult:
    if not disposable:
        return _generate_layout_impl(
            spec,
            client,
            artifact_root=artifact_root,
            overwrite=overwrite,
            timeout=timeout,
            disposable=False,
        )
    with DisposableWork.create(prefix="sigilicon-oa-layout-") as work:
        return _generate_layout_impl(
            spec,
            client,
            artifact_root=artifact_root,
            overwrite=overwrite,
            timeout=timeout,
            disposable=True,
            _disposable_work=work,
        )


def _generate_layout_impl(
    spec: LayoutSpec,
    client: Any,
    *,
    artifact_root: Path | None = None,
    overwrite: bool = False,
    timeout: int = 120,
    disposable: bool = False,
    _disposable_work: DisposableWork | None = None,
) -> LayoutGenerationResult:
    """Run layout generation under its caller-owned work scope."""

    plan = build_layout_plan(spec)
    paths = ProjectContext.from_project_root(spec.project_root, artifact_root=artifact_root)
    if disposable:
        if _disposable_work is None:
            raise RuntimeError("disposable layout generation requires a work scope")
        attempt: Any = _disposable_work
    else:
        source_state = inspect_source_state(spec.project_root)
        attempt = ArtifactRecord.begin(
            paths.artifacts.layout_generation_attempt(
                spec.library, spec.cell, spec.view, new_identity()
            ),
            entities={"library": spec.library, "cell": spec.cell, "view": spec.view},
            operation="generate-layout",
            backend="laygo2+virtuoso-oa",
            source_fingerprint=spec.source_fingerprint,
            run_fingerprint=plan.fingerprint,
        )
        attempt.write_json(
            "inputs",
            ("source-state.json",),
            source_state.as_dict(),
            label="source-state snapshot at OA layout generation start",
        )
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
            ("layout-generator-dependencies", f"{index:02d}-{dependency.name}"),
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
                {
                    "path": snapshot.source_path.relative_to(
                        spec.project_root
                    ).as_posix(),
                    "sha256": snapshot.sha256,
                }
                for snapshot in spec.source_snapshots
            ],
        },
        label="canonical layout source provenance",
    )
    attempt.write_text(
        "inputs", ("layout-plan.json",), plan.canonical_json(), label="stable layout plan"
    )
    operation = None
    oa_written = False
    oa_sha256: str | None = None
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
            paths.workspace_root,
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
            if oa_sha256 is None:
                raise RuntimeError("layout commit is missing its OA content receipt")
            completion = attempt.write_json(
                "evidence",
                ("completion.json",),
                {
                    "library": spec.library,
                    "cell": spec.cell,
                    "view": spec.view,
                    "library_path": str(library_path),
                    "stage": plan.stage,
                    "layout_fingerprint": plan.fingerprint,
                    "oa_sha256": oa_sha256,
                    "instance_count": len(plan.instances),
                    "oa_completion_confirmed": True,
                },
                label="OA layout generation completion proof",
            )
            return attempt.succeed(
                completion_evidence=(completion,),
                details={
                    "instance_count": len(plan.instances),
                    "stage": plan.stage,
                    "oa_sha256": oa_sha256,
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
                plan,
                pcell_policy=spec.pdk.oa.pcell_policy,
                operation=operation,
                overwrite=overwrite,
                timeout=timeout,
            )
            oa_written = True
        validate_layout_plan(
            client,
            plan,
            pcell_policy=spec.pdk.oa.pcell_policy,
            operation=operation,
            timeout=timeout,
        )
        oa_sha256 = oa_view_digest(
            paths.workspace_root / spec.library / spec.cell / spec.view,
            allowed_symlink_root=spec.project_root,
        )
    if not disposable and (deferred is None or not deferred.completed):
        raise RuntimeError("layout generation completed without committing its artifact")
    return LayoutGenerationResult(
        attempt_dir=attempt.paths.root if not disposable else None,
        manifest_path=attempt.paths.manifest if not disposable else None,
        fingerprint=plan.fingerprint,
        instance_count=len(plan.instances),
    )
