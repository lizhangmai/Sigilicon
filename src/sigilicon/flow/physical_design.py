"""Tool-independent Flow artifact contracts for physical design."""

from __future__ import annotations

from sigilicon.flow.model import (
    ActionContract,
    ArtifactPort,
)
from sigilicon.flow.registry import FlowRegistry


PHYSICAL_DESIGN_JOB_KIND = "physical-design.job"
PHYSICAL_DESIGN_RESULT_KIND = "physical-design.result"
PHYSICAL_CLOSURE_EVIDENCE_KIND = "evidence.physical-closure"
PHYSICAL_DESIGN_SOURCE_ACTION = "physical-design.source-job"
PHYSICAL_DESIGN_ACTION = "physical-design.solve"
PHYSICAL_MATERIALIZATION_ACTION = "physical-design.compile-materialization"
PHYSICAL_MATERIALIZATION_PLAN_KIND = "physical-design.materialization-plan"
MATERIALIZATION_ACCEPTANCE_EVIDENCE_KIND = "evidence.materialization-acceptance"
REFERENCE_PNR_ADAPTER = "reference-pnr"
REFERENCE_MATERIALIZATION_ADAPTER = "reference-materialization"


def register_physical_design_actions(registry: FlowRegistry) -> None:
    """Register reusable artifact and solver seams without project policy."""

    registry.register_action(
        ActionContract(
            kind=PHYSICAL_DESIGN_SOURCE_ACTION,
            outputs=(ArtifactPort("job", PHYSICAL_DESIGN_JOB_KIND),),
            adapters=("source-assets",),
            resolves_source_assets=True,
        )
    )
    registry.register_action(
        ActionContract(
            kind=PHYSICAL_MATERIALIZATION_ACTION,
            inputs=(
                ArtifactPort("job", PHYSICAL_DESIGN_JOB_KIND),
                ArtifactPort("result", PHYSICAL_DESIGN_RESULT_KIND),
            ),
            outputs=(
                ArtifactPort("plan", PHYSICAL_MATERIALIZATION_PLAN_KIND),
                ArtifactPort(
                    "acceptance-evidence",
                    MATERIALIZATION_ACCEPTANCE_EVIDENCE_KIND,
                ),
            ),
            facts=(
                "materialization-decision",
                "materialization-reason",
                "materialization-executable",
            ),
            adapters=(REFERENCE_MATERIALIZATION_ADAPTER,),
        )
    )
    registry.register_action(
        ActionContract(
            kind=PHYSICAL_DESIGN_ACTION,
            inputs=(ArtifactPort("job", PHYSICAL_DESIGN_JOB_KIND),),
            outputs=(
                ArtifactPort("result", PHYSICAL_DESIGN_RESULT_KIND),
                ArtifactPort(
                    "closure-evidence",
                    PHYSICAL_CLOSURE_EVIDENCE_KIND,
                    required=False,
                ),
            ),
            facts=(
                "physical-design-status",
                "physical-design-succeeded",
                "physical-design-closed",
                "closure-termination",
                "routing-termination",
                "state-budget-exhausted",
                "iteration-budget-exhausted",
            ),
            adapters=(REFERENCE_PNR_ADAPTER,),
        )
    )


__all__ = [
    "PHYSICAL_CLOSURE_EVIDENCE_KIND",
    "PHYSICAL_DESIGN_ACTION",
    "PHYSICAL_DESIGN_JOB_KIND",
    "PHYSICAL_DESIGN_RESULT_KIND",
    "PHYSICAL_DESIGN_SOURCE_ACTION",
    "PHYSICAL_MATERIALIZATION_ACTION",
    "PHYSICAL_MATERIALIZATION_PLAN_KIND",
    "MATERIALIZATION_ACCEPTANCE_EVIDENCE_KIND",
    "REFERENCE_MATERIALIZATION_ADAPTER",
    "REFERENCE_PNR_ADAPTER",
    "register_physical_design_actions",
]
