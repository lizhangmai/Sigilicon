"""Unforgeable-at-the-API-boundary workspace operation capabilities."""

from __future__ import annotations

import weakref
from enum import Enum
from typing import Any, Callable, TypeVar


_CAPABILITY_SECRET = object()
_T = TypeVar("_T")


class WorkspaceAuthority(str, Enum):
    """Actions granted by a workspace operation policy."""

    READ = "read"
    OA_MUTATION = "oa-mutation"
    RECURSIVE_OA = "recursive-oa"
    MAESTRO_SESSION = "maestro-session"
    PROCESS_ENV_MUTATION = "process-env-mutation"
    GUI = "gui"


_POLICY_AUTHORITIES = {
    "read-only": frozenset({WorkspaceAuthority.READ}),
    "direct-mutation": frozenset(
        {WorkspaceAuthority.READ, WorkspaceAuthority.OA_MUTATION}
    ),
    "recursive-oa": frozenset(
        {
            WorkspaceAuthority.READ,
            WorkspaceAuthority.OA_MUTATION,
            WorkspaceAuthority.RECURSIVE_OA,
            WorkspaceAuthority.PROCESS_ENV_MUTATION,
        }
    ),
    "maestro-run": frozenset(
        {WorkspaceAuthority.READ, WorkspaceAuthority.MAESTRO_SESSION}
    ),
    "gui-action": frozenset({WorkspaceAuthority.GUI}),
}


class _WorkspaceCapability:
    def __init__(self, secret: object, client: Any) -> None:
        if secret is not _CAPABILITY_SECRET:
            raise RuntimeError("workspace capabilities may only be issued by the boundary")
        self._secret = secret
        self._client = client
        self._owner: weakref.ReferenceType[Any] | None = None
        self._active = True

    def bind(self, operation: Any) -> None:
        if self._owner is not None:
            raise RuntimeError("workspace capability is already bound")
        self._owner = weakref.ref(operation)

    def revoke(self) -> None:
        self._active = False

    def require(self, operation: Any, client: Any) -> None:
        owner = self._owner() if self._owner is not None else None
        if (
            self._secret is not _CAPABILITY_SECRET
            or not self._active
            or owner is not operation
        ):
            raise RuntimeError("workspace operation capability is inactive or invalid")
        if self._client is not client:
            raise RuntimeError("workspace operation belongs to a different bridge client")


def _issue_workspace_capability(client: Any) -> _WorkspaceCapability:
    return _WorkspaceCapability(_CAPABILITY_SECRET, client)


def _bind_workspace_capability(
    capability: _WorkspaceCapability,
    operation: Any,
) -> None:
    capability.bind(operation)


def _revoke_workspace_capability(capability: _WorkspaceCapability) -> None:
    capability.revoke()


def require_workspace_capability(
    operation: Any,
    client: Any,
    *,
    authority: WorkspaceAuthority = WorkspaceAuthority.READ,
    library: str | None = None,
    cell: str | None = None,
    view: str | None = None,
    library_wide: bool = False,
) -> None:
    """Reject forged, expired, overprivileged, or insufficiently leased actions."""

    capability = getattr(operation, "_workspace_capability", None)
    if not isinstance(capability, _WorkspaceCapability):
        raise RuntimeError(
            "operation was not issued by the workspace safety boundary"
        )
    capability.require(operation, client)
    policy = getattr(operation, "policy", None)
    policy_value = getattr(policy, "value", policy)
    allowed = _POLICY_AUTHORITIES.get(policy_value)
    if allowed is None or authority not in allowed:
        raise RuntimeError(
            f"workspace policy {policy_value!r} does not grant {authority.value} authority"
        )
    if library is None:
        if cell is not None or view is not None or library_wide:
            raise ValueError("lease target requires a library")
        return
    if view is not None and cell is None:
        raise ValueError("an exact view target requires a cell")
    if cell is not None and library_wide:
        raise ValueError("cell and library-wide lease requirements are exclusive")
    checker = (
        operation.has_active_library_view_lease
        if library_wide
        else lambda target: operation.has_active_view_lease(
            target, cell=cell, view=view
        )
    )
    if not checker(library):
        scope = "library-wide" if library_wide else "project"
        raise RuntimeError(
            f"operation requires an active {scope} view lease for {library}"
        )


def require_oa_mutation_capability(
    operation: Any,
    client: Any,
    *,
    library: str,
    cell: str | None,
    phase: str,
) -> Any:
    """Require and immediately revalidate the exact target mutation permit."""

    require_workspace_capability(
        operation,
        client,
        authority=WorkspaceAuthority.OA_MUTATION,
    )
    checker = getattr(operation, "require_active_mutation", None)
    if checker is None:
        raise RuntimeError("workspace operation has no OA mutation-scope authority")
    return checker(client, library, cell, phase=phase)


def require_oa_target_capability(
    operation: Any,
    client: Any,
    *,
    library: str,
    cell: str | None,
    phase: str,
) -> Any:
    """Check exact target authority without claiming that a write was dispatched."""

    require_workspace_capability(
        operation,
        client,
        authority=WorkspaceAuthority.OA_MUTATION,
    )
    checker = getattr(operation, "require_mutation_target", None)
    if checker is None:
        raise RuntimeError("workspace operation has no OA mutation-scope authority")
    return checker(client, library, cell, phase=phase)


def dispatch_oa_mutation(
    operation: Any,
    client: Any,
    *,
    library: str,
    cell: str | None,
    phase: str,
    callback: Callable[[], _T],
) -> _T:
    """Revalidate one exact permit immediately before one write dispatch."""

    require_oa_mutation_capability(
        operation,
        client,
        library=library,
        cell=cell,
        phase=phase,
    )
    return callback()
