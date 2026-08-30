"""Immutable failure evidence for guarded non-disposable operations.

Disposable OA rebuild/check operations do not call this journal.  It remains
for standalone Spectre/AMS workflows that need a durable safety incident when
process cleanup or ownership becomes uncertain.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import stat
from typing import Any, Mapping, Sequence

from sigilicon.artifacts import atomic_write_json
from sigilicon.paths import ArtifactLayout


def write_operation_incident(
    *,
    workspace_root: Path,
    artifact_root: Path,
    operation_id: str,
    name: str,
    policy: str,
    status: str,
    error: BaseException,
    uncertain_reason: str | None,
    view_snapshots: Sequence[Mapping[str, Any]],
    ownership_scopes: Sequence[Mapping[str, Any]],
) -> Path:
    """Create one UUID-scoped incident without replacing prior evidence."""

    incident_paths = ArtifactLayout(artifact_root.resolve()).system_operation(
        operation_id
    )
    incident_paths.create()
    incident_path = incident_paths.incident
    payload = {
        "schema": 1,
        "contract_kind": "workspace-operation-incident",
        "operation_id": operation_id,
        "name": name,
        "policy": policy,
        "status": status,
        "workspace_root": str(workspace_root),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "error_type": type(error).__name__,
        "error": str(error)[:4000],
        "uncertain_reason": uncertain_reason,
        "view_snapshots": list(view_snapshots),
        "ownership_scopes": list(ownership_scopes),
    }
    atomic_write_json(incident_path, payload)
    return incident_path


def rollback_unreferenced_operation_incident(
    *,
    artifact_root: Path,
    operation_id: str,
    incident_path: Path,
) -> None:
    """Remove only the exact lexical journal target through nofollow dirfds."""

    root = Path(os.path.abspath(artifact_root))
    incident_paths = ArtifactLayout(root).system_operation(operation_id)
    identity = incident_paths.operation_id
    expected = Path(os.path.abspath(incident_paths.incident))
    if Path(os.path.abspath(incident_path)) != expected:
        raise RuntimeError("refusing to roll back a non-canonical operation incident")
    relative_operation = incident_paths.root.relative_to(root)
    parent_components = relative_operation.parts[:-1]
    operation_component = relative_operation.parts[-1]
    incident_name = incident_paths.incident.name
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptors: list[int] = []
    descriptor = os.open("/", flags)
    descriptors.append(descriptor)
    try:
        for component in root.parts[1:]:
            descriptor = os.open(component, flags, dir_fd=descriptor)
            descriptors.append(descriptor)
        for component in parent_components:
            descriptor = os.open(component, flags, dir_fd=descriptor)
            descriptors.append(descriptor)
        operations_fd = descriptor
        operation_fd = os.open(operation_component, flags, dir_fd=operations_fd)
        descriptors.append(operation_fd)
        metadata = os.stat(
            incident_name,
            dir_fd=operation_fd,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise RuntimeError("operation incident rollback target is not an owned file")
        os.unlink(incident_name, dir_fd=operation_fd)
        os.rmdir(operation_component, dir_fd=operations_fd)
    except OSError as exc:
        raise RuntimeError(
            "could not safely roll back unreferenced operation incident"
        ) from exc
    finally:
        for opened in reversed(descriptors):
            os.close(opened)
