"""Source-driven lifecycle for OA model text views."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from sigilicon.domain.provenance import digest
from sigilicon.paths import ProjectContext
from sigilicon.virtuoso.disposable import DisposableWork
from sigilicon.virtuoso.oa import cell_view_exists, delete_cell_view
from sigilicon.virtuoso.text_view import import_oa_text_view
from sigilicon.virtuoso.workspace import OperationPolicy, workspace_operation


def oa_text_view_fingerprint(
    *, library: str, cell: str, view: str, kind: str, source: Path
) -> str:
    return digest(
        {
            "library": library,
            "cell": cell,
            "view": view,
            "kind": kind,
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        }
    )


def sync_oa_text_view(
    client: Any,
    *,
    project_root: Path,
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

    with DisposableWork.create(prefix="llm-cim-oa-text-") as work:
        _sync_oa_text_view_impl(
            client,
            project_root=project_root,
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
    project_root: Path,
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

    paths = ProjectContext.from_project_root(project_root)
    source_path = source.resolve()
    if not source_path.is_file() or not source_path.is_relative_to(project_root.resolve()):
        raise ValueError("OA text-view source must be a project-owned file")
    source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
    if _work is None:
        raise RuntimeError("OA text-view synchronization requires a work scope")
    work = _work
    with workspace_operation(
        client,
        paths.workspace_root,
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
                source_sha256=source_sha256,
                log_dir=work.directory("logs", "text-view"),
                work_dir=work.directory("work", "text-view"),
                operation=operation,
                timeout=timeout,
            )
