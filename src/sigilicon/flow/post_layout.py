"""Tool-independent PEX, post-layout, and physical qualification Actions."""

from __future__ import annotations

from sigilicon.flow.model import ActionContract, ArtifactPort, PlatformAssetRequirement
from sigilicon.flow.physical_design import MATERIALIZATION_RECEIPT_KIND
from sigilicon.flow.physical_verification import (
    CANONICAL_SOURCE_NETLIST_KIND,
    DRC_EVIDENCE_KIND,
    LVS_EVIDENCE_KIND,
    MATERIALIZED_LAYOUT_KIND,
)
from sigilicon.flow.registry import FlowRegistry


PEX_ACTION = "physical-verification.pex"
POST_LAYOUT_ACTION = "physical-design.post-layout"
PHYSICAL_QUALIFICATION_ACTION = "physical-design.qualification"
PEX_EVIDENCE_KIND = "evidence.pex"
PEX_NETLIST_KIND = "netlist.pex"
POST_LAYOUT_SPEC_KIND = "spec.post-layout"
POST_LAYOUT_EVIDENCE_KIND = "evidence.post-layout"
PHYSICAL_QUALIFICATION_SPEC_KIND = "spec.physical-qualification"
PHYSICAL_QUALIFICATION_EVIDENCE_KIND = "evidence.physical-qualification"


def register_post_layout_actions(registry: FlowRegistry) -> None:
    """Register deep owner-extension seams without inventing a backend Adapter."""

    receipt_bound = (
        ArtifactPort("layout", MATERIALIZED_LAYOUT_KIND),
        ArtifactPort("receipt", MATERIALIZATION_RECEIPT_KIND),
        ArtifactPort("source", CANONICAL_SOURCE_NETLIST_KIND),
    )
    registry.register_action(
        ActionContract(
            kind=PEX_ACTION,
            inputs=receipt_bound,
            outputs=(
                ArtifactPort("parasitics", PEX_NETLIST_KIND),
                ArtifactPort("evidence", PEX_EVIDENCE_KIND),
            ),
            facts=("pex-status", "pex-completed"),
            required_capabilities=("tool.pex",),
            platform_assets=(
                PlatformAssetRequirement(
                    "physical-pex",
                    "platform.pex",
                    members=("pex-deck", "qrc-tech"),
                ),
            ),
            adapter_extensible=True,
        )
    )
    registry.register_action(
        ActionContract(
            kind=POST_LAYOUT_ACTION,
            inputs=(
                *receipt_bound,
                ArtifactPort("pex", PEX_EVIDENCE_KIND),
                ArtifactPort("parasitics", PEX_NETLIST_KIND),
                ArtifactPort("specification", POST_LAYOUT_SPEC_KIND),
            ),
            outputs=(ArtifactPort("evidence", POST_LAYOUT_EVIDENCE_KIND),),
            facts=("post-layout-status", "post-layout-passed"),
            required_capabilities=("tool.post-layout-simulation",),
            adapter_extensible=True,
        )
    )
    registry.register_action(
        ActionContract(
            kind=PHYSICAL_QUALIFICATION_ACTION,
            inputs=(
                *receipt_bound,
                ArtifactPort("drc", DRC_EVIDENCE_KIND),
                ArtifactPort("lvs", LVS_EVIDENCE_KIND),
                ArtifactPort("pex", PEX_EVIDENCE_KIND),
                ArtifactPort("post-layout", POST_LAYOUT_EVIDENCE_KIND),
                ArtifactPort(
                    "specification",
                    PHYSICAL_QUALIFICATION_SPEC_KIND,
                ),
            ),
            outputs=(
                ArtifactPort(
                    "evidence",
                    PHYSICAL_QUALIFICATION_EVIDENCE_KIND,
                ),
            ),
            facts=("qualification-status", "qualification-passed"),
            adapter_extensible=True,
        )
    )


__all__ = [
    "PEX_ACTION",
    "PEX_EVIDENCE_KIND",
    "PEX_NETLIST_KIND",
    "PHYSICAL_QUALIFICATION_ACTION",
    "PHYSICAL_QUALIFICATION_EVIDENCE_KIND",
    "PHYSICAL_QUALIFICATION_SPEC_KIND",
    "POST_LAYOUT_ACTION",
    "POST_LAYOUT_EVIDENCE_KIND",
    "POST_LAYOUT_SPEC_KIND",
    "register_post_layout_actions",
]
