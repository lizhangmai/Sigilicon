"""Stable fingerprints for canonical design and generated AMS state."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from sigilicon.domain.design import DesignSpec
from sigilicon.domain.netlist import (
    NetlistHierarchy,
    NetlistInstance,
    parse_subcircuit_instances,
)


def digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def design_identity_fingerprint(design: DesignSpec) -> str:
    """Bind one generated OA identity to its exact canonical source contract.

    Library and cell names deliberately belong here: this fingerprint protects
    an exact OA object.  It must not be used as a proxy for electrical or
    physical equivalence.
    """

    return digest(
        {
            "library": design.library,
            "cell": design.cell,
            "source_sha256": design.netlist_snapshot.sha256,
            "ports": design.port_order,
            "inputs": design.inputs,
            "outputs": design.outputs,
            "inouts": design.inouts,
            "directions": dict(design.directions),
            "technology_library": design.pdk.technology_library,
            "reference_libraries": design.pdk.reference_libraries,
        }
    )


def design_fingerprint(design: DesignSpec) -> str:
    """Backward-compatible name for the exact OA identity fingerprint."""

    return design_identity_fingerprint(design)


@dataclass(frozen=True)
class _ElectricalDefinition:
    ports: tuple[str, ...]
    parameters: tuple[str, ...]
    instances: tuple[NetlistInstance, ...]


def _electrical_hierarchy_fingerprint(
    definitions: Mapping[str, _ElectricalDefinition],
    *,
    top: str,
    primitive_masters: Sequence[str],
) -> str:
    """Hash electrical structure while excluding cell and instance identity."""

    if top not in definitions:
        raise ValueError(f"electrical fingerprint top is undefined: {top}")
    primitives = frozenset(primitive_masters)
    memo: dict[str, str] = {}
    active: list[str] = []

    def visit(cell: str) -> str:
        if cell in memo:
            return memo[cell]
        if cell in active:
            cycle = " -> ".join((*active[active.index(cell) :], cell))
            raise ValueError(f"recursive electrical fingerprint hierarchy: {cycle}")
        active.append(cell)
        definition = definitions[cell]
        rows: list[dict[str, object]] = []
        for instance in definition.instances:
            if instance.master in definitions and instance.master not in primitives:
                master: dict[str, str] = {"subcircuit": visit(instance.master)}
            else:
                master = {"primitive": instance.master}
            rows.append(
                {
                    "nodes": instance.nodes,
                    "master": master,
                    "parameters": instance.parameters,
                }
            )
        rows.sort(key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":")))
        result = digest(
            {
                "ports": definition.ports,
                "parameters": definition.parameters,
                "instances": rows,
            }
        )
        active.pop()
        memo[cell] = result
        return result

    return visit(top)


def netlist_electrical_fingerprint(
    hierarchy: NetlistHierarchy,
    *,
    primitive_masters: Sequence[str] = (),
) -> str:
    """Fingerprint resolved electrical semantics without OA naming identity."""

    definitions = {
        name: _ElectricalDefinition(
            ports=definition.ports,
            parameters=tuple(
                (
                    *definition.parameters,
                    *(
                        token
                        for statement in definition.statements
                        if statement.lower().startswith("parameters ")
                        for token in statement.split()[1:]
                    ),
                )
            ),
            instances=parse_subcircuit_instances(definition),
        )
        for name, definition in hierarchy.definitions.items()
    }
    return _electrical_hierarchy_fingerprint(
        definitions,
        top=hierarchy.top,
        primitive_masters=primitive_masters,
    )


_CDL_SUBCKT = re.compile(r"^\.SUBCKT\s+(?P<name>\S+)(?:\s+(?P<body>.*))?$", re.I)
_CDL_ENDS = re.compile(r"^\.ENDS(?:\s+(?P<name>\S+))?$", re.I)


def canonical_cdl_electrical_fingerprint(
    text: str,
    *,
    top: str,
    primitive_masters: Sequence[str] = (),
) -> str:
    """Fingerprint CDL emitted by :func:`render_canonical_cdl` semantically.

    The parser is intentionally limited to the mechanically rendered CDL
    subset.  Cell and instance names are excluded; ports, connectivity,
    primitive masters and parameters remain significant.
    """

    definitions: dict[str, _ElectricalDefinition] = {}
    active_name: str | None = None
    active_ports: tuple[str, ...] = ()
    active_parameters: tuple[str, ...] = ()
    active_instances: list[NetlistInstance] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("*"):
            continue
        if active_name is None:
            match = _CDL_SUBCKT.fullmatch(line)
            if match is None:
                raise ValueError(f"unsupported canonical CDL line: {line}")
            active_name = match.group("name")
            tokens = (match.group("body") or "").split()
            try:
                parameter_index = next(
                    index for index, token in enumerate(tokens) if token.upper() == "PARAMS:"
                )
            except StopIteration:
                parameter_index = len(tokens)
            active_ports = tuple(tokens[:parameter_index])
            active_parameters = tuple(tokens[parameter_index + 1 :])
            active_instances = []
            continue
        end = _CDL_ENDS.fullmatch(line)
        if end is not None:
            if end.group("name") not in {None, active_name}:
                raise ValueError(
                    f"canonical CDL .ENDS {end.group('name')} does not match {active_name}"
                )
            if active_name in definitions:
                raise ValueError(f"duplicate canonical CDL subcircuit: {active_name}")
            definitions[active_name] = _ElectricalDefinition(
                ports=active_ports,
                parameters=active_parameters,
                instances=tuple(active_instances),
            )
            active_name = None
            continue
        tokens = line.split()
        parameter_index = next(
            (index for index, token in enumerate(tokens) if "=" in token),
            len(tokens),
        )
        if parameter_index < 3:
            raise ValueError(f"malformed canonical CDL instance: {line}")
        master_index = parameter_index - 1
        active_instances.append(
            NetlistInstance(
                name=tokens[0],
                nodes=tuple(tokens[1:master_index]),
                master=tokens[master_index],
                parameters=tuple(tokens[parameter_index:]),
            )
        )
    if active_name is not None:
        raise ValueError(f"unterminated canonical CDL subcircuit: {active_name}")
    return _electrical_hierarchy_fingerprint(
        definitions,
        top=top,
        primitive_masters=primitive_masters,
    )
