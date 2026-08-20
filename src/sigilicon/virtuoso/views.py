"""Narrow GUI view operations."""

from __future__ import annotations

from typing import Any

from sigilicon.virtuoso.capability import WorkspaceAuthority, require_workspace_capability
from sigilicon.virtuoso.confirmation import require_bridge_confirmation


def open_cell_window(
    client: Any,
    library: str,
    cell: str,
    view: str,
    *,
    operation: Any,
) -> None:
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
