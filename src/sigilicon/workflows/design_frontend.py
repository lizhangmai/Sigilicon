"""Normalize owner-authored circuit source into front-end design artifacts."""

from __future__ import annotations

import hashlib

from sigilicon.domain.circuit_design import (
    ARTIFACT_SCHEMA,
    CIRCUIT_TOPOLOGY_KIND,
    ArtifactMetadata,
    CircuitInstance,
    CircuitParameter,
    CircuitPort,
    CircuitTopologyProposal,
    PortDirection,
    ProposalProvenance,
    StateSemantic,
    TopologyOrigin,
)
from sigilicon.domain.config_contracts import read_toml, require_config_header
from sigilicon.domain.design import DesignSpec
from sigilicon.domain.netlist import (
    parse_subcircuit_definitions,
    parse_subcircuit_instances,
)


_DIRECTIONS = {
    "input": PortDirection.INPUT,
    "output": PortDirection.OUTPUT,
    "inputOutput": PortDirection.INPUT_OUTPUT,
}


def _parameter(token: str, label: str) -> CircuitParameter:
    name, separator, expression = token.partition("=")
    if not separator or not name or not expression:
        raise ValueError(f"{label} must be one name=value token")
    return CircuitParameter(name, expression)


class SourceAuthoredTopologyAdapter:
    """Read one validated DesignSpec and preserve its exact source topology."""

    def read(
        self,
        design: DesignSpec,
        *,
        owner: str,
        instance_roles: tuple[tuple[str, str], ...] = (),
        states: tuple[StateSemantic, ...] = (),
    ) -> CircuitTopologyProposal:
        raw = read_toml(design.path)
        require_config_header(
            raw,
            design.path,
            contract_kind="cell-design",
            path_scope="cell",
            owner=owner,
        )
        role_names = tuple(name for name, _role in instance_roles)
        if len(role_names) != len(set(role_names)):
            raise ValueError("duplicate source-authored instance role")
        roles: dict[str, str] = {}
        for name, role in instance_roles:
            roles[name] = role

        definitions = parse_subcircuit_definitions((design.netlist_snapshot,))
        try:
            definition = definitions[design.cell]
        except KeyError as exc:
            raise ValueError(
                f"source-authored topology has no subckt {design.cell!r}"
            ) from exc
        parsed_instances = parse_subcircuit_instances(definition)
        unknown_roles = sorted(set(roles) - {item.name for item in parsed_instances})
        if unknown_roles:
            raise ValueError(
                f"unknown topology instance roles: {unknown_roles}"
            )

        ports: list[CircuitPort] = []
        for name in design.port_order:
            if name == design.primary_supply:
                role = "primary-supply"
            elif name == design.ground_supply:
                role = "ground-supply"
            elif name in design.supplies:
                role = "supply"
            else:
                role = "signal"
            ports.append(CircuitPort(name, _DIRECTIONS[design.directions[name]], role))

        instances = tuple(
            CircuitInstance(
                item.name,
                item.master,
                item.nodes,
                tuple(
                    _parameter(token, f"instance {item.name} parameter")
                    for token in item.parameters
                ),
                roles.get(item.name),
            )
            for item in parsed_instances
        )
        parameter_tokens = list(definition.parameters)
        for statement in definition.statements:
            if statement.lower().startswith("parameters "):
                parameter_tokens.extend(statement.split()[1:])
        parameters = tuple(
            _parameter(token, "subckt parameter") for token in parameter_tokens
        )
        source_sha256 = hashlib.sha256(
            design.netlist_snapshot.text.encode("utf-8")
        ).hexdigest()
        return CircuitTopologyProposal(
            ArtifactMetadata(ARTIFACT_SCHEMA, CIRCUIT_TOPOLOGY_KIND, owner),
            design.cell,
            source_sha256,
            TopologyOrigin.SOURCE_AUTHORED,
            tuple(ports),
            parameters,
            instances,
            states,
            ProposalProvenance("source-authored-baseline", "1"),
        )


__all__ = ["SourceAuthoredTopologyAdapter"]
