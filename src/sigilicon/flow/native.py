"""Typed Actions for native-OA and mixed-signal verification workflows."""

from __future__ import annotations

from sigilicon.flow.model import ActionContract, ArtifactPort
from sigilicon.flow.registry import FlowRegistry


NATIVE_OA_PLAN_ACTION = "native-oa.plan"
NATIVE_OA_SIMULATION_ACTION = "native-oa.simulate"
XCELIUM_VERIFICATION_ACTION = "verification.xcelium-rtl"
XCELIUM_AMS_VERIFICATION_ACTION = "verification.xcelium-ams"

NATIVE_OA_PLAN_ADAPTER = "native-oa-plan"
NATIVE_OA_SIMULATION_ADAPTER = "native-oa-simulation"
XCELIUM_VERIFICATION_ADAPTER = "xcelium-verification"
XCELIUM_AMS_VERIFICATION_ADAPTER = "xcelium-ams-verification"

NATIVE_OA_PLAN_KIND = "native-oa.assembly-plan"
NATIVE_OA_ACTION_PLAN = "native-oa.assembly"
XCELIUM_ACTION_PLAN = "verification.xcelium-rtl.cell"
XCELIUM_AMS_ACTION_PLAN = "verification.xcelium-ams.cell"
NATIVE_OA_EVIDENCE_KIND = "evidence.native-oa-maestro"
XCELIUM_EVIDENCE_KIND = "evidence.xcelium-rtl-verification"
XCELIUM_AMS_EVIDENCE_KIND = "evidence.xcelium-ams-verification"


def register_native_actions(registry: FlowRegistry) -> None:
    """Register reusable workflow seams without selecting owner recipes."""

    registry.register_action(
        ActionContract(
            kind=NATIVE_OA_PLAN_ACTION,
            outputs=(ArtifactPort("plan", NATIVE_OA_PLAN_KIND),),
            facts=(
                "source-plan-valid",
                "source-cell-count",
                "source-layout-count",
                "source-testbench-count",
            ),
            adapters=(NATIVE_OA_PLAN_ADAPTER,),
            plan_input_kind=NATIVE_OA_ACTION_PLAN,
        )
    )
    registry.register_action(
        ActionContract(
            kind=NATIVE_OA_SIMULATION_ACTION,
            inputs=(ArtifactPort("plan", NATIVE_OA_PLAN_KIND),),
            outputs=(ArtifactPort("evidence", NATIVE_OA_EVIDENCE_KIND),),
            facts=(
                "execution-completed",
                "native-evidence-status",
                "evidence-role",
                "evidence-level",
                "evidence-scope",
                "product-qualification-conclusion",
            ),
            required_capabilities=(
                "tool.virtuoso-bridge",
                "license.cadence-oa",
            ),
            execution_capability="mutate-workspace",
            adapters=(NATIVE_OA_SIMULATION_ADAPTER,),
            plan_input_kind=NATIVE_OA_ACTION_PLAN,
        )
    )
    registry.register_action(
        ActionContract(
            kind=XCELIUM_VERIFICATION_ACTION,
            outputs=(ArtifactPort("evidence", XCELIUM_EVIDENCE_KIND),),
            facts=(
                "passed",
                "simulator",
                "evidence-role",
                "evidence-level",
                "evidence-scope",
                "product-qualification-conclusion",
            ),
            required_capabilities=("tool.cadence-xcelium",),
            adapters=(XCELIUM_VERIFICATION_ADAPTER,),
            plan_input_kind=XCELIUM_ACTION_PLAN,
        )
    )
    registry.register_action(
        ActionContract(
            kind=XCELIUM_AMS_VERIFICATION_ACTION,
            outputs=(ArtifactPort("evidence", XCELIUM_AMS_EVIDENCE_KIND),),
            facts=(
                "passed",
                "simulator",
                "evidence-role",
                "evidence-level",
                "evidence-scope",
                "product-qualification-conclusion",
            ),
            required_capabilities=("tool.cadence-xcelium",),
            adapters=(XCELIUM_AMS_VERIFICATION_ADAPTER,),
            plan_input_kind=XCELIUM_AMS_ACTION_PLAN,
        )
    )


__all__ = [
    "NATIVE_OA_EVIDENCE_KIND",
    "NATIVE_OA_ACTION_PLAN",
    "NATIVE_OA_PLAN_ACTION",
    "NATIVE_OA_PLAN_ADAPTER",
    "NATIVE_OA_PLAN_KIND",
    "NATIVE_OA_SIMULATION_ACTION",
    "NATIVE_OA_SIMULATION_ADAPTER",
    "XCELIUM_EVIDENCE_KIND",
    "XCELIUM_ACTION_PLAN",
    "XCELIUM_AMS_EVIDENCE_KIND",
    "XCELIUM_AMS_ACTION_PLAN",
    "XCELIUM_AMS_VERIFICATION_ACTION",
    "XCELIUM_AMS_VERIFICATION_ADAPTER",
    "XCELIUM_VERIFICATION_ACTION",
    "XCELIUM_VERIFICATION_ADAPTER",
    "register_native_actions",
]
