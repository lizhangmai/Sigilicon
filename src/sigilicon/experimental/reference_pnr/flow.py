"""Experimental Flow contract and registration for the reference PNR Adapter."""

from __future__ import annotations

from sigilicon.flow.evidence import FactKind, FactSchema, FactSpec
from sigilicon.flow.model import ActionContract, ArtifactPort
from sigilicon.flow.registry import FlowRegistry
from sigilicon.flow.physical_design import (
    PHYSICAL_DESIGN_JOB_KIND,
    PHYSICAL_DESIGN_RESULT_KIND,
)


REFERENCE_PHYSICAL_DESIGN_ACTION = "physical-design.reference-solve"
PHYSICAL_CLOSURE_EVIDENCE_KIND = "evidence.physical-closure"
REFERENCE_PNR_ADAPTER = "reference-pnr"

REFERENCE_PHYSICAL_DESIGN_FACT_SCHEMA = FactSchema(
    REFERENCE_PHYSICAL_DESIGN_ACTION,
    fields=(
        FactSpec(
            "physical-design-status",
            FactKind.TEXT,
            enum_values=("succeeded", "failed", "unsupported", "exhausted"),
        ),
        FactSpec("physical-design-succeeded", FactKind.BOOLEAN),
        FactSpec("physical-design-closed", FactKind.BOOLEAN),
        FactSpec(
            "closure-termination",
            FactKind.TEXT,
            enum_values=(
                "not_evaluated",
                "closed",
                "routing_terminated",
                "no_legal_repair",
                "repair_state_budget",
                "repair_iteration_budget",
                "independent_evaluation_failed",
            ),
        ),
        FactSpec(
            "routing-termination",
            FactKind.TEXT,
            enum_values=(
                "not_evaluated",
                "closed",
                "infeasible",
                "unsupported",
                "state_budget",
                "iteration_budget",
            ),
        ),
        FactSpec("state-budget-exhausted", FactKind.BOOLEAN),
        FactSpec("iteration-budget-exhausted", FactKind.BOOLEAN),
    ),
)


def register_reference_physical_design_action(registry: FlowRegistry) -> None:
    """Add the opt-in reference action to an existing Flow registry."""

    registry.register_action(
        ActionContract(
            kind=REFERENCE_PHYSICAL_DESIGN_ACTION,
            inputs=(ArtifactPort("job", PHYSICAL_DESIGN_JOB_KIND),),
            outputs=(
                ArtifactPort("result", PHYSICAL_DESIGN_RESULT_KIND),
                ArtifactPort(
                    "closure-evidence",
                    PHYSICAL_CLOSURE_EVIDENCE_KIND,
                    required=False,
                ),
            ),
            fact_schema=REFERENCE_PHYSICAL_DESIGN_FACT_SCHEMA,
            adapters=(REFERENCE_PNR_ADAPTER,),
        )
    )


__all__ = [
    "PHYSICAL_CLOSURE_EVIDENCE_KIND",
    "REFERENCE_PHYSICAL_DESIGN_ACTION",
    "REFERENCE_PHYSICAL_DESIGN_FACT_SCHEMA",
    "REFERENCE_PNR_ADAPTER",
    "register_reference_physical_design_action",
]
