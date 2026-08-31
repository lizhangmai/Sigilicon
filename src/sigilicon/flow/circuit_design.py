"""Tool-independent Actions that project trusted backend evidence into a Design Campaign."""

from __future__ import annotations

from sigilicon.flow.model import ActionContract, ArtifactPort
from sigilicon.flow.physical_verification import (
    CANONICAL_SOURCE_NETLIST_KIND,
    DRC_EVIDENCE_KIND,
    LVS_EVIDENCE_KIND,
    PHYSICAL_VERIFICATION_POLICY_KIND,
)
from sigilicon.flow.post_layout import PEX_EVIDENCE_KIND
from sigilicon.flow.registry import FlowRegistry


CIRCUIT_DESIGN_SOURCE_ACTION = "circuit-design.source"
PHYSICAL_DESIGN_OBSERVATION_ACTION = "circuit-design.observe-physical"
PHYSICAL_DESIGN_OBSERVATION_ADAPTER = "physical-design-observation"
DESIGN_SOURCE_CHECK_ACTION = "circuit-design.source-check"
DESIGN_SOURCE_CHECK_ADAPTER = "project-design-source-check"
DESIGN_ELECTRICAL_DIAGNOSTIC_ACTION = "circuit-design.electrical-diagnostic"
DESIGN_ELECTRICAL_DIAGNOSTIC_ADAPTER = "project-design-electrical-diagnostic"
DESIGN_ACTION_PLAN = "circuit-design.target"
_CIRCUIT_TOPOLOGY_KIND = "circuit.topology-proposal.v1"
_DESIGN_CANDIDATE_KIND = "design.candidate.v1"
_DESIGN_EVIDENCE_KIND = "design.evidence.v1"
_DESIGN_CHECK_EVIDENCE_KIND = "evidence.design-source-check"
_DESIGN_ELECTRICAL_EVIDENCE_KIND = "evidence.design-electrical-diagnostic"


def register_circuit_design_actions(registry: FlowRegistry) -> None:
    # Ingestion seam only: source-assets selects an owner-authored typed topology.
    # Owner-specific source normalization remains in design_frontend.
    registry.register_action(
        ActionContract(
            kind=CIRCUIT_DESIGN_SOURCE_ACTION,
            outputs=(ArtifactPort("topology", _CIRCUIT_TOPOLOGY_KIND),),
            adapters=("source-assets",),
            resolves_source_assets=True,
        )
    )
    registry.register_action(
        ActionContract(
            kind=PHYSICAL_DESIGN_OBSERVATION_ACTION,
            inputs=(
                ArtifactPort("topology", _CIRCUIT_TOPOLOGY_KIND),
                ArtifactPort("source", CANONICAL_SOURCE_NETLIST_KIND),
                ArtifactPort("verification-policy", PHYSICAL_VERIFICATION_POLICY_KIND),
                ArtifactPort("drc", DRC_EVIDENCE_KIND),
                ArtifactPort("lvs", LVS_EVIDENCE_KIND),
                ArtifactPort("pex", PEX_EVIDENCE_KIND),
            ),
            outputs=(
                ArtifactPort("candidate", _DESIGN_CANDIDATE_KIND),
                ArtifactPort("drc", _DESIGN_EVIDENCE_KIND),
                ArtifactPort("lvs", _DESIGN_EVIDENCE_KIND),
                ArtifactPort("pex", _DESIGN_EVIDENCE_KIND),
            ),
            adapters=(PHYSICAL_DESIGN_OBSERVATION_ADAPTER,),
        )
    )
    registry.register_action(
        ActionContract(
            kind=DESIGN_SOURCE_CHECK_ACTION,
            outputs=(
                ArtifactPort("evidence", _DESIGN_CHECK_EVIDENCE_KIND),
            ),
            facts=(
                "passed",
                "execution-completed",
                "process-returncode",
                "evidence-role",
                "evidence-level",
                "evidence-scope",
                "product-qualification-conclusion",
            ),
            adapters=(DESIGN_SOURCE_CHECK_ADAPTER,),
            plan_input_kind=DESIGN_ACTION_PLAN,
        )
    )
    registry.register_action(
        ActionContract(
            kind=DESIGN_ELECTRICAL_DIAGNOSTIC_ACTION,
            outputs=(
                ArtifactPort("evidence", _DESIGN_ELECTRICAL_EVIDENCE_KIND),
            ),
            facts=(
                "passed",
                "execution-completed",
                "process-returncode",
                "evidence-role",
                "evidence-level",
                "evidence-scope",
                "product-qualification-conclusion",
            ),
            required_capabilities=("tool.cadence-spectre",),
            adapters=(DESIGN_ELECTRICAL_DIAGNOSTIC_ADAPTER,),
            plan_input_kind=DESIGN_ACTION_PLAN,
        )
    )


__all__ = [
    "CIRCUIT_DESIGN_SOURCE_ACTION",
    "DESIGN_ELECTRICAL_DIAGNOSTIC_ACTION",
    "DESIGN_ACTION_PLAN",
    "DESIGN_ELECTRICAL_DIAGNOSTIC_ADAPTER",
    "DESIGN_SOURCE_CHECK_ACTION",
    "DESIGN_SOURCE_CHECK_ADAPTER",
    "PHYSICAL_DESIGN_OBSERVATION_ACTION",
    "PHYSICAL_DESIGN_OBSERVATION_ADAPTER",
    "register_circuit_design_actions",
]
