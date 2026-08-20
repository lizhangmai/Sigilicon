"""Optional legacy ADE receipt and load-wrapper helpers.

The source-driven OA assembly does not consume ADE component receipts.  This
module is kept only for the older AMS/ADE workflow and the historical
SystemVerilog lifecycle; current OA parity uses :mod:`sigilicon.virtuoso.provenance`
only for live view digests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from sigilicon.virtuoso.bridge import decode_skill_output

from sigilicon.virtuoso.capability import require_workspace_capability
from sigilicon.virtuoso.confirmation import require_bridge_confirmation
from sigilicon.virtuoso.oa import (
    audit_cellview_delta_skill,
    cell_view_exists,
    own_synchronous_cellview_delta_skill,
    skill_quote,
)
from sigilicon.virtuoso.provenance import oa_view_digest


ADE_COMPONENT_VIEWS = {
    "load": "schematic",
    "systemverilog": "systemVerilog",
    "config": "config",
    "maestro": "maestro",
}


def read_oa_load_instances(
    client: Any,
    library: str,
    wrapper_cell: str,
    *,
    operation: Any,
) -> tuple[dict[str, Any], ...]:
    """Read CLOAD connectivity/value while closing the exact opened OA handle."""

    require_workspace_capability(
        operation,
        client,
        library=library,
        cell=wrapper_cell,
        view="schematic",
    )
    source = f'''let((cv out prop value attempt)
  cv = nil
  out = ""
  attempt = errset(
    unwindProtect(
      progn(
        cv = dbOpenCellViewByType(
          {skill_quote(library)} {skill_quote(wrapper_cell)} "schematic" "" "r")
        unless(cv error("cannot open ADE load wrapper schematic"))
        foreach(inst cv~>instances
          when(rexMatchp("^CLOAD[0-9]+$" inst~>name)
            prop = dbFindProp(inst "c")
            unless(prop prop = dbFindProp(inst "value"))
            value = if(prop prop~>value nil)
            out = strcat(out sprintf(nil "%s|%s|%s|%L|"
              inst~>name inst~>libName inst~>cellName value))
            foreach(instTerm inst~>instTerms
              when(instTerm~>net
                out = strcat(out instTerm~>net~>name ",")))
            out = strcat(out "\\n")))
        out)
      when(cv unless(dbClose(cv) error("ADE load wrapper close failed")) cv = nil)
    )
    nil)
  unless(attempt && car(attempt) error("ADE load wrapper attestation failed"))
  car(attempt)
)'''
    result = require_bridge_confirmation(
        operation,
        f"attest ADE load wrapper {library}/{wrapper_cell}",
        lambda: client.execute_skill(
            audit_cellview_delta_skill(
                own_synchronous_cellview_delta_skill(
                    source,
                    label=f"ADE load attestation {library}/{wrapper_cell}",
                ),
                label=f"ADE load attestation {library}/{wrapper_cell}",
            ),
            timeout=60,
        ),
    )
    if result.errors:
        raise RuntimeError(result.errors[0])
    decoded = decode_skill_output(result.output or "")
    instances: list[dict[str, Any]] = []
    for line in decoded.splitlines():
        if not line.strip():
            continue
        fields = line.split("|", 4)
        if len(fields) != 5:
            raise RuntimeError(f"invalid ADE OA load attestation row: {line!r}")
        instance, device_library, device_cell, value, raw_nets = fields
        nets = tuple(net for net in raw_nets.split(",") if net)
        if not instance or not device_cell or not value or not nets:
            raise RuntimeError(f"incomplete ADE OA load attestation row: {line!r}")
        instances.append(
            {
                "instance": instance,
                "library": device_library,
                "cell": device_cell,
                "value": value.strip().strip('"'),
                "nets": nets,
            }
        )
    return tuple(instances)


def capture_ade_component(
    virtuoso_root: Path,
    library: str,
    cell: str,
    component: str,
) -> dict[str, str]:
    try:
        view = ADE_COMPONENT_VIEWS[component]
    except KeyError as exc:
        raise ValueError(f"unsupported ADE setup component: {component!r}") from exc
    view_dir = virtuoso_root / library / cell / view
    return {
        "library": library,
        "cell": cell,
        "view": view,
        "oa_sha256": oa_view_digest(
            view_dir,
            allowed_symlink_root=virtuoso_root.parent,
        ),
    }


def validate_ade_component_views(
    client: Any,
    virtuoso_root: Path,
    library: str,
    cell: str,
    wrapper_cell: str,
    components: Mapping[str, Mapping[str, Any]],
) -> None:
    """Ensure old ADE OA views still match their component receipts."""

    for component, expected_view in ADE_COMPONENT_VIEWS.items():
        receipt = components.get(component)
        expected_cell = wrapper_cell if component == "load" else cell
        if not isinstance(receipt, Mapping):
            raise RuntimeError(f"missing committed ADE {component} component")
        if (
            receipt.get("library") != library
            or receipt.get("cell") != expected_cell
            or receipt.get("view") != expected_view
        ):
            raise RuntimeError(f"ADE {component} receipt identifies the wrong OA view")
        if not cell_view_exists(client, library, expected_cell, expected_view):
            raise RuntimeError(
                f"committed ADE view is missing: {library}/{expected_cell}/{expected_view}"
            )
        actual = oa_view_digest(
            virtuoso_root / library / expected_cell / expected_view,
            allowed_symlink_root=virtuoso_root.parent,
        )
        if receipt.get("oa_sha256") != actual:
            raise RuntimeError(
                f"ADE {library}/{expected_cell}/{expected_view} no longer matches "
                "its commit manifest; rerun setup-ams-ade"
            )
