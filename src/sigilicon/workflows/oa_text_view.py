"""Source-driven lifecycle for OA model text views."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sigilicon.domain.repository import Project
from sigilicon.virtuoso.disposable import DisposableWork
from sigilicon.virtuoso.oa import cell_view_exists, delete_cell_view
from sigilicon.virtuoso.text_view import import_oa_text_view
from sigilicon.virtuoso.workspace import OperationPolicy, workspace_operation


def sync_oa_text_view(
    client: Any,
    *,
    project: Project,
    library: str,
    cell: str,
    view: str,
    kind: str,
    source: Path,
    overwrite: bool = False,
    timeout: int = 300,
) -> None:
    """Materialize one source-owned OA model view.

    ``overwrite`` is used by a rebuild to replace disposable OA cache state
    with the current Git source.
    """

    with DisposableWork.create(prefix="sigilicon-oa-text-") as work:
        _sync_oa_text_view_impl(
            client,
            project=project,
            library=library,
            cell=cell,
            view=view,
            kind=kind,
            source=source,
            overwrite=overwrite,
            timeout=timeout,
            _work=work,
        )


def _sync_oa_text_view_impl(
    client: Any,
    *,
    project: Project,
    library: str,
    cell: str,
    view: str,
    kind: str,
    source: Path,
    overwrite: bool = False,
    timeout: int = 300,
    _work: DisposableWork | None = None,
) -> None:
    """Materialize one text view under its caller-owned temporary scope."""

    source_path = source.resolve()
    if not source_path.is_file() or not source_path.is_relative_to(project.project_root):
        raise ValueError("OA text-view source must be a project-owned file")
    if _work is None:
        raise RuntimeError("OA text-view synchronization requires a work scope")
    work = _work
    with workspace_operation(
        client,
        project.workspace_root,
        "sync-oa-text-view",
        policy=OperationPolicy.DIRECT_MUTATION,
    ) as operation, operation.view_lease(
        library,
        cells=(cell,),
        views=((cell, view),),
    ):
        if cell_view_exists(client, library, cell, view) and not overwrite:
            raise RuntimeError(
                f"refusing to replace existing OA text view {library}/{cell}/{view}"
            )
        if cell_view_exists(client, library, cell, view):
            with operation.mutation_scope(
                library,
                cells=(cell,),
                views=((cell, view),),
                phase=f"replace existing OA text view {library}/{cell}/{view}",
            ):
                delete_cell_view(
                    client,
                    library,
                    cell,
                    view,
                    operation=operation,
                    timeout=timeout,
                )
        with operation.mutation_scope(
            library,
            cells=(cell,),
            views=((cell, view),),
            phase=f"canonical OA text-view import {library}/{cell}/{view}",
        ):
            import_oa_text_view(
                client,
                library=library,
                cell=cell,
                kind=kind,
                view=view,
                # cdsTextTo5x materializes its native master as a source link.
                # Keep that link on the canonical project-owned file rather
                # than on this operation's disposable staging directory.
                source=source_path,
                log_dir=work.directory("logs", "text-view"),
                work_dir=work.directory("work", "text-view"),
                operation=operation,
                timeout=timeout,
            )
