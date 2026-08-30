"""Typed Actions for owner-planned custom-layout execution."""

from __future__ import annotations

from sigilicon.flow.model import ActionContract, ArtifactPort
from sigilicon.flow.registry import FlowRegistry


LAYOUT_GENERATION_ACTION = "custom-layout.generate"
LAYOUT_GENERATION_ADAPTER = "project-layout-generation"
LAYOUT_VERIFICATION_ACTION = "custom-layout.verify"
LAYOUT_VERIFICATION_ADAPTER = "project-layout-verification"
LAYOUT_GENERATION_EVIDENCE_KIND = "evidence.layout-generation"
LAYOUT_VERIFICATION_EVIDENCE_KIND = "evidence.layout-verification"


def register_layout_actions(registry: FlowRegistry) -> None:
    """Register the stable seams shared by analog/custom-layout backends."""

    registry.register_action(
        ActionContract(
            kind=LAYOUT_GENERATION_ACTION,
            outputs=(
                ArtifactPort("evidence", LAYOUT_GENERATION_EVIDENCE_KIND),
            ),
            facts=(
                "passed",
                "execution-completed",
                "instance-count",
                "product-qualification-conclusion",
            ),
            required_capabilities=(
                "tool.virtuoso-bridge",
                "license.cadence-oa",
            ),
            adapters=(LAYOUT_GENERATION_ADAPTER,),
            execution_capability="mutate-workspace",
        )
    )
    registry.register_action(
        ActionContract(
            kind=LAYOUT_VERIFICATION_ACTION,
            outputs=(
                ArtifactPort("evidence", LAYOUT_VERIFICATION_EVIDENCE_KIND),
            ),
            facts=(
                "passed",
                "execution-completed",
                "check",
                "evidence-role",
                "evidence-level",
                "evidence-scope",
                "product-qualification-conclusion",
            ),
            required_capabilities=(
                "tool.virtuoso-bridge",
                "license.cadence-oa",
                "tool.cadence-xstream",
                "tool.calibre",
            ),
            adapters=(LAYOUT_VERIFICATION_ADAPTER,),
        )
    )


__all__ = [
    "LAYOUT_GENERATION_ACTION",
    "LAYOUT_GENERATION_ADAPTER",
    "LAYOUT_GENERATION_EVIDENCE_KIND",
    "LAYOUT_VERIFICATION_ACTION",
    "LAYOUT_VERIFICATION_ADAPTER",
    "LAYOUT_VERIFICATION_EVIDENCE_KIND",
    "register_layout_actions",
]
