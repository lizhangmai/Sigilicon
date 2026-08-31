"""Tool-independent PEX, post-layout, and physical qualification Actions."""

from __future__ import annotations

from sigilicon.flow.evidence import FactKind, FactSchema, FactSpec

from sigilicon.flow.model import ActionContract, ArtifactPort, PlatformAssetRequirement
from sigilicon.flow.physical_design import (
    MATERIALIZED_GDS_KIND,
    MATERIALIZATION_RECEIPT_KIND,
)
from sigilicon.flow.physical_verification import (
    CANONICAL_SOURCE_NETLIST_KIND,
    DRC_EVIDENCE_KIND,
    LVS_EVIDENCE_KIND,
)
from sigilicon.flow.registry import FlowRegistry


PEX_ACTION = "physical-verification.pex"
CALIBRE_XRC_PEX_ADAPTER = "calibre-xrc-pex"
POST_LAYOUT_ACTION = "physical-design.post-layout"
PHYSICAL_QUALIFICATION_ACTION = "physical-design.qualification"
PEX_EVIDENCE_KIND = "evidence.pex"
PEX_NETLIST_KIND = "netlist.pex"
POST_LAYOUT_SPEC_KIND = "spec.post-layout"
POST_LAYOUT_EVIDENCE_KIND = "evidence.post-layout"
PHYSICAL_QUALIFICATION_SPEC_KIND = "spec.physical-qualification"
PHYSICAL_QUALIFICATION_EVIDENCE_KIND = "evidence.physical-qualification"


_PEX_STATUSES = (
    "extracted",
    "unsupported",
    "backend_unavailable",
    "execution_failed",
)
_PHYSICAL_ANALYSIS_STATUSES = (
    "passed",
    "violated",
    "unsupported",
    "backend_unavailable",
    "execution_failed",
)

PEX_FACT_SCHEMA = FactSchema(
    PEX_ACTION,
    fields=(
        FactSpec("pex-status", FactKind.TEXT, enum_values=_PEX_STATUSES),
        FactSpec("pex-completed", FactKind.BOOLEAN),
    ),
)
POST_LAYOUT_FACT_SCHEMA = FactSchema(
    POST_LAYOUT_ACTION,
    fields=(
        FactSpec(
            "post-layout-status",
            FactKind.TEXT,
            enum_values=_PHYSICAL_ANALYSIS_STATUSES,
        ),
        FactSpec("post-layout-passed", FactKind.BOOLEAN),
    ),
)
PHYSICAL_QUALIFICATION_FACT_SCHEMA = FactSchema(
    PHYSICAL_QUALIFICATION_ACTION,
    fields=(
        FactSpec(
            "qualification-status",
            FactKind.TEXT,
            enum_values=_PHYSICAL_ANALYSIS_STATUSES,
        ),
        FactSpec("qualification-passed", FactKind.BOOLEAN),
    ),
)


def register_post_layout_actions(registry: FlowRegistry) -> None:
    """Register deep owner-extension seams without inventing a backend Adapter."""

    receipt_bound = (
        ArtifactPort("layout", MATERIALIZED_GDS_KIND),
        ArtifactPort("receipt", MATERIALIZATION_RECEIPT_KIND),
        ArtifactPort("source", CANONICAL_SOURCE_NETLIST_KIND),
    )
    registry.register_action(
        ActionContract(
            kind=PEX_ACTION,
            inputs=receipt_bound,
            outputs=(
                ArtifactPort("parasitics", PEX_NETLIST_KIND, required=False),
                ArtifactPort("evidence", PEX_EVIDENCE_KIND),
            ),
            fact_schema=PEX_FACT_SCHEMA,
            required_capabilities=("tool.pex",),
            platform_assets=(
                PlatformAssetRequirement(
                    "physical-pex",
                    "platform.pex",
                    members=("pex-deck", "pex-support-root"),
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
            fact_schema=POST_LAYOUT_FACT_SCHEMA,
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
            fact_schema=PHYSICAL_QUALIFICATION_FACT_SCHEMA,
            adapter_extensible=True,
        )
    )


__all__ = [
    "CALIBRE_XRC_PEX_ADAPTER",
    "PEX_ACTION",
    "PEX_EVIDENCE_KIND",
    "PEX_FACT_SCHEMA",
    "PEX_NETLIST_KIND",
    "PHYSICAL_QUALIFICATION_ACTION",
    "PHYSICAL_QUALIFICATION_EVIDENCE_KIND",
    "PHYSICAL_QUALIFICATION_FACT_SCHEMA",
    "PHYSICAL_QUALIFICATION_SPEC_KIND",
    "POST_LAYOUT_ACTION",
    "POST_LAYOUT_EVIDENCE_KIND",
    "POST_LAYOUT_FACT_SCHEMA",
    "POST_LAYOUT_SPEC_KIND",
    "register_post_layout_actions",
]
