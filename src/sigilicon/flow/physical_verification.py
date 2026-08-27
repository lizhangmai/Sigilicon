"""Tool-independent Flow contracts for physical DRC and LVS evidence."""

from __future__ import annotations

from sigilicon.flow.model import ActionContract, ArtifactPort
from sigilicon.flow.registry import FlowRegistry


MATERIALIZED_LAYOUT_KIND = "layout.materialized"
CANONICAL_SOURCE_NETLIST_KIND = "netlist.canonical-source"
DRC_EVIDENCE_KIND = "evidence.drc"
LVS_EVIDENCE_KIND = "evidence.lvs"
DRC_ACTION = "physical-verification.drc"
LVS_ACTION = "physical-verification.lvs"
OFFLINE_PHYSICAL_VERIFICATION_ADAPTER = "offline-physical-verification"


def register_physical_verification_actions(registry: FlowRegistry) -> None:
    registry.register_action(
        ActionContract(
            kind=DRC_ACTION,
            inputs=(ArtifactPort("layout", MATERIALIZED_LAYOUT_KIND),),
            outputs=(ArtifactPort("evidence", DRC_EVIDENCE_KIND),),
            facts=("drc-status", "drc-clean", "drc-completed"),
            adapters=(OFFLINE_PHYSICAL_VERIFICATION_ADAPTER,),
            adapter_extensible=True,
        )
    )
    registry.register_action(
        ActionContract(
            kind=LVS_ACTION,
            inputs=(
                ArtifactPort("layout", MATERIALIZED_LAYOUT_KIND),
                ArtifactPort("source", CANONICAL_SOURCE_NETLIST_KIND),
            ),
            outputs=(ArtifactPort("evidence", LVS_EVIDENCE_KIND),),
            facts=("lvs-status", "lvs-clean", "lvs-completed"),
            adapters=(OFFLINE_PHYSICAL_VERIFICATION_ADAPTER,),
            adapter_extensible=True,
        )
    )


__all__ = [
    "CANONICAL_SOURCE_NETLIST_KIND",
    "DRC_ACTION",
    "DRC_EVIDENCE_KIND",
    "LVS_ACTION",
    "LVS_EVIDENCE_KIND",
    "MATERIALIZED_LAYOUT_KIND",
    "OFFLINE_PHYSICAL_VERIFICATION_ADAPTER",
    "register_physical_verification_actions",
]
