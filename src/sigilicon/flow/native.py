"""Typed Actions for native-OA and mixed-signal verification workflows."""

from __future__ import annotations

from sigilicon.flow.evidence import FactKind, FactSchema, FactSpec
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

_EVIDENCE_ROLES = (
    "diagnostic",
    "regression",
    "qualification",
    "signoff",
)
_EVIDENCE_LEVELS = ("l0", "l1", "l2", "l3", "l4")


def _schema(action_kind: str, *fields: FactSpec) -> FactSchema:
    return FactSchema(action_kind, fields)


def register_native_actions(registry: FlowRegistry) -> None:
    """Register reusable workflow seams without selecting owner recipes."""

    registry.register_action(
        ActionContract(
            kind=NATIVE_OA_PLAN_ACTION,
            outputs=(ArtifactPort("plan", NATIVE_OA_PLAN_KIND),),
            fact_schema=_schema(
                NATIVE_OA_PLAN_ACTION,
                FactSpec("source-plan-valid", FactKind.BOOLEAN),
                FactSpec("source-cell-count", FactKind.INTEGER, unit="count"),
                FactSpec("source-layout-count", FactKind.INTEGER, unit="count"),
                FactSpec(
                    "source-testbench-count",
                    FactKind.INTEGER,
                    unit="count",
                ),
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
            fact_schema=_schema(
                NATIVE_OA_SIMULATION_ACTION,
                FactSpec(
                    "native-evidence-status",
                    FactKind.TEXT,
                    enum_values=(
                        "pass",
                        "fail",
                        "not_evaluated",
                        "inconclusive",
                    ),
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
            fact_schema=_schema(
                XCELIUM_VERIFICATION_ACTION,
                FactSpec("passed", FactKind.BOOLEAN),
                FactSpec("simulator", FactKind.TEXT),
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
            required_capabilities=("tool.cadence-xcelium",),
            adapters=(XCELIUM_VERIFICATION_ADAPTER,),
            plan_input_kind=XCELIUM_ACTION_PLAN,
        )
    )
    registry.register_action(
        ActionContract(
            kind=XCELIUM_AMS_VERIFICATION_ACTION,
            outputs=(ArtifactPort("evidence", XCELIUM_AMS_EVIDENCE_KIND),),
            fact_schema=_schema(
                XCELIUM_AMS_VERIFICATION_ACTION,
                FactSpec("passed", FactKind.BOOLEAN),
                FactSpec("simulator", FactKind.TEXT),
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
