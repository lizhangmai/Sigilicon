"""Tool-independent Flow contracts for physical DRC and LVS evidence."""

from __future__ import annotations

from sigilicon.flow.evidence import FactKind, FactSchema, FactSpec

from sigilicon.flow.model import ActionContract, ArtifactPort, PlatformAssetRequirement
from sigilicon.flow.physical_design import (
    MATERIALIZED_GDS_KIND,
    MATERIALIZATION_RECEIPT_KIND,
)
from sigilicon.flow.registry import FlowRegistry


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


_PHYSICAL_VERIFICATION_STATUSES = (
    "clean",
    "violated",
    "unsupported",
    "backend_unavailable",
    "execution_failed",
)

DRC_FACT_SCHEMA = FactSchema(
    DRC_ACTION,
    fields=(
        FactSpec(
            "drc-status",
            FactKind.TEXT,
            enum_values=_PHYSICAL_VERIFICATION_STATUSES,
        ),
        FactSpec("drc-clean", FactKind.BOOLEAN),
        FactSpec("drc-completed", FactKind.BOOLEAN),
    ),
)
LVS_FACT_SCHEMA = FactSchema(
    LVS_ACTION,
    fields=(
        FactSpec(
            "lvs-status",
            FactKind.TEXT,
            enum_values=_PHYSICAL_VERIFICATION_STATUSES,
        ),
        FactSpec("lvs-clean", FactKind.BOOLEAN),
        FactSpec("lvs-completed", FactKind.BOOLEAN),
    ),
)


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
            fact_schema=FactSchema(PHYSICAL_VERIFICATION_SOURCE_ACTION),
            adapters=(RECEIPT_BOUND_VERIFICATION_SOURCE_ADAPTER,),
            resolves_source_assets=True,
        )
    )
    registry.register_action(
        ActionContract(
            kind=DRC_ACTION,
            inputs=(
                ArtifactPort("layout", MATERIALIZED_GDS_KIND),
                ArtifactPort("receipt", MATERIALIZATION_RECEIPT_KIND),
                ArtifactPort("verification-policy", PHYSICAL_VERIFICATION_POLICY_KIND),
            ),
            outputs=(ArtifactPort("evidence", DRC_EVIDENCE_KIND),),
            fact_schema=DRC_FACT_SCHEMA,
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
                ArtifactPort("layout", MATERIALIZED_GDS_KIND),
                ArtifactPort("receipt", MATERIALIZATION_RECEIPT_KIND),
                ArtifactPort("source", CANONICAL_SOURCE_NETLIST_KIND),
                ArtifactPort("verification-policy", PHYSICAL_VERIFICATION_POLICY_KIND),
            ),
            outputs=(ArtifactPort("evidence", LVS_EVIDENCE_KIND),),
            fact_schema=LVS_FACT_SCHEMA,
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
    "DRC_FACT_SCHEMA",
    "LVS_ACTION",
    "LVS_EVIDENCE_KIND",
    "LVS_FACT_SCHEMA",
    "OFFLINE_PHYSICAL_VERIFICATION_ADAPTER",
    "PHYSICAL_VERIFICATION_POLICY_KIND",
    "PHYSICAL_VERIFICATION_SOURCE_ACTION",
    "RECEIPT_BOUND_VERIFICATION_SOURCE_ADAPTER",
    "register_physical_verification_actions",
]
