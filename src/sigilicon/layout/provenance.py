"""Scope-specific fingerprints for generated layout verification.

The :class:`~sigilicon.layout.ir.LayoutPlan` fingerprint is the exact OA physical
content and mutation guard.  The projections here additionally omit identity,
names and connectivity that cannot affect the selected DRC or LVS result.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from sigilicon.layout.ir import LayoutPlan


MasterKey = tuple[str, str, str]
VerificationScope = Literal["drc", "lvs"]


def _payload(plan: LayoutPlan | Mapping[str, Any]) -> Mapping[str, Any]:
    value = plan.payload() if isinstance(plan, LayoutPlan) else plan
    if not isinstance(value, Mapping):
        raise ValueError("layout provenance input must be a layout-plan mapping")
    return value


def _rows(value: object, label: str) -> tuple[Mapping[str, Any], ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"layout plan {label} must be an array")
    rows = tuple(value)
    if any(not isinstance(row, Mapping) for row in rows):
        raise ValueError(f"layout plan {label} entries must be mappings")
    return rows  # type: ignore[return-value]


def _canonical_sort(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    return sorted(
        rows,
        key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":")),
    )


def _alias(name: str, aliases: Mapping[str, str]) -> str:
    return aliases.get(name, name)


def _master_key(instance: Mapping[str, Any]) -> MasterKey:
    return (
        str(instance.get("library", "")),
        str(instance.get("cell", "")),
        str(instance.get("view", "")),
    )


def _instance_rows(
    plan: Mapping[str, Any],
    *,
    scope: VerificationScope,
    master_fingerprints: Mapping[MasterKey, str],
    cell_aliases: Mapping[str, str],
) -> list[dict[str, object]]:
    plan_library = str(plan.get("library", ""))
    result: list[dict[str, object]] = []
    for instance in _rows(plan.get("instances"), "instances"):
        parameters = tuple(
            sorted(
                tuple(str(item) for item in parameter)
                for parameter in instance.get("parameters", ())
            )
        )
        key = _master_key(instance)
        hierarchical = not parameters and key[0] == plan_library
        if hierarchical and key in master_fingerprints:
            master: dict[str, object] = {
                "generated_master_fingerprint": master_fingerprints[key]
            }
        elif hierarchical:
            master = {
                "generated_master": (
                    key[0],
                    _alias(key[1], cell_aliases),
                    key[2],
                )
            }
        else:
            master = {
                "physical_master": key,
                "parameters": tuple(sorted(parameters)),
            }
        row: dict[str, object] = {
            "master": master,
            "origin_dbu": instance.get("origin_dbu"),
            "transform": instance.get("transform"),
        }
        if scope == "lvs":
            row["terminals"] = tuple(
                sorted(
                    tuple(str(item) for item in terminal)
                    for terminal in instance.get("terminals", ())
                )
            )
        result.append(row)
    return _canonical_sort(result)


def layout_verification_payload(
    plan: LayoutPlan | Mapping[str, Any],
    *,
    scope: VerificationScope,
    master_fingerprints: Mapping[MasterKey, str] | None = None,
    cell_aliases: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Project a plan onto inputs that can affect one physical check.

    DRC excludes OA identity, generator metadata, instance/shape names, nets
    and pin directions.  LVS adds physical connectivity and the public pin
    contract.  Generated child names are replaced by their scoped child
    fingerprints when the caller supplies a hierarchy closure.
    """

    if scope not in {"drc", "lvs"}:
        raise ValueError(f"unsupported layout verification scope: {scope}")
    value = _payload(plan)
    masters = master_fingerprints or {}
    aliases = cell_aliases or {}
    rectangles = [
        {
            "layer": row.get("layer"),
            "purpose": row.get("purpose"),
            "bbox_dbu": row.get("bbox_dbu"),
            **({"net": row.get("net")} if scope == "lvs" else {}),
        }
        for row in _rows(value.get("rectangles"), "rectangles")
    ]
    pins = [
        {
            "layer": row.get("layer"),
            "purpose": row.get("purpose"),
            "bbox_dbu": row.get("bbox_dbu"),
            **(
                {"name": row.get("name"), "direction": row.get("direction")}
                if scope == "lvs"
                else {}
            ),
        }
        for row in _rows(value.get("pins"), "pins")
    ]
    vias = [
        {
            "via_definition": row.get("via_definition"),
            "origin_dbu": row.get("origin_dbu"),
            "transform": row.get("transform"),
            **({"net": row.get("net")} if scope == "lvs" else {}),
        }
        for row in _rows(value.get("vias"), "vias")
    ]
    return {
        "scope": scope,
        "dbu_per_micron": value.get("dbu_per_micron"),
        "instances": _instance_rows(
            value,
            scope=scope,
            master_fingerprints=masters,
            cell_aliases=aliases,
        ),
        "rectangles": _canonical_sort(rectangles),
        "pins": _canonical_sort(pins),
        "vias": _canonical_sort(vias),
    }


def layout_verification_fingerprint(
    plan: LayoutPlan | Mapping[str, Any],
    *,
    scope: VerificationScope,
    master_fingerprints: Mapping[MasterKey, str] | None = None,
    cell_aliases: Mapping[str, str] | None = None,
) -> str:
    payload = layout_verification_payload(
        plan,
        scope=scope,
        master_fingerprints=master_fingerprints,
        cell_aliases=cell_aliases,
    )
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def layout_hierarchy_fingerprints(
    plans: Sequence[LayoutPlan | Mapping[str, Any]],
    *,
    scope: VerificationScope,
    cell_aliases: Mapping[str, str] | None = None,
) -> dict[MasterKey, str]:
    """Compute leaf-to-top scoped fingerprints for a layout plan closure."""

    pending = {_master_key(_payload(plan)): plan for plan in plans}
    if len(pending) != len(plans):
        raise ValueError("layout fingerprint closure contains duplicate masters")
    result: dict[MasterKey, str] = {}
    while pending:
        ready: list[MasterKey] = []
        for key, plan in pending.items():
            value = _payload(plan)
            dependencies = {
                _master_key(instance)
                for instance in _rows(value.get("instances"), "instances")
                if not instance.get("parameters")
                and str(instance.get("library", "")) == str(value.get("library", ""))
                and _master_key(instance) in pending
            }
            if not dependencies:
                ready.append(key)
        if not ready:
            raise ValueError(
                "layout fingerprint closure is recursive or has unresolved internal order"
            )
        for key in sorted(ready):
            result[key] = layout_verification_fingerprint(
                pending.pop(key),
                scope=scope,
                master_fingerprints=result,
                cell_aliases=cell_aliases,
            )
    return result
