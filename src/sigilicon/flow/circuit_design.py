"""Tool-independent Actions that project trusted backend evidence into a Design Campaign."""

from __future__ import annotations

from sigilicon.flow.evidence import FactKind, FactSchema, FactSpec
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

_EVIDENCE_ROLES = (
    "diagnostic",
    "regression",
    "qualification",
    "signoff",
)
_EVIDENCE_LEVELS = ("l0", "l1", "l2", "l3", "l4")


def _schema(action_kind: str, *fields: FactSpec) -> FactSchema:
    return FactSchema(action_kind, fields)


def register_circuit_design_actions(registry: FlowRegistry) -> None:
    # Ingestion seam only: source-assets selects an owner-authored typed topology.
    # Owner-specific source normalization remains in design_frontend.
    registry.register_action(
        ActionContract(
            kind=CIRCUIT_DESIGN_SOURCE_ACTION,
            outputs=(ArtifactPort("topology", _CIRCUIT_TOPOLOGY_KIND),),
            fact_schema=_schema(CIRCUIT_DESIGN_SOURCE_ACTION),
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
            fact_schema=_schema(PHYSICAL_DESIGN_OBSERVATION_ACTION),
            adapters=(PHYSICAL_DESIGN_OBSERVATION_ADAPTER,),
        )
    )
    registry.register_action(
        ActionContract(
            kind=DESIGN_SOURCE_CHECK_ACTION,
            outputs=(
                ArtifactPort("evidence", _DESIGN_CHECK_EVIDENCE_KIND),
            ),
            fact_schema=_schema(
                DESIGN_SOURCE_CHECK_ACTION,
                FactSpec("passed", FactKind.BOOLEAN),
                FactSpec(
                    "process-returncode",
                    FactKind.INTEGER,
                    unit="exit-code",
                ),
                FactSpec(
                    "evidence-role",
                    FactKind.TEXT,
                    enum_values=_EVIDENCE_ROLES,
                ),
                FactSpec(
                    "evidence-level",
                    FactKind.TEXT,
                    enum_values=_EVIDENCE_LEVELS,
                ),
                FactSpec("evidence-scope", FactKind.TEXT),
                FactSpec("product-qualification-conclusion", FactKind.BOOLEAN),
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
            fact_schema=_schema(
                DESIGN_ELECTRICAL_DIAGNOSTIC_ACTION,
                FactSpec("passed", FactKind.BOOLEAN),
                FactSpec(
                    "process-returncode",
                    FactKind.INTEGER,
                    unit="exit-code",
                ),
                FactSpec(
                    "evidence-role",
                    FactKind.TEXT,
                    enum_values=_EVIDENCE_ROLES,
                ),
                FactSpec(
                    "evidence-level",
                    FactKind.TEXT,
                    enum_values=_EVIDENCE_LEVELS,
                ),
                FactSpec("evidence-scope", FactKind.TEXT),
                FactSpec("product-qualification-conclusion", FactKind.BOOLEAN),
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
