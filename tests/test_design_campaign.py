from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from sigilicon.domain.circuit_design import (
    ARTIFACT_SCHEMA,
    CIRCUIT_TOPOLOGY_KIND,
    DESIGN_CANDIDATE_KIND,
    DESIGN_EVIDENCE_KIND,
    ArtifactMetadata,
    ArtifactReference,
    CircuitInstance,
    CircuitPort,
    CircuitTopologyProposal,
    DesignCandidate,
    DesignDecisionConclusion,
    DesignEvidence,
    EvidenceCompletion,
    EvidenceConclusion,
    EvidenceFinding,
    EvidenceLevel,
    EvidenceProducer,
    EvidenceProducerKind,
    EvidenceRole,
    PortDirection,
    ProposalProvenance,
    StateSemantic,
    TopologyOrigin,
)
from sigilicon.flow import (
    ActionContext,
    ActionContract,
    AdapterExecution,
    AdapterSelection,
    ArtifactPort,
    CollectedActionResult,
    ExecutionProfile,
    FlowEngine,
    FlowNode,
    FlowRegistry,
    FlowSpec,
    FlowTarget,
    ProducedArtifact,
)
from sigilicon.workflows.design_campaign import (
    DesignArtifactBinding,
    DesignCampaign,
    DesignCampaignAttempt,
    DesignCampaignAttemptSpec,
    DesignCampaignBudget,
    DesignCampaignRunner,
    DesignCampaignScope,
    DesignCampaignSpec,
    DesignCampaignTermination,
    DesignStage,
    DesignStageBinding,
    DesignStageStatus,
    design_campaign_result_from_json,
    design_campaign_spec_from_json,
)
from sigilicon.workflows.design_repair import (
    TopologyRepairPolicy,
    compile_topology_repair,
)


OWNER = "example"
SOURCE = ArtifactReference(OWNER, "source.netlist", "1" * 64, None)
POLICY = ArtifactReference(OWNER, "spec.design-policy", "2" * 64, None)
ACTION = "design.attempt"


def _topology(
    *,
    master: str = "INV",
    origin: TopologyOrigin = TopologyOrigin.SOURCE_AUTHORED,
) -> CircuitTopologyProposal:
    return CircuitTopologyProposal(
        ArtifactMetadata(ARTIFACT_SCHEMA, CIRCUIT_TOPOLOGY_KIND, OWNER),
        "pilot",
        SOURCE.sha256,
        origin,
        (
            CircuitPort("IN", PortDirection.INPUT, "signal"),
            CircuitPort("OUT", PortDirection.OUTPUT, "signal"),
        ),
        (),
        (CircuitInstance("X0", master, ("IN", "OUT"), (), "logic-stage"),),
        (StateSemantic("functional", "owner-authored functional state"),),
        ProposalProvenance("test-proposal", "1"),
    )


def _attempt_artifacts(
    conclusion: EvidenceConclusion,
    *,
    topology: CircuitTopologyProposal | None = None,
    parent_candidate: ArtifactReference | None = None,
) -> tuple[DesignCandidate, CircuitTopologyProposal, DesignEvidence]:
    topology = _topology() if topology is None else topology
    completion = EvidenceCompletion(
        "test-backend",
        conclusion not in {
            EvidenceConclusion.BACKEND_UNAVAILABLE,
            EvidenceConclusion.NOT_EVALUATED,
        },
        conclusion not in {
            EvidenceConclusion.BACKEND_UNAVAILABLE,
            EvidenceConclusion.NOT_EVALUATED,
        },
        None
        if conclusion in {
            EvidenceConclusion.BACKEND_UNAVAILABLE,
            EvidenceConclusion.NOT_EVALUATED,
        }
        else 0,
    )
    evidence = DesignEvidence(
        ArtifactMetadata(ARTIFACT_SCHEMA, DESIGN_EVIDENCE_KIND, OWNER),
        topology.reference(),
        SOURCE,
        POLICY,
        EvidenceRole.DIAGNOSTIC,
        EvidenceLevel.L0,
        ("functional",),
        EvidenceProducer(
            EvidenceProducerKind.EXECUTED_BACKEND,
            "test-backend",
            "1",
        ),
        completion,
        conclusion,
        (
            (EvidenceFinding("functional-failure", 1, "typed failure"),)
            if conclusion is EvidenceConclusion.VIOLATED
            else ()
        ),
        "typed campaign evidence",
    )
    candidate = DesignCandidate(
        ArtifactMetadata(ARTIFACT_SCHEMA, DESIGN_CANDIDATE_KIND, OWNER),
        "pilot",
        SOURCE,
        None,
        (POLICY,),
        topology.reference(),
        None,
        None,
        (evidence.reference(),),
        parent_candidate,
        ProposalProvenance("test-candidate", "1"),
    )
    return candidate, topology, evidence


class AttemptAdapter:
    def __init__(
        self,
        conclusion: EvidenceConclusion,
        *,
        artifacts: tuple[DesignCandidate, CircuitTopologyProposal, DesignEvidence]
        | None = None,
    ) -> None:
        self.candidate, self.topology, self.evidence = (
            _attempt_artifacts(conclusion) if artifacts is None else artifacts
        )

    def validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        return ()

    def prepare(self, context: ActionContext) -> None:
        pass

    def execute(self, context: ActionContext) -> AdapterExecution:
        context.output_path("candidate", "candidate.json").write_text(
            self.candidate.canonical_json(), encoding="utf-8"
        )
        context.output_path("topology", "topology.json").write_text(
            self.topology.canonical_json(), encoding="utf-8"
        )
        context.output_path("l0-evidence", "evidence.json").write_text(
            self.evidence.canonical_json(), encoding="utf-8"
        )
        return AdapterExecution.succeeded()

    def collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        return CollectedActionResult(
            artifacts=(
                ProducedArtifact(
                    "candidate",
                    DESIGN_CANDIDATE_KIND,
                    context.output_path("candidate", "candidate.json"),
                ),
                ProducedArtifact(
                    "topology",
                    CIRCUIT_TOPOLOGY_KIND,
                    context.output_path("topology", "topology.json"),
                ),
                ProducedArtifact(
                    "l0-evidence",
                    DESIGN_EVIDENCE_KIND,
                    context.output_path("l0-evidence", "evidence.json"),
                ),
            )
        )


def _engine_and_plan(conclusion: EvidenceConclusion) -> tuple[FlowEngine, object]:
    registry = FlowRegistry()
    registry.register_action(
        ActionContract(
            ACTION,
            outputs=(
                ArtifactPort("candidate", DESIGN_CANDIDATE_KIND),
                ArtifactPort("topology", CIRCUIT_TOPOLOGY_KIND),
                ArtifactPort("l0-evidence", DESIGN_EVIDENCE_KIND),
            ),
            adapters=("typed-attempt",),
        )
    )
    registry.register_adapter("typed-attempt", AttemptAdapter(conclusion))
    engine = FlowEngine(registry)
    spec = FlowSpec(
        OWNER,
        "design-campaign-attempt",
        (FlowNode("attempt", ACTION),),
        (FlowTarget("all", ("attempt",)),),
    )
    profile = ExecutionProfile(
        OWNER,
        "typed",
        (AdapterSelection(ACTION, "typed-attempt"),),
    )
    return engine, engine.plan(spec, "all", profile)


def _campaign(plan: object) -> DesignCampaign:
    return DesignCampaign(
        OWNER,
        "pilot-campaign",
        (
            DesignCampaignAttempt(
                "baseline",
                plan,
                DesignArtifactBinding("candidate", "attempt", "candidate"),
                (
                    DesignArtifactBinding("topology", "attempt", "topology"),
                    DesignArtifactBinding("l0", "attempt", "l0-evidence"),
                ),
                (DesignStageBinding(DesignStage.L0, "l0"),),
                None,
            ),
        ),
        DesignCampaignBudget(1, 1, 1),
        DesignCampaignScope(
            (DesignStage.L0,),
            EvidenceRole.DIAGNOSTIC,
            EvidenceLevel.L0,
            ("functional",),
            POLICY,
        ),
    )


def test_campaign_spec_is_canonical_strict_and_path_free() -> None:
    spec = DesignCampaignSpec(
        OWNER,
        "pilot-campaign",
        (
            DesignCampaignAttemptSpec(
                "baseline",
                "design-campaign-attempt",
                "all",
                "typed",
                DesignArtifactBinding("candidate", "attempt", "candidate"),
                (
                    DesignArtifactBinding("topology", "attempt", "topology"),
                    DesignArtifactBinding("l0", "attempt", "l0-evidence"),
                ),
                (DesignStageBinding(DesignStage.L0, "l0"),),
                None,
            ),
        ),
        DesignCampaignBudget(1, 1, 1),
        DesignCampaignScope(
            (DesignStage.L0,),
            EvidenceRole.DIAGNOSTIC,
            EvidenceLevel.L0,
            ("functional",),
            POLICY,
        ),
    )

    assert design_campaign_spec_from_json(spec.canonical_json()) == spec
    assert "/" not in spec.canonical_json()
    payload = json.loads(spec.canonical_json())
    payload["command"] = "touch owned"
    with pytest.raises(ValueError, match="unknown"):
        design_campaign_spec_from_json(
            json.dumps(payload, indent=2, sort_keys=True) + "\n"
        )


def test_campaign_executes_one_flow_attempt_and_produces_typed_decision(tmp_path: Path) -> None:
    engine, plan = _engine_and_plan(EvidenceConclusion.SATISFIED)
    result = DesignCampaignRunner(engine, artifact_root=tmp_path).run(_campaign(plan))

    assert result.termination is DesignCampaignTermination.PASSED
    assert result.final_decision.conclusion is DesignDecisionConclusion.PASSED
    assert result.iterations[0].quality.status(DesignStage.L0) is DesignStageStatus.SATISFIED
    assert result.iterations[0].provenance.plan_sha256
    assert result.iterations[0].provenance.artifacts
    assert design_campaign_result_from_json(result.canonical_json()) == result


def test_campaign_preserves_backend_unavailable_as_non_conclusion(tmp_path: Path) -> None:
    engine, plan = _engine_and_plan(EvidenceConclusion.BACKEND_UNAVAILABLE)
    result = DesignCampaignRunner(engine, artifact_root=tmp_path).run(_campaign(plan))

    assert result.termination is DesignCampaignTermination.BACKEND_UNAVAILABLE
    assert result.final_decision.conclusion is DesignDecisionConclusion.NON_CONCLUSION
    assert (
        result.iterations[0].quality.status(DesignStage.L0)
        is DesignStageStatus.BACKEND_UNAVAILABLE
    )


def test_campaign_reports_artifact_lineage_drift_as_invalid_identity(
    tmp_path: Path,
) -> None:
    valid = _attempt_artifacts(EvidenceConclusion.SATISFIED)
    drifted = replace(
        valid[0],
        topology=ArtifactReference(OWNER, CIRCUIT_TOPOLOGY_KIND, "f" * 64, None),
    )
    registry = FlowRegistry()
    registry.register_action(
        ActionContract(
            ACTION,
            outputs=(
                ArtifactPort("candidate", DESIGN_CANDIDATE_KIND),
                ArtifactPort("topology", CIRCUIT_TOPOLOGY_KIND),
                ArtifactPort("l0-evidence", DESIGN_EVIDENCE_KIND),
            ),
            adapters=("drifted-attempt",),
        )
    )
    registry.register_adapter(
        "drifted-attempt",
        AttemptAdapter(
            EvidenceConclusion.SATISFIED,
            artifacts=(drifted, valid[1], valid[2]),
        ),
    )
    engine = FlowEngine(registry)
    plan = engine.plan(
        FlowSpec(
            OWNER,
            "design-campaign-attempt",
            (FlowNode("attempt", ACTION),),
            (FlowTarget("all", ("attempt",)),),
        ),
        "all",
        ExecutionProfile(
            OWNER,
            "drifted",
            (AdapterSelection(ACTION, "drifted-attempt"),),
        ),
    )

    result = DesignCampaignRunner(engine, artifact_root=tmp_path).run(_campaign(plan))

    assert result.termination is DesignCampaignTermination.INVALID_IDENTITY
    assert result.iterations == ()


def test_campaign_budget_is_independent_of_flow_engine_and_fail_closed(tmp_path: Path) -> None:
    engine, plan = _engine_and_plan(EvidenceConclusion.SATISFIED)
    campaign = _campaign(plan)
    too_small = DesignCampaign(
        campaign.owner,
        campaign.campaign_id,
        campaign.attempts,
        DesignCampaignBudget(1, 1, 0),
        campaign.scope,
    )

    result = DesignCampaignRunner(engine, artifact_root=tmp_path).run(too_small)

    assert result.termination is DesignCampaignTermination.COST_BUDGET
    assert result.iterations == ()
    assert result.final_decision is None


def test_campaign_replays_an_explicit_parent_bound_repair_attempt(tmp_path: Path) -> None:
    first = _attempt_artifacts(EvidenceConclusion.VIOLATED)
    proposed = _topology(master="BUF", origin=TopologyOrigin.PROPOSED)
    repair = compile_topology_repair(
        candidate=first[0],
        parent=first[1],
        proposed=proposed,
        evidence=(first[2],),
        policy=TopologyRepairPolicy(
            OWNER,
            "topology-policy",
            ("BUF",),
            1,
            ("l0-functional",),
        ),
        required_regressions=("l0-functional",),
    )
    second = _attempt_artifacts(
        EvidenceConclusion.SATISFIED,
        topology=proposed,
        parent_candidate=first[0].reference(),
    )
    registry = FlowRegistry()
    registry.register_action(
        ActionContract(
            ACTION,
            outputs=(
                ArtifactPort("candidate", DESIGN_CANDIDATE_KIND),
                ArtifactPort("topology", CIRCUIT_TOPOLOGY_KIND),
                ArtifactPort("l0-evidence", DESIGN_EVIDENCE_KIND),
            ),
            adapters=("first-attempt", "second-attempt"),
        )
    )
    registry.register_adapter(
        "first-attempt",
        AttemptAdapter(EvidenceConclusion.VIOLATED, artifacts=first),
    )
    registry.register_adapter(
        "second-attempt",
        AttemptAdapter(EvidenceConclusion.SATISFIED, artifacts=second),
    )
    engine = FlowEngine(registry)
    spec = FlowSpec(
        OWNER,
        "design-campaign-attempt",
        (FlowNode("attempt", ACTION),),
        (FlowTarget("all", ("attempt",)),),
    )
    first_plan = engine.plan(
        spec,
        "all",
        ExecutionProfile(
            OWNER,
            "first",
            (AdapterSelection(ACTION, "first-attempt"),),
        ),
    )
    second_plan = engine.plan(
        spec,
        "all",
        ExecutionProfile(
            OWNER,
            "second",
            (AdapterSelection(ACTION, "second-attempt"),),
        ),
    )
    binding = lambda iteration, plan, repair_plan: DesignCampaignAttempt(
        iteration,
        plan,
        DesignArtifactBinding("candidate", "attempt", "candidate"),
        (
            DesignArtifactBinding("topology", "attempt", "topology"),
            DesignArtifactBinding("l0", "attempt", "l0-evidence"),
        ),
        (DesignStageBinding(DesignStage.L0, "l0"),),
        repair_plan,
    )
    campaign = DesignCampaign(
        OWNER,
        "repair-campaign",
        (
            binding("baseline", first_plan, None),
            binding("repaired", second_plan, repair),
        ),
        DesignCampaignBudget(2, 2, 2),
        DesignCampaignScope(
            (DesignStage.L0,),
            EvidenceRole.DIAGNOSTIC,
            EvidenceLevel.L0,
            ("functional",),
            POLICY,
        ),
    )

    result = DesignCampaignRunner(engine, artifact_root=tmp_path).run(campaign)

    assert result.termination is DesignCampaignTermination.PASSED
    assert len(result.iterations) == 2
    assert result.iterations[0].decision.conclusion is DesignDecisionConclusion.FAILED
    assert result.iterations[1].candidate.parent_candidate == first[0].reference()

    drifted = DesignCampaign(
        campaign.owner,
        "drifted-repair-campaign",
        (
            binding("baseline", first_plan, None),
            binding(
                "repaired",
                second_plan,
                replace(repair, parent_candidate_sha256="f" * 64),
            ),
        ),
        campaign.budget,
        campaign.scope,
    )
    rejected = DesignCampaignRunner(engine, artifact_root=tmp_path).run(drifted)
    assert rejected.termination is DesignCampaignTermination.INVALID_IDENTITY
    assert len(rejected.iterations) == 1

    evidence_drift = DesignCampaign(
        campaign.owner,
        "evidence-drift-repair-campaign",
        (
            binding("baseline", first_plan, None),
            binding(
                "repaired",
                second_plan,
                replace(repair, evidence_sha256=("f" * 64,)),
            ),
        ),
        campaign.budget,
        campaign.scope,
    )
    evidence_rejected = DesignCampaignRunner(
        engine,
        artifact_root=tmp_path,
    ).run(evidence_drift)
    assert evidence_rejected.termination is DesignCampaignTermination.INVALID_IDENTITY
    assert len(evidence_rejected.iterations) == 1
