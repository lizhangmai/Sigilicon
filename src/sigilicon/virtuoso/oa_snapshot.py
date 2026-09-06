"""Lease-bound import and attestation of native OA source snapshots."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from sigilicon.artifacts import write_immutable_bytes
from sigilicon.domain.oa_snapshot import NativeOaSnapshot
from sigilicon.virtuoso.library import ensure_project_library
from sigilicon.virtuoso.oa import cell_view_exists, delete_cell_view
from sigilicon.virtuoso.workspace import OperationPolicy, workspace_operation


def attest_native_snapshot(snapshot: NativeOaSnapshot, client: Any, operation: Any) -> None:
    library = operation.require_project_library_target(client, snapshot.library)
    info = client.library.get(snapshot.library, timeout=30)
    if str(info.technology_library or "") != snapshot.technology_library:
        raise ValueError("native OA snapshot technology drift")
    snapshot.verify(library / snapshot.cell / snapshot.view)


def import_native_snapshot(snapshot: NativeOaSnapshot, client: Any, *, workspace_root: Path,
                           operation_id: str | None = None, bind_operation: Any = None) -> None:
    with workspace_operation(client, workspace_root, "ensure-native-oa-library", policy=OperationPolicy.RECURSIVE_OA,
                             operation_id=operation_id) as operation:
        if callable(bind_operation):
            bind_operation(operation)
        with operation.mutation_scope(snapshot.library, cells=None, phase="ensure native OA library",
                                      expected_library_path=workspace_root / snapshot.library, require_view_lease=False):
            ensure_project_library(client, library=snapshot.library, path=workspace_root / snapshot.library,
                                   technology_library=snapshot.technology_library, cds_lib=workspace_root / "cds.lib", operation=operation)
    with workspace_operation(client, workspace_root, "import-native-oa-source", policy=OperationPolicy.DIRECT_MUTATION,
                             operation_id=operation_id) as operation, operation.view_lease(
            snapshot.library, cells=(snapshot.cell,), views=((snapshot.cell, snapshot.view),)):
        if callable(bind_operation):
            bind_operation(operation)
        library = operation.require_project_library_target(client, snapshot.library)
        info = client.library.get(snapshot.library, timeout=30)
        if str(info.technology_library or "") != snapshot.technology_library:
            raise ValueError("native OA snapshot technology drift")
        with operation.mutation_scope(snapshot.library, cells=(snapshot.cell,), views=((snapshot.cell, snapshot.view),),
                                      phase="materialize native OA source"):
            if cell_view_exists(client, snapshot.library, snapshot.cell, snapshot.view):
                delete_cell_view(client, snapshot.library, snapshot.cell, snapshot.view, operation=operation)
            operation.require_active_mutation(client, snapshot.library, snapshot.cell, phase="write native OA source")
            for name, content in snapshot.files.items():
                write_immutable_bytes(library / snapshot.cell / snapshot.view / name, content)
            if not cell_view_exists(client, snapshot.library, snapshot.cell, snapshot.view):
                raise RuntimeError("imported native OA source is not a recognized cell view")
            attest_native_snapshot(snapshot, client, operation)
