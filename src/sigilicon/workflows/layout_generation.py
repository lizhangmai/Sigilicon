"""Workflow for creating a Laygo2-generated OA layout."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

from sigilicon.artifacts import read_nofollow_text
from sigilicon.domain.platform import PlatformSnapshot
from sigilicon.project import Project
from sigilicon.layout.generator import (
    LayoutGeneratorInput,
    build_layout_plan_from_sources,
)
from sigilicon.layout.ir import LayoutPlan
from sigilicon.layout.spec import LayoutSpec
from sigilicon.layout.spec import load_layout_spec, resolve_layout_spec
from sigilicon.virtuoso.disposable import DisposableWork
from sigilicon.virtuoso.layout_generation import (
    validate_layout_plan,
    write_layout_plan,
)
from sigilicon.virtuoso.workspace import OperationPolicy, workspace_operation
from sigilicon.execution._workspace import ExecutionWorkspace


@dataclass(frozen=True)
class LayoutGenerationResult:
    instance_count: int


@dataclass(frozen=True)
class LayoutPlanningResult:
    """Pure source snapshot awaiting managed LayoutIR generation."""

    spec: LayoutSpec
    source_records: Mapping[Path, str] = field(
        default_factory=lambda: MappingProxyType({}),
        repr=False,
    )
    plan: LayoutPlan | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        plan = self.plan
        if plan is None:
            return
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


def _layout_source_paths(spec: LayoutSpec) -> tuple[Path, ...]:
    paths = {
        *spec.source_documents,
        *spec.pdk.source_paths,
        spec.generator_source,
        *spec.generator_dependencies,
        *(
            source
            for source in spec.generator_module_sources
            if source.is_relative_to(spec.project_root)
        ),
        *(snapshot.source_path for snapshot in spec.source_snapshots),
    }
    if spec.oa_assembly_manifest is not None:
        paths.add(spec.oa_assembly_manifest)
    if spec.physical_verification is not None:
        paths.add(spec.physical_verification.path)
    return tuple(sorted(Path(path).resolve() for path in paths))


def plan_layout_snapshot(spec: LayoutSpec) -> LayoutPlanningResult:
    """Freeze layout inputs without executing owner-authored Python."""
    paths = _layout_source_paths(spec)
    before = {path: read_nofollow_text(path) for path in paths}
    planning = LayoutPlanningResult(
        spec,
        source_records=MappingProxyType(before),
    )
    resolve_layout_spec(spec.path, project=spec.project, snapshot=spec)
    after = {path: read_nofollow_text(path) for path in paths}
    if after != before:
        raise ValueError("layout source changed while its typed plan was built")
    return planning


def with_layout_ir(
    planning: LayoutPlanningResult,
    plan: LayoutPlan,
) -> LayoutPlanningResult:
    """Attach one generated LayoutIR value after checking its source identity."""

    if planning.plan is not None:
        raise ValueError("layout planning result already contains LayoutIR")
    return LayoutPlanningResult(
        planning.spec,
        source_records=planning.source_records,
        plan=plan,
    )


def build_managed_layout_ir(
    planning: LayoutPlanningResult,
    *,
    source_paths: Mapping[Path, Path],
    workspace: ExecutionWorkspace,
    python_executable: Path,
) -> LayoutPlanningResult:
    """Generate LayoutIR from sealed inputs inside one managed work tree."""

    if planning.plan is not None:
        raise ValueError("managed LayoutIR generation requires a pure snapshot")
    root = workspace.path("work", "layout-ir")
    if root.exists():
        raise ValueError("managed layout project root must be new")
    workspace.directory("work", "layout-ir")
    original_root = planning.spec.project_root.resolve()
    materialized: dict[Path, Path] = {}
    for original, expected in planning.source_records.items():
        source = Path(original).resolve()
        if not source.is_relative_to(original_root):
            continue
        try:
            sealed = source_paths[source]
        except KeyError as exc:
            raise ValueError("layout source is outside the sealed closure") from exc
        text = read_nofollow_text(sealed)
        if text != expected:
            raise ValueError("sealed layout source disagrees with its snapshot")
        relative = source.relative_to(original_root)
        target = workspace.write_text(
            "work",
            ("layout-ir", *relative.parts),
            text,
        )
        materialized[source] = target

    def bound(source: Path) -> Path:
        original = Path(source).resolve()
        if original.is_relative_to(original_root):
            try:
                return materialized[original]
            except KeyError as exc:
                raise ValueError("layout generator input is outside the sealed closure") from exc
        return original

    snapshots = tuple(
        replace(snapshot, source_path=bound(snapshot.source_path))
        for snapshot in planning.spec.source_snapshots
    )
    by_source = {snapshot.source_path.resolve(): snapshot for snapshot in snapshots}
    source_snapshot_path = bound(planning.spec.source_snapshot.source_path).resolve()
    try:
        source_snapshot = by_source[source_snapshot_path]
    except KeyError as exc:
        raise ValueError("layout source snapshot closure is incomplete") from exc
    generator_input = LayoutGeneratorInput(
        library=planning.spec.library,
        cell=planning.spec.cell,
        view=planning.spec.view,
        generator=planning.spec.generator,
        stage=planning.spec.stage,
        source_snapshot=source_snapshot,
        source_snapshots=snapshots,
        ports=planning.spec.ports,
        directions=planning.spec.directions,
        primitive_masters=planning.spec.primitive_masters,
        technology_library=planning.spec.pdk.oa.technology_library,
        dbu_per_micron=planning.spec.layout_pdk.dbu_per_micron,
    )
    plan = build_layout_plan_from_sources(
        generator_input,
        project_root=root,
        generator_source=bound(planning.spec.generator_source),
        python_executable=python_executable,
    )
    return with_layout_ir(planning, plan)


def plan_layout_spec(
    spec_path: Path,
    *,
    project: Project,
    platform: PlatformSnapshot | None = None,
) -> LayoutPlanningResult:
    spec = load_layout_spec(spec_path, project=project, platform=platform)
    return plan_layout_snapshot(spec)


def generate_layout(
    planning: LayoutPlanningResult,
    client: Any,
    *,
    overwrite: bool = False,
    timeout: int = 120,
    disposable: bool = False,
    artifacts: ExecutionWorkspace | None = None,
    operation_id: str | None = None,
    bind_operation: Any | None = None,
    record_uncertainty: Callable[[str], None] | None = None,
) -> LayoutGenerationResult:
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
            record_uncertainty=record_uncertainty,
        )
    with DisposableWork.create(prefix="sigilicon-oa-layout-") as work:
        return _generate_layout_impl(
            planning,
            client,
            overwrite=overwrite,
            timeout=timeout,
            disposable=True,
            _disposable_work=work,
            operation_id=operation_id,
            bind_operation=bind_operation,
            record_uncertainty=record_uncertainty,
        )


def _generate_layout_impl(
    planning: LayoutPlanningResult,
    client: Any,
    *,
    overwrite: bool = False,
    timeout: int = 120,
    disposable: bool = False,
    _disposable_work: DisposableWork | None = None,
    artifacts: ExecutionWorkspace | None = None,
    operation_id: str | None = None,
    bind_operation: Any | None = None,
    record_uncertainty: Callable[[str], None] | None = None,
) -> LayoutGenerationResult:
    """Run layout generation under its caller-owned work scope."""

    spec = planning.spec
    execution_plan = planning.plan
    if execution_plan is None:
        raise ValueError("layout generation requires managed LayoutIR")
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
        )
        for index, snapshot in enumerate(spec.source_snapshots):
            attempt.write_text(
                "inputs",
                ("canonical-source", f"{index:02d}-{snapshot.source_path.name}"),
                snapshot.text,
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
        )
        attempt.write_text(
            "inputs",
            ("layout-plan.json",),
            execution_plan.canonical_json(),
        )
    operation = None
    try:
        with workspace_operation(
            client,
            spec.project.workspace_root,
            "generate-layout",
            policy=OperationPolicy.DIRECT_MUTATION,
            operation_id=operation_id,
        ) as operation:
            if callable(bind_operation):
                bind_operation(operation)
            elif not disposable:
                raise RuntimeError("managed layout generation requires operation binding")
            with operation.view_lease(
                spec.library,
                cells=(spec.cell,),
                views=((spec.cell, spec.view),),
            ):
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
    except BaseException:
        reason = getattr(operation, "uncertain_reason", None)
        if isinstance(reason, str) and reason and callable(record_uncertainty):
            record_uncertainty(reason)
        raise
    if not disposable and (deferred is None or not deferred.completed):
        raise RuntimeError("layout generation completed without committing its artifact")
    return LayoutGenerationResult(
        instance_count=len(execution_plan.instances),
    )
