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
from sigilicon.paths import ProjectContext
from sigilicon.paths import validate_artifact_id


def write_operation_incident(
    *,
    workspace_root: Path,
    artifact_root: Path | None,
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

    paths = ProjectContext.from_project_root(
        workspace_root.parent,
        artifact_root=artifact_root,
    )
    incident_paths = paths.artifacts.operation_incident(operation_id)
    incident_paths.create()
    incident_path = incident_paths.incident
    payload = {
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

    identity = validate_artifact_id(operation_id, "operation id")
    root = Path(os.path.abspath(artifact_root))
    expected = root / "system" / "operations" / identity / "incident.json"
    if Path(os.path.abspath(incident_path)) != expected:
        raise RuntimeError("refusing to roll back a non-canonical operation incident")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptors: list[int] = []
    descriptor = os.open("/", flags)
    descriptors.append(descriptor)
    try:
        for component in root.parts[1:]:
            descriptor = os.open(component, flags, dir_fd=descriptor)
            descriptors.append(descriptor)
        for component in ("system", "operations"):
            descriptor = os.open(component, flags, dir_fd=descriptor)
            descriptors.append(descriptor)
        operations_fd = descriptor
        operation_fd = os.open(identity, flags, dir_fd=operations_fd)
        descriptors.append(operation_fd)
        metadata = os.stat(
            "incident.json",
            dir_fd=operation_fd,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise RuntimeError("operation incident rollback target is not an owned file")
        os.unlink("incident.json", dir_fd=operation_fd)
        os.rmdir(identity, dir_fd=operations_fd)
    except OSError as exc:
        raise RuntimeError(
            "could not safely roll back unreferenced operation incident"
        ) from exc
    finally:
        for opened in reversed(descriptors):
            os.close(opened)
