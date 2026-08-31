"""Stable physical-design contracts with an external solver Adapter."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from conftest import StagedAdapterFixture
from physical_design_fixtures import routed_job, typed_result
from sigilicon.canonical import CanonicalSerializationError
from sigilicon.flow import (
    ActionBinding,
    ActionContract,
    AdapterExecution,
    ArtifactBinding,
    ArtifactPort,
    CollectedActionResult,
    ExecutionEnvironment,
    FactSet,
    FactSource,
    FlowEngine,
    FlowNode,
    FlowSpec,
    FlowTarget,
    ProducedArtifact,
)
from sigilicon.flow.physical_design import (
    MATERIALIZATION_PLAN_ADAPTER,
    PHYSICAL_DESIGN_ACTION,
    PHYSICAL_DESIGN_JOB_KIND,
    PHYSICAL_DESIGN_RESULT_KIND,
    PHYSICAL_MATERIALIZATION_ACTION,
)
from sigilicon.layout.materialization import (
    MaterializationDecision,
    materialization_plan_from_json,
)
from sigilicon.layout.physical_design import PhysicalDesignJob, ResultStatus
from sigilicon.layout.physical_design_serialization import (
    physical_design_job_from_json,
    physical_design_job_id,
    physical_design_result_from_json,
    physical_design_result_id,
)
from sigilicon.workflows.action_registry import build_action_registry


_JOB_ACTION = "contract-fixture.physical-job"
_JOB_ADAPTER = "contract-physical-job"
_SOLVER_ADAPTER = "contract-physical-solver"


class _JobAdapter(StagedAdapterFixture):
    def __init__(self, job: PhysicalDesignJob) -> None:
        self.job = job

    def validate_inputs(self, _context):
        return ()

    def prepare(self, _context):
        pass

    def execute(self, context):
        context.output_path("job", "job.json").write_text(
            self.job.canonical_json(), encoding="utf-8"
        )
        return AdapterExecution.succeeded()

    def collect_result(self, context, _execution):
        return CollectedActionResult(
            facts=FactSet.empty(
                context.action.fact_schema,
                source=FactSource(context.action.kind, context.node_id)
            ),
            artifacts=(
                ProducedArtifact(
                    "job",
                    PHYSICAL_DESIGN_JOB_KIND,
                    context.output_path("job", "job.json"),
                ),
            ),
        )


class _SolverAdapter(StagedAdapterFixture):
    def validate_inputs(self, context):
        try:
            physical_design_job_from_json(
                context.input("job").path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            return (str(exc),)
        return ()

    def prepare(self, _context):
        pass

    def execute(self, context):
        job = physical_design_job_from_json(
            context.input("job").path.read_text(encoding="utf-8")
        )
        result = typed_result(job, backend="external-contract-solver")
        context.output_path("result", "result.json").write_text(
            result.canonical_json(), encoding="utf-8"
        )
        return AdapterExecution.succeeded()

    def collect_result(self, context, _execution):
        result = physical_design_result_from_json(
            context.output_path("result", "result.json").read_text(encoding="utf-8")
        )
        facts = context.action.fact_schema.project(
            {
                "physical-design-status": result.status.value,
                "physical-design-succeeded": result.status is ResultStatus.SUCCEEDED,
                "physical-design-closed": result.closed,
            },
            source=FactSource(context.action.kind, context.node_id, result.artifact_id),
        )
        return CollectedActionResult(
            facts=facts,
            artifacts=(
                ProducedArtifact(
                    "result",
                    PHYSICAL_DESIGN_RESULT_KIND,
                    context.output_path("result", "result.json"),
                    qualifiers={"result-identity": physical_design_result_id(result)},
                ),
            ),
        )


def _engine(job: PhysicalDesignJob) -> FlowEngine:
    registry = build_action_registry()
    registry.register_action(
        ActionContract(
            _JOB_ACTION,
            outputs=(ArtifactPort("job", PHYSICAL_DESIGN_JOB_KIND),),
            adapters=(_JOB_ADAPTER,),
        )
    )
    registry.register_adapter(_JOB_ADAPTER, _JobAdapter(job))
    registry.register_action_adapter(
        PHYSICAL_DESIGN_ACTION,
        _SOLVER_ADAPTER,
        _SolverAdapter(),
    )
    return FlowEngine(registry)


def _spec() -> FlowSpec:
    return FlowSpec(
        owner="benchmark",
        flow_id="physical-design-flow",
        recipe_id="physical-design-flow-recipe",
        nodes=(
            FlowNode("job", _JOB_ACTION),
            FlowNode(
                "solve",
                PHYSICAL_DESIGN_ACTION,
                bindings=(ArtifactBinding("job", "job", "job"),),
            ),
            FlowNode(
                "materialize",
                PHYSICAL_MATERIALIZATION_ACTION,
                config={"target": {"owner": "benchmark", "name": "layout-candidate"}},
                bindings=(
                    ArtifactBinding("job", "job", "job"),
                    ArtifactBinding("result", "solve", "result"),
                ),
            ),
        ),
        targets=(FlowTarget("physical", ("materialize",)),),
        action_bindings=(
            ActionBinding(_JOB_ACTION, _JOB_ADAPTER),
            ActionBinding(PHYSICAL_DESIGN_ACTION, _SOLVER_ADAPTER),
            ActionBinding(PHYSICAL_MATERIALIZATION_ACTION, MATERIALIZATION_PLAN_ADAPTER),
        ),
    )


def test_physical_design_serialization_is_reversible_and_strict() -> None:
    job = routed_job("serialization")
    result = typed_result(job)

    assert physical_design_job_from_json(job.canonical_json()) == job
    assert physical_design_result_from_json(result.canonical_json()) == result

    raw = json.loads(job.canonical_json())
    raw["unknown"] = True
    with pytest.raises(CanonicalSerializationError, match="unknown=.*unknown"):
        physical_design_job_from_json(json.dumps(raw))

    raw = json.loads(job.canonical_json())
    raw["request"]["stages"][0] = "unknown-stage"
    with pytest.raises(CanonicalSerializationError, match="unknown PhysicalDesignStage"):
        physical_design_job_from_json(json.dumps(raw))


def test_physical_design_job_identity_binds_complete_stable_intent() -> None:
    job = routed_job("identity")
    changed = replace(
        job,
        request=replace(job.request, minimum_instance_spacing_dbu=1),
    )

    identity = physical_design_job_id(job)
    assert identity != physical_design_job_id(changed)
    assert identity.startswith(
        "physical-design-job:identity-technology:identity:sha256:"
    )


def test_external_solver_runs_through_public_flow_seam(tmp_path: Path) -> None:
    job = routed_job("external-solver")
    engine = _engine(job)
    result = engine.run(
        engine.plan(_spec(), "physical"),
        artifact_root=tmp_path / "artifacts",
        environment=ExecutionEnvironment(),
        run_id="1" * 32,
    )

    solved = physical_design_result_from_json(
        result.nodes["solve"].artifacts["result"].path.read_text(encoding="utf-8")
    )
    plan = materialization_plan_from_json(
        result.nodes["materialize"].artifacts["plan"].path.read_text(encoding="utf-8")
    )

    assert result.status == "accepted"
    assert solved.provenance.backend == "external-contract-solver"
    assert solved.provenance.job_identity == physical_design_job_id(job)
    assert plan.acceptance.decision is MaterializationDecision.EXECUTABLE


def test_physical_result_identity_is_deterministic() -> None:
    job = routed_job("deterministic-result")
    first = typed_result(job, backend="external-contract-solver")
    second = typed_result(job, backend="external-contract-solver")

    assert first.canonical_json() == second.canonical_json()
    assert physical_design_result_id(first) == physical_design_result_id(second)
