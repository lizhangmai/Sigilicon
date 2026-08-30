"""Compile project domain catalogs into ordinary typed Flow specifications."""

from __future__ import annotations

from sigilicon.flow.layout import (
    LAYOUT_GENERATION_ACTION,
    LAYOUT_VERIFICATION_ACTION,
)
from sigilicon.flow.model import (
    DesignCatalogExpansion,
    FlowContractError,
    FlowNode,
    FlowSpec,
    FlowTarget,
    LayoutCatalogExpansion,
)
from sigilicon.workflows.design_targets import DesignTargetCatalog
from sigilicon.workflows.layout_targets import LayoutTargetCatalog


def _compiled_spec(
    source: FlowSpec,
    nodes: list[FlowNode],
    targets: list[FlowTarget],
) -> FlowSpec:
    if not nodes or not targets:
        raise FlowContractError(
            f"catalog expansion produced no routes for Flow {source.flow_id!r}"
        )
    return FlowSpec(
        owner=source.owner,
        flow_id=source.flow_id,
        nodes=tuple(nodes),
        targets=tuple(targets),
        policies=source.policies,
        owner_root=source.owner_root,
    )


def compile_design_catalog_flow(
    source: FlowSpec,
    catalog: DesignTargetCatalog,
) -> FlowSpec:
    """Compile design routes selected by one expanded Flow contract."""

    expansion = source.catalog_expansion
    if not isinstance(expansion, DesignCatalogExpansion):
        raise FlowContractError("Flow does not declare a design catalog expansion")
    source.policy(expansion.policy)
    nodes: list[FlowNode] = []
    targets: list[FlowTarget] = []
    for target in catalog.targets:
        if target.owner != source.owner:
            continue
        for mode in target.modes:
            if mode.flow != source.flow_id:
                continue
            if mode.action_kind is None or mode.evidence_level is None:
                raise FlowContractError(
                    f"design route {target.name}.{mode.name} needs Action and "
                    "evidence metadata for catalog expansion"
                )
            node_id = mode.target
            nodes.append(
                FlowNode(
                    node_id=node_id,
                    action_kind=mode.action_kind,
                    config={
                        "target": target.name,
                        "mode": mode.name,
                        "evidence_role": (
                            mode.evidence_role or expansion.evidence_role
                        ),
                        "evidence_level": mode.evidence_level,
                        "evidence_scope": mode.evidence_scope or node_id,
                    },
                    policy=expansion.policy,
                )
            )
            targets.append(FlowTarget(node_id, (node_id,)))
    return _compiled_spec(source, nodes, targets)


def compile_layout_catalog_flow(
    source: FlowSpec,
    catalog: LayoutTargetCatalog,
) -> FlowSpec:
    """Compile layout routes selected by one expanded Flow contract."""

    expansion = source.catalog_expansion
    if not isinstance(expansion, LayoutCatalogExpansion):
        raise FlowContractError("Flow does not declare a layout catalog expansion")
    source.policy(expansion.generation_policy)
    source.policy(expansion.verification_policy)
    nodes: list[FlowNode] = []
    targets: list[FlowTarget] = []
    for target in catalog.targets:
        if target.owner != source.owner:
            continue
        routes = {route.operation: route for route in target.routes}
        for route in target.routes:
            if route.flow != source.flow_id:
                continue
            if route.operation == "generate":
                nodes.append(
                    FlowNode(
                        node_id=route.target,
                        action_kind=LAYOUT_GENERATION_ACTION,
                        config={
                            "target": target.name,
                            "operation": route.operation,
                        },
                        policy=expansion.generation_policy,
                    )
                )
                goals = (route.target,)
            elif route.operation in {"verify-drc", "verify-lvs"}:
                check = route.operation.removeprefix("verify-")
                nodes.append(
                    FlowNode(
                        node_id=route.target,
                        action_kind=LAYOUT_VERIFICATION_ACTION,
                        config={
                            "target": target.name,
                            "operation": route.operation,
                            "check": check,
                            "evidence_role": expansion.evidence_role,
                            "evidence_level": expansion.evidence_level,
                            "evidence_scope": (
                                f"{target.name}-physical-verification"
                            ),
                        },
                        policy=expansion.verification_policy,
                    )
                )
                goals = (route.target,)
            elif route.operation == "verify-all":
                try:
                    goals = (
                        routes["verify-drc"].target,
                        routes["verify-lvs"].target,
                    )
                except KeyError as exc:
                    raise FlowContractError(
                        f"layout target {target.name!r} verify-all route needs "
                        "verify-drc and verify-lvs routes"
                    ) from exc
            else:
                raise FlowContractError(
                    f"unsupported expanded layout operation: {route.operation!r}"
                )
            targets.append(FlowTarget(route.target, goals))
    return _compiled_spec(source, nodes, targets)


__all__ = ["compile_design_catalog_flow", "compile_layout_catalog_flow"]
