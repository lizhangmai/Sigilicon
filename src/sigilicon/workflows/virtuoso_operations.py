"""Application use cases for direct Virtuoso operations."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Any, Mapping

from sigilicon.artifacts import ArtifactRecord, new_identity
from sigilicon.paths import ProjectContext
from sigilicon.virtuoso.library import LibraryCreateResult, create_project_library
from sigilicon.virtuoso.netlisting import export_netlist
from sigilicon.virtuoso.oa import (
    WindowCloseResult,
    assert_cell_has_no_open_views,
    close_visible_cell_windows,
)
from sigilicon.virtuoso.schematic import (
    normalize_instance_parameters,
    read_instance_parameters,
    set_instance_parameters,
)
from sigilicon.virtuoso.capability import (
    WorkspaceAuthority,
    require_workspace_capability,
)
from sigilicon.virtuoso.confirmation import require_bridge_confirmation
from sigilicon.virtuoso.workspace import (
    OperationPolicy,
    require_project_library_path,
    workspace_operation,
)
from sigilicon.workflows.source_control import artifact_source_state


_SPECTRE_RELATIVE_INCLUDE = re.compile(
    r'^\s*include\s+"(?P<path>[^"]+)"',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParameterUpdateResult:
    applied: Mapping[str, str]
    before: Mapping[str, str]
    after: Mapping[str, str]


@dataclass(frozen=True)
class ProjectNetlistExport:
    """Exact artifact references produced by one OA netlist export."""

    run_id: str
    run_dir: Path
    input_scs: Path
    support_files: tuple[Path, ...]
    manifest_path: Path


def _copy_export_support_files(
    record: ArtifactRecord,
    generated: Path,
) -> tuple[Path, ...]:
    """Preserve direct relative Spectre include files beside an OA export.

    Cadence's ``input.scs`` commonly references a local helper such as
    ``ade_e.scs``.  The generic export artifact must carry that helper too;
    absolute PDK model paths remain declared external inputs.
    """

    source_text = generated.read_text(encoding="utf-8", errors="strict")
    copied: list[Path] = []
    for line in source_text.splitlines():
        match = _SPECTRE_RELATIVE_INCLUDE.match(line)
        if match is None:
            continue
        relative = Path(match.group("path"))
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            continue
        source = generated.parent / relative
        if not source.is_file():
            continue
        if len(relative.parts) > 1:
            record.directory("outputs", *relative.parts[:-1])
        copied.append(
            record.copy_file(
                "outputs",
                relative.parts,
                source,
                label="OA netlist relative include support file",
            )
        )
    return tuple(copied)


def export_project_netlist(
    client: Any,
    paths: ProjectContext,
    library: str,
    cell: str,
    *,
    artifact_root: Path | None = None,
    view: str,
    simulator: str,
    timeout: int,
) -> ProjectNetlistExport:
    if artifact_root is not None:
        paths = paths.with_artifact_root(artifact_root)
    record = ArtifactRecord.begin(
        paths.artifacts.execution(
            owner=library,
            target=cell,
            flow="netlist-export",
            variant=f"{view}-{simulator}",
            identity=new_identity(),
            artifact_kind="netlist_export",
            identity_kind="run_id",
        ),
        entities={
            "library": library,
            "cell": cell,
            "view": view,
            "simulator": simulator,
        },
        operation="export-netlist",
        backend=simulator,
        source=artifact_source_state(paths.project_root),
    )
    record.write_json(
        "inputs",
        ("oa-view.json",),
        {
            "kind": "oa-view-reference",
            "workspace": str(paths.workspace_root),
            "library": library,
            "cell": cell,
            "view": view,
            "simulator": simulator,
        },
        label="exact OA view export request",
    )
    operation = None
    stable_result: Path | None = None
    support_files: tuple[Path, ...] = ()
    try:
        with workspace_operation(
            client,
            paths.workspace_root,
            "export-netlist",
            policy=OperationPolicy.READ_ONLY,
        ) as operation:
            operation.register_artifact(record)
            with operation.view_lease(
                library, cells=(cell,), views=((cell, view),)
            ):
                generated = export_netlist(
                    client,
                    library,
                    cell,
                    record.paths.role("work"),
                    view=view,
                    simulator=simulator,
                    timeout=timeout,
                    operation=operation,
                )
                stable_result = record.copy_file(
                    "outputs",
                    (generated.name,),
                    generated,
                    label="exported netlist",
                )
                support_files = _copy_export_support_files(record, generated)
                record.add_file(
                    "work",
                    record.paths.role("work"),
                    label="netlister work directory",
                )

                def fail_export(error: BaseException) -> None:
                    if record.status == "running":
                        record.fail(
                            error,
                            uncertain_reason=operation.uncertain_reason,
                        )

                operation.defer_commit(
                    lambda: record.succeed(
                        completion_evidence=(stable_result,),
                        details={
                            "generated_file": stable_result.name,
                            "relative_support_files": [
                                path.relative_to(record.paths.role("outputs")).as_posix()
                                for path in support_files
                            ],
                        },
                    ),
                    on_failure=fail_export,
                )
    except BaseException as error:
        if record.status == "running":
            record.fail(
                error,
                uncertain_reason=(operation.uncertain_reason if operation else None),
            )
        raise
    if stable_result is None:
        raise RuntimeError("netlist export completed without a stable input.scs result")
    return ProjectNetlistExport(
        run_id=record.paths.identity,
        run_dir=record.paths.root,
        input_scs=stable_result,
        support_files=support_files,
        manifest_path=record.paths.manifest,
    )


def open_project_cell(
    client: Any,
    paths: ProjectContext,
    library: str,
    cell: str,
    view: str,
) -> None:
    with workspace_operation(
        client,
        paths.workspace_root,
        "open-cell",
        policy=OperationPolicy.GUI_ACTION,
    ) as operation:
        require_workspace_capability(
            operation,
            client,
            authority=WorkspaceAuthority.GUI,
        )
        operation.require_project_library_target(client, library)
        result = require_bridge_confirmation(
            operation,
            f"open window {library}/{cell}/{view}",
            lambda: client.open_window(library, cell, view=view),
        )
        if result.is_nil:
            raise RuntimeError(f"geOpen returned nil for {library}/{cell}/{view}")


_LIBRARY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")


def create_library(
    client: Any,
    paths: ProjectContext,
    *,
    library: str,
    library_path: Path,
    technology_library: str | None,
    if_missing: bool,
) -> LibraryCreateResult:
    if not _LIBRARY_RE.fullmatch(library):
        raise ValueError(f"invalid Virtuoso library name: {library!r}")
    resolved = Path(os.path.abspath(library_path))
    if resolved == paths.workspace_root or not resolved.is_relative_to(
        paths.workspace_root
    ):
        raise RuntimeError(
            f"refusing to create a library outside the project workspace: {resolved}"
        )
    with workspace_operation(
        client,
        paths.workspace_root,
        "create-library",
    ) as operation:
        with operation.mutation_scope(
            library,
            cells=None,
            phase="create project library",
            expected_library_path=resolved,
            require_view_lease=False,
        ):
            return create_project_library(
                client,
                library=library,
                path=resolved,
                technology_library=technology_library,
                cds_lib=paths.workspace_root / "cds.lib",
                if_missing=if_missing,
                operation=operation,
            )


def close_cell(
    client: Any,
    paths: ProjectContext,
    library: str,
    cell: str,
    view: str | None,
) -> WindowCloseResult:
    with workspace_operation(
        client,
        paths.workspace_root,
        "close-cell",
        policy=OperationPolicy.GUI_ACTION,
    ) as operation:
        require_project_library_path(operation, library)
        return close_visible_cell_windows(
            client,
            library,
            cell,
            view,
            operation=operation,
        )


def update_instance_parameters(
    client: Any,
    paths: ProjectContext,
    library: str,
    cell: str,
    instance: str,
    params: Mapping[str, str],
) -> ParameterUpdateResult:
    normalized = normalize_instance_parameters(params)
    names = tuple(normalized)
    with workspace_operation(
        client,
        paths.workspace_root,
        "manual-set-params",
        policy=OperationPolicy.DIRECT_MUTATION,
    ) as operation:
        with operation.view_lease(
            library,
            cells=(cell,),
            views=((cell, "schematic"),),
        ):
            before = read_instance_parameters(
                client,
                library,
                cell,
                instance,
                names,
                operation=operation,
            )
            with operation.mutation_scope(
                library,
                cells=(cell,),
                views=((cell, "schematic"),),
                phase="set instance parameters",
            ):
                applied = set_instance_parameters(
                    client,
                    library,
                    cell,
                    instance,
                    normalized,
                    operation=operation,
                )
            after = read_instance_parameters(
                client,
                library,
                cell,
                instance,
                names,
                operation=operation,
            )
            assert_cell_has_no_open_views(client, library, cell)
    return ParameterUpdateResult(applied=applied, before=before, after=after)
