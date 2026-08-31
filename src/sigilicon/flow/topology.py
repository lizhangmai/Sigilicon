"""Pure target-closure resolution for compiled Flow specifications.

Topology is structural: artifact bindings and explicit ordering edges are enough
to determine which nodes belong to a target.  Operation recipes do not own
targets; the repository target catalog compiles one target into a FlowSpec
before this module is called.  Action and Adapter semantics are validated only
after this closure has been selected.
"""

from __future__ import annotations

from dataclasses import dataclass

from sigilicon.flow.model import FlowContractError, FlowSpec


@dataclass(frozen=True)
class TargetTopology:
    """The selected nodes and structural dependencies for one Flow target."""

    nodes: tuple[str, ...]
    dependencies: dict[str, tuple[str, ...]]


def resolve_target_topology(spec: FlowSpec, target_id: str) -> TargetTopology:
    """Resolve one target without importing or instantiating tool Adapters."""

    target = spec.target(target_id)
    node_order = {node.node_id: index for index, node in enumerate(spec.nodes)}
    structural_dependencies = {
        node.node_id: tuple(
            dict.fromkeys(
                (
                    *(binding.producer for binding in node.bindings),
                    *node.order_after,
                )
            )
        )
        for node in spec.nodes
    }

    selected: set[str] = set()
    pending = list(target.goals)
    while pending:
        node_id = pending.pop()
        spec.node(node_id)
        if node_id in selected:
            continue
        selected.add(node_id)
        pending.extend(structural_dependencies[node_id])

    remaining = {
        node_id: {
            dependency
            for dependency in structural_dependencies[node_id]
            if dependency in selected
        }
        for node_id in selected
    }
    ready = sorted(
        (node_id for node_id, dependencies in remaining.items() if not dependencies),
        key=node_order.__getitem__,
    )
    topology: list[str] = []
    scheduled: set[str] = set()
    while ready:
        node_id = ready.pop(0)
        topology.append(node_id)
        scheduled.add(node_id)
        for candidate in sorted(selected - scheduled, key=node_order.__getitem__):
            if node_id in remaining[candidate]:
                remaining[candidate].remove(node_id)
                if not remaining[candidate] and candidate not in ready:
                    ready.append(candidate)
        ready.sort(key=node_order.__getitem__)
    if len(topology) != len(selected):
        cyclic = sorted(selected - scheduled, key=node_order.__getitem__)
        raise FlowContractError(f"Flow contains a dependency cycle: {cyclic}")

    topology_tuple = tuple(topology)
    return TargetTopology(
        nodes=topology_tuple,
        dependencies={
            node_id: structural_dependencies[node_id]
            for node_id in topology_tuple
        },
    )


__all__ = ["TargetTopology", "resolve_target_topology"]
