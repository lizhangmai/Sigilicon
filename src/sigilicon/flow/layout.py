"""Typed Actions for owner-planned custom-layout execution."""

from __future__ import annotations

from sigilicon.flow.evidence import FactKind, FactSchema, FactSpec
from sigilicon.flow.model import ActionContract, ArtifactPort
from sigilicon.flow.registry import FlowRegistry


LAYOUT_GENERATION_ACTION = "custom-layout.generate"
LAYOUT_GENERATION_ADAPTER = "project-layout-generation"
LAYOUT_VERIFICATION_ACTION = "custom-layout.verify"
LAYOUT_VERIFICATION_ADAPTER = "project-layout-verification"
LAYOUT_GENERATION_EVIDENCE_KIND = "evidence.layout-generation"
LAYOUT_VERIFICATION_EVIDENCE_KIND = "evidence.layout-verification"
LAYOUT_ACTION_PLAN = "custom-layout.plan"

_EVIDENCE_ROLES = (
    "diagnostic",
    "regression",
    "qualification",
    "signoff",
)
_EVIDENCE_LEVELS = ("l0", "l1", "l2", "l3", "l4")


def _schema(action_kind: str, *fields: FactSpec) -> FactSchema:
    return FactSchema(action_kind, fields)


def register_layout_actions(registry: FlowRegistry) -> None:
    """Register the stable seams shared by analog/custom-layout backends."""

    registry.register_action(
        ActionContract(
            kind=LAYOUT_GENERATION_ACTION,
            outputs=(
                ArtifactPort("evidence", LAYOUT_GENERATION_EVIDENCE_KIND),
            ),
            fact_schema=_schema(
                LAYOUT_GENERATION_ACTION,
                FactSpec("passed", FactKind.BOOLEAN),
                FactSpec("instance-count", FactKind.INTEGER, unit="count"),
                FactSpec("product-qualification-conclusion", FactKind.BOOLEAN),
            ),
            required_capabilities=(
                "tool.virtuoso-bridge",
                "license.cadence-oa",
            ),
            adapters=(LAYOUT_GENERATION_ADAPTER,),
            execution_capability="mutate-workspace",
            plan_input_kind=LAYOUT_ACTION_PLAN,
        )
    )
    registry.register_action(
        ActionContract(
            kind=LAYOUT_VERIFICATION_ACTION,
            outputs=(
                ArtifactPort("evidence", LAYOUT_VERIFICATION_EVIDENCE_KIND),
            ),
            fact_schema=_schema(
                LAYOUT_VERIFICATION_ACTION,
                FactSpec("passed", FactKind.BOOLEAN),
                FactSpec(
                    "check",
                    FactKind.TEXT,
                    enum_values=("drc", "lvs"),
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
            required_capabilities=(
                "tool.virtuoso-bridge",
                "license.cadence-oa",
                "tool.cadence-xstream",
                "tool.calibre",
            ),
            adapters=(LAYOUT_VERIFICATION_ADAPTER,),
            plan_input_kind=LAYOUT_ACTION_PLAN,
        )
    )


__all__ = [
    "LAYOUT_GENERATION_ACTION",
    "LAYOUT_ACTION_PLAN",
    "LAYOUT_GENERATION_ADAPTER",
    "LAYOUT_GENERATION_EVIDENCE_KIND",
    "LAYOUT_VERIFICATION_ACTION",
    "LAYOUT_VERIFICATION_ADAPTER",
    "LAYOUT_VERIFICATION_EVIDENCE_KIND",
    "register_layout_actions",
]
