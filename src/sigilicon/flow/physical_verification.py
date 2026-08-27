"""Tool-independent Flow contracts for physical DRC and LVS evidence."""

from __future__ import annotations

from sigilicon.flow.model import ActionContract, ArtifactPort, PlatformAssetRequirement
from sigilicon.flow.physical_design import (
    MATERIALIZED_GDS_KIND,
    MATERIALIZATION_RECEIPT_KIND,
)
from sigilicon.flow.registry import FlowRegistry


MATERIALIZED_LAYOUT_KIND = MATERIALIZED_GDS_KIND
CANONICAL_SOURCE_NETLIST_KIND = "netlist.canonical-source"
PHYSICAL_VERIFICATION_POLICY_KIND = "policy.physical-verification"
PHYSICAL_VERIFICATION_SOURCE_ACTION = "physical-verification.source-inputs"
RECEIPT_BOUND_VERIFICATION_SOURCE_ADAPTER = "receipt-bound-verification-source"
DRC_EVIDENCE_KIND = "evidence.drc"
LVS_EVIDENCE_KIND = "evidence.lvs"
DRC_ACTION = "physical-verification.drc"
LVS_ACTION = "physical-verification.lvs"
OFFLINE_PHYSICAL_VERIFICATION_ADAPTER = "offline-physical-verification"
CALIBRE_PHYSICAL_VERIFICATION_ADAPTER = "calibre-physical-verification"


def _verification_asset(*members: str) -> tuple[PlatformAssetRequirement, ...]:
    return (
        PlatformAssetRequirement(
            "physical-verification",
            "platform.calibre-verification",
            members=members,
        ),
    )


def register_physical_verification_actions(registry: FlowRegistry) -> None:
    registry.register_action(
        ActionContract(
            kind=PHYSICAL_VERIFICATION_SOURCE_ACTION,
            outputs=(
                ArtifactPort("source", CANONICAL_SOURCE_NETLIST_KIND),
                ArtifactPort(
                    "verification-policy",
                    PHYSICAL_VERIFICATION_POLICY_KIND,
                ),
            ),
            adapters=(RECEIPT_BOUND_VERIFICATION_SOURCE_ADAPTER,),
            resolves_source_assets=True,
        )
    )
    registry.register_action(
        ActionContract(
            kind=DRC_ACTION,
            inputs=(
                ArtifactPort("layout", MATERIALIZED_LAYOUT_KIND),
                ArtifactPort("receipt", MATERIALIZATION_RECEIPT_KIND),
                ArtifactPort("verification-policy", PHYSICAL_VERIFICATION_POLICY_KIND),
            ),
            outputs=(ArtifactPort("evidence", DRC_EVIDENCE_KIND),),
            facts=("drc-status", "drc-clean", "drc-completed"),
            required_capabilities=("tool.calibre",),
            platform_assets=_verification_asset("drc-deck"),
            adapters=(
                CALIBRE_PHYSICAL_VERIFICATION_ADAPTER,
                OFFLINE_PHYSICAL_VERIFICATION_ADAPTER,
            ),
            adapter_extensible=True,
        )
    )
    registry.register_action(
        ActionContract(
            kind=LVS_ACTION,
            inputs=(
                ArtifactPort("layout", MATERIALIZED_LAYOUT_KIND),
                ArtifactPort("receipt", MATERIALIZATION_RECEIPT_KIND),
                ArtifactPort("source", CANONICAL_SOURCE_NETLIST_KIND),
                ArtifactPort("verification-policy", PHYSICAL_VERIFICATION_POLICY_KIND),
            ),
            outputs=(ArtifactPort("evidence", LVS_EVIDENCE_KIND),),
            facts=("lvs-status", "lvs-clean", "lvs-completed"),
            required_capabilities=("tool.calibre",),
            platform_assets=_verification_asset("lvs-deck"),
            adapters=(
                CALIBRE_PHYSICAL_VERIFICATION_ADAPTER,
                OFFLINE_PHYSICAL_VERIFICATION_ADAPTER,
            ),
            adapter_extensible=True,
        )
    )


__all__ = [
    "CANONICAL_SOURCE_NETLIST_KIND",
    "CALIBRE_PHYSICAL_VERIFICATION_ADAPTER",
    "DRC_ACTION",
    "DRC_EVIDENCE_KIND",
    "LVS_ACTION",
    "LVS_EVIDENCE_KIND",
    "MATERIALIZED_LAYOUT_KIND",
    "OFFLINE_PHYSICAL_VERIFICATION_ADAPTER",
    "PHYSICAL_VERIFICATION_POLICY_KIND",
    "PHYSICAL_VERIFICATION_SOURCE_ACTION",
    "RECEIPT_BOUND_VERIFICATION_SOURCE_ADAPTER",
    "register_physical_verification_actions",
]
