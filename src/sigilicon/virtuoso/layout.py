"""Read-only layout bridge adapter with deterministic handle cleanup."""

from __future__ import annotations

from typing import Any

from sigilicon.virtuoso.bridge import decode_skill_output
from sigilicon.virtuoso.bridge import parse_layout_geometry_output
from sigilicon.virtuoso.bridge import layout_read_geometry

from sigilicon.virtuoso.capability import require_workspace_capability
from sigilicon.virtuoso.confirmation import require_bridge_confirmation
from sigilicon.virtuoso.oa import (
    audit_cellview_delta_skill,
    own_synchronous_cellview_delta_skill,
)


def _protected_layout_skill(library: str, cell: str, view: str) -> str:
    source = layout_read_geometry(library, cell, view=view)
    prefix = "prog((cv out) "
    suffix = "return(out))"
    if not source.startswith(prefix) or not source.endswith(suffix):
        raise RuntimeError("unsupported virtuoso-bridge layout reader template")
    body = source[len(prefix) : -len(suffix)]
    return (
        "prog((cv out result) "
        "cv = nil "
        "unwindProtect(progn("
        f"{body} result = out) "
        'when(cv unless(dbClose(cv) error("layout read close failed")) cv = nil)) '
        "return(result))"
    )


def read_layout_geometry(
    client: Any,
    library: str,
    cell: str,
    view: str,
    *,
    operation: Any,
) -> list[dict[str, Any]]:
    require_workspace_capability(
        operation,
        client,
        library=library,
        cell=cell,
        view=view,
    )
    result = require_bridge_confirmation(
        operation,
        f"read layout {library}/{cell}/{view}",
        lambda: client.execute_skill(
            audit_cellview_delta_skill(
                own_synchronous_cellview_delta_skill(
                    _protected_layout_skill(library, cell, view),
                    label=f"layout read {library}/{cell}/{view}",
                ),
                label=f"layout read {library}/{cell}/{view}",
            ),
            timeout=60,
        ),
    )
    if result.errors:
        raise RuntimeError(result.errors[0])
    output = decode_skill_output(result.output or "")
    if output.lstrip().startswith(("ERROR", "Error", "*Error*")):
        raise RuntimeError(output)
    return result.metadata.get("geometry") or parse_layout_geometry_output(
        result.output or ""
    )
