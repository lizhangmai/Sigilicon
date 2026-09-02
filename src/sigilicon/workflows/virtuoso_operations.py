"""Application use cases for direct Virtuoso operations."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Any, Mapping

from sigilicon.paths import ProjectContext
from sigilicon.virtuoso.library import LibraryCreateResult, create_project_library
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


@dataclass(frozen=True)
class ParameterUpdateResult:
    applied: Mapping[str, str]
    before: Mapping[str, str]
    after: Mapping[str, str]


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
