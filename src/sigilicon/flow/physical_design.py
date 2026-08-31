"""Stable, tool-independent Flow contracts for physical design."""

from __future__ import annotations

from sigilicon.flow.model import (
    ActionContract,
    ArtifactPort,
    PlatformAssetRequirement,
)
from sigilicon.flow.evidence import FactKind, FactSchema, FactSpec
from sigilicon.flow.registry import FlowRegistry


PHYSICAL_DESIGN_JOB_KIND = "physical-design.job"
PHYSICAL_DESIGN_RESULT_KIND = "physical-design.result"
PHYSICAL_DESIGN_SOURCE_ACTION = "physical-design.source-job"
PHYSICAL_DESIGN_RESULT_SOURCE_ACTION = "physical-design.source-result"
PHYSICAL_DESIGN_ACTION = "physical-design.solve"
PHYSICAL_MATERIALIZATION_ACTION = "physical-design.compile-materialization"
PHYSICAL_MATERIALIZATION_PLAN_KIND = "physical-design.materialization-plan"
MATERIALIZATION_ACCEPTANCE_EVIDENCE_KIND = "evidence.materialization-acceptance"
PHYSICAL_MATERIALIZATION_EXECUTION_ACTION = "physical-design.materialize"
MATERIALIZED_GDS_KIND = "layout.gds"
MATERIALIZATION_RECEIPT_KIND = "evidence.materialization-receipt"
MATERIALIZATION_PLAN_ADAPTER = "materialization-plan"
OA_XSTREAM_MATERIALIZATION_ADAPTER = "oa-virtuoso-xstream-materialization"


PHYSICAL_DESIGN_SOURCE_FACT_SCHEMA = FactSchema(
    PHYSICAL_DESIGN_SOURCE_ACTION,
)
PHYSICAL_DESIGN_RESULT_SOURCE_FACT_SCHEMA = FactSchema(
    PHYSICAL_DESIGN_RESULT_SOURCE_ACTION,
)
PHYSICAL_MATERIALIZATION_EXECUTION_FACT_SCHEMA = FactSchema(
    PHYSICAL_MATERIALIZATION_EXECUTION_ACTION,
    fields=(
        FactSpec(
            "materialization-status",
            FactKind.TEXT,
            enum_values=(
                "materialized",
                "unsupported",
                "backend_unavailable",
                "execution_failed",
                "invalid_plan_identity",
            ),
        ),
        FactSpec("materialized", FactKind.BOOLEAN),
        FactSpec("backend-executed", FactKind.BOOLEAN),
        FactSpec("backend-completed", FactKind.BOOLEAN),
    ),
)
PHYSICAL_MATERIALIZATION_FACT_SCHEMA = FactSchema(
    PHYSICAL_MATERIALIZATION_ACTION,
    fields=(
        FactSpec(
            "materialization-decision",
            FactKind.TEXT,
            enum_values=("executable", "diagnostic", "rejected"),
        ),
        FactSpec(
            "materialization-reason",
            FactKind.TEXT,
            enum_values=(
                "accepted",
                "state_budget",
                "iteration_budget",
                "budget_exhausted",
                "unsupported",
                "infeasible",
                "invalid_solution",
            ),
        ),
        FactSpec("materialization-executable", FactKind.BOOLEAN),
    ),
)
PHYSICAL_DESIGN_FACT_SCHEMA = FactSchema(
    PHYSICAL_DESIGN_ACTION,
    fields=(
        FactSpec(
            "physical-design-status",
            FactKind.TEXT,
            enum_values=("succeeded", "failed", "unsupported", "exhausted"),
        ),
        FactSpec("physical-design-succeeded", FactKind.BOOLEAN),
        FactSpec("physical-design-closed", FactKind.BOOLEAN),
    ),
)


def register_physical_design_actions(registry: FlowRegistry) -> None:
    """Register reusable source, solver, and materialization seams."""

    registry.register_action(
        ActionContract(
            kind=PHYSICAL_DESIGN_SOURCE_ACTION,
            outputs=(ArtifactPort("job", PHYSICAL_DESIGN_JOB_KIND),),
            fact_schema=PHYSICAL_DESIGN_SOURCE_FACT_SCHEMA,
            adapters=("source-assets",),
            resolves_source_assets=True,
        )
    )
    registry.register_action(
        ActionContract(
            kind=PHYSICAL_DESIGN_RESULT_SOURCE_ACTION,
            outputs=(ArtifactPort("result", PHYSICAL_DESIGN_RESULT_KIND),),
            fact_schema=PHYSICAL_DESIGN_RESULT_SOURCE_FACT_SCHEMA,
            adapters=("source-assets",),
            resolves_source_assets=True,
        )
    )
    registry.register_action(
        ActionContract(
            kind=PHYSICAL_MATERIALIZATION_EXECUTION_ACTION,
            inputs=(
                ArtifactPort("job", PHYSICAL_DESIGN_JOB_KIND),
                ArtifactPort("result", PHYSICAL_DESIGN_RESULT_KIND),
                ArtifactPort("plan", PHYSICAL_MATERIALIZATION_PLAN_KIND),
            ),
            outputs=(
                ArtifactPort("layout", MATERIALIZED_GDS_KIND, required=False),
                ArtifactPort("receipt", MATERIALIZATION_RECEIPT_KIND),
            ),
            fact_schema=PHYSICAL_MATERIALIZATION_EXECUTION_FACT_SCHEMA,
            required_capabilities=("tool.layout-materializer",),
            platform_assets=(
                PlatformAssetRequirement(
                    "physical-layout",
                    "platform.layout-view-set",
                    members=("layer-map", "master-layouts"),
                ),
            ),
            adapters=(),
            adapter_extensible=True,
            execution_capability="mutate-workspace",
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
            fact_schema=PHYSICAL_MATERIALIZATION_FACT_SCHEMA,
            adapters=(MATERIALIZATION_PLAN_ADAPTER,),
            adapter_extensible=True,
        )
    )
    registry.register_action(
        ActionContract(
            kind=PHYSICAL_DESIGN_ACTION,
            inputs=(ArtifactPort("job", PHYSICAL_DESIGN_JOB_KIND),),
            outputs=(
                ArtifactPort("result", PHYSICAL_DESIGN_RESULT_KIND),
            ),
            fact_schema=PHYSICAL_DESIGN_FACT_SCHEMA,
            adapters=(),
            adapter_extensible=True,
        )
    )


__all__ = [
    "PHYSICAL_DESIGN_ACTION",
    "PHYSICAL_DESIGN_JOB_KIND",
    "PHYSICAL_DESIGN_RESULT_KIND",
    "PHYSICAL_DESIGN_RESULT_SOURCE_ACTION",
    "OA_XSTREAM_MATERIALIZATION_ADAPTER",
    "PHYSICAL_DESIGN_SOURCE_ACTION",
    "PHYSICAL_MATERIALIZATION_ACTION",
    "PHYSICAL_MATERIALIZATION_EXECUTION_ACTION",
    "PHYSICAL_MATERIALIZATION_PLAN_KIND",
    "MATERIALIZED_GDS_KIND",
    "MATERIALIZATION_ACCEPTANCE_EVIDENCE_KIND",
    "MATERIALIZATION_RECEIPT_KIND",
    "PHYSICAL_DESIGN_SOURCE_FACT_SCHEMA",
    "PHYSICAL_DESIGN_RESULT_SOURCE_FACT_SCHEMA",
    "PHYSICAL_MATERIALIZATION_EXECUTION_FACT_SCHEMA",
    "PHYSICAL_MATERIALIZATION_FACT_SCHEMA",
    "PHYSICAL_DESIGN_FACT_SCHEMA",
    "MATERIALIZATION_PLAN_ADAPTER",
    "register_physical_design_actions",
]
