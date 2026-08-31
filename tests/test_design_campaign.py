from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from conftest import StagedAdapterFixture
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
    ActionBinding,
    ActionContext,
    ActionContract,
    AdapterExecution,
    ArtifactPort,
    CollectedActionResult,
    FlowContractError,
    FlowEngine,
    FlowNode,
    FlowRegistry,
    FlowSpec,
    FlowTarget,
    FactSet,
    FactSource,
    ProducedArtifact,
)
from sigilicon.campaigns.design import (
    DESIGN_CAMPAIGN_ITERATION_EXTENSION,
    DesignArtifactBinding,
    DesignCampaign,
    DesignCampaignAttempt,
    DesignCampaignContinuation,
    DesignCampaignPhase,
    DesignCampaignAttemptSpec,
    DesignCampaignBudget,
    DesignCampaignRunner,
    DesignCampaignScope,
    DesignCampaignSpec,
    DesignCampaignTermination,
    DesignStage,
    DesignStageBinding,
    DesignStageStatus,
    design_campaign_iteration_input,
    design_campaign_result_from_json,
    design_campaign_spec_from_json,
    design_campaign_state_from_json,
)
from sigilicon.campaigns.repair import (
    DesignRepairProposal,
    TopologyRepairPolicy,
    compile_topology_repair,
)


OWNER = "example"
SOURCE = ArtifactReference(OWNER, "source.netlist", "source-fixture", None)
POLICY = ArtifactReference(OWNER, "spec.design-policy", "design-policy-fixture", None)
ACTION = "design.attempt"


def _empty_action_facts(context: ActionContext) -> FactSet:
    """Bind an intentionally empty evidence set to the executing Action/node."""

    return FactSet.empty(
        context.action.fact_schema,
        source=FactSource(context.action.kind, context.node_id),
    )


def _topology(
    *,
    master: str = "INV",
    origin: TopologyOrigin = TopologyOrigin.SOURCE_AUTHORED,
) -> CircuitTopologyProposal:
    return CircuitTopologyProposal(
        ArtifactMetadata(
            ARTIFACT_SCHEMA,
            CIRCUIT_TOPOLOGY_KIND,
            OWNER,
            f"{OWNER}:topology:{master.lower()}:{origin.value}",
        ),
        "pilot",
        SOURCE.identity,
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
        "typed-fake-adapter",
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
        ArtifactMetadata(
            ARTIFACT_SCHEMA,
            DESIGN_EVIDENCE_KIND,
            OWNER,
            f"{OWNER}:evidence:{topology.identity}:{conclusion.value}",
        ),
        topology.reference(),
        SOURCE,
        POLICY,
        EvidenceRole.DIAGNOSTIC,
        EvidenceLevel.L0,
        ("functional",),
        EvidenceProducer(EvidenceProducerKind.FAKE, "typed-fake-adapter", "1"),
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
        ArtifactMetadata(
            ARTIFACT_SCHEMA,
            DESIGN_CANDIDATE_KIND,
            OWNER,
            f"{OWNER}:candidate:{topology.identity}:{conclusion.value}",
        ),
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


class AttemptAdapter(StagedAdapterFixture):
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
            ),
            facts=_empty_action_facts(context),
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
        owner=OWNER,
        flow_id="design-campaign-attempt",
        recipe_id="design-campaign-attempt-recipe",
        nodes=(FlowNode("attempt", ACTION),),
        targets=(FlowTarget("all", ("attempt",)),),
        action_bindings=(ActionBinding(ACTION, "typed-attempt"),),
    )
    return engine, engine.plan(spec, "all")


def _campaign(plan: object) -> DesignCampaign:
    return DesignCampaign(
        OWNER,
        "pilot-campaign",
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
        DesignCampaignAttemptSpec(
            "baseline",
            "all",
            "design-campaign-attempt",
            DesignArtifactBinding("candidate", "attempt", "candidate"),
            (
                DesignArtifactBinding("topology", "attempt", "topology"),
                DesignArtifactBinding("l0", "attempt", "l0-evidence"),
            ),
            (DesignStageBinding(DesignStage.L0, "l0"),),
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
    result = DesignCampaignRunner(engine, artifact_root=tmp_path).start(_campaign(plan))

    assert result.termination is DesignCampaignTermination.PASSED
    assert result.iterations[-1].decision.conclusion is DesignDecisionConclusion.PASSED
    assert result.iterations[0].quality.status(DesignStage.L0) is DesignStageStatus.SATISFIED
    assert result.iterations[0].provenance.plan_identity
    assert result.iterations[0].provenance.artifacts
    assert design_campaign_state_from_json(result.canonical_json()) == result


def test_campaign_preserves_backend_unavailable_as_non_conclusion(tmp_path: Path) -> None:
    engine, plan = _engine_and_plan(EvidenceConclusion.BACKEND_UNAVAILABLE)
    result = DesignCampaignRunner(engine, artifact_root=tmp_path).start(_campaign(plan))

    assert result.termination is DesignCampaignTermination.BACKEND_UNAVAILABLE
    assert result.iterations[-1].decision.conclusion is DesignDecisionConclusion.NON_CONCLUSION
    assert (
        result.iterations[0].quality.status(DesignStage.L0)
        is DesignStageStatus.BACKEND_UNAVAILABLE
    )


@pytest.mark.parametrize(
    "drift",
    (
        {"role": EvidenceRole.REGRESSION},
        {"level": EvidenceLevel.L1},
        {"scope": ("other",)},
        {
            "specification": ArtifactReference(
                OWNER,
                "spec.design-policy",
                "forged-identity",
                None,
            )
        },
    ),
)
def test_campaign_rejects_required_stage_evidence_contract_drift(
    tmp_path: Path,
    drift: dict[str, object],
) -> None:
    candidate, topology, evidence = _attempt_artifacts(EvidenceConclusion.SATISFIED)
    drifted_evidence = replace(evidence, **drift)
    specifications = (
        candidate.canonical_specifications
        if drifted_evidence.specification in candidate.canonical_specifications
        else (*candidate.canonical_specifications, drifted_evidence.specification)
    )
    drifted_candidate = replace(
        candidate,
        canonical_specifications=specifications,
        evidence=(drifted_evidence.reference(),),
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
            adapters=("role-drift",),
        )
    )
    registry.register_adapter(
        "role-drift",
        AttemptAdapter(
            EvidenceConclusion.SATISFIED,
            artifacts=(drifted_candidate, topology, drifted_evidence),
        ),
    )
    engine = FlowEngine(registry)
    plan = engine.plan(
        FlowSpec(
            owner=OWNER,
            flow_id="design-campaign-attempt",
            recipe_id="design-campaign-attempt-role-drift-recipe",
            nodes=(FlowNode("attempt", ACTION),),
            targets=(FlowTarget("all", ("attempt",)),),
            action_bindings=(ActionBinding(ACTION, "role-drift"),),
        ),
        "all",
    )

    result = DesignCampaignRunner(engine, artifact_root=tmp_path).start(_campaign(plan))

    assert result.termination is DesignCampaignTermination.INVALID_IDENTITY
    assert result.iterations == ()


def test_campaign_reports_artifact_lineage_drift_as_invalid_identity(
    tmp_path: Path,
) -> None:
    valid = _attempt_artifacts(EvidenceConclusion.SATISFIED)
    drifted = replace(
        valid[0],
        topology=ArtifactReference(OWNER, CIRCUIT_TOPOLOGY_KIND, "forged-topology", None),
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
            owner=OWNER,
            flow_id="design-campaign-attempt",
            recipe_id="design-campaign-attempt-lineage-drift-recipe",
            nodes=(FlowNode("attempt", ACTION),),
            targets=(FlowTarget("all", ("attempt",)),),
            action_bindings=(ActionBinding(ACTION, "drifted-attempt"),),
        ),
        "all",
    )

    result = DesignCampaignRunner(engine, artifact_root=tmp_path).start(_campaign(plan))

    assert result.termination is DesignCampaignTermination.INVALID_IDENTITY
    assert result.iterations == ()


def test_campaign_budget_is_independent_of_flow_engine_and_fail_closed(tmp_path: Path) -> None:
    engine, plan = _engine_and_plan(EvidenceConclusion.SATISFIED)
    campaign = _campaign(plan)
    too_small = DesignCampaign(
        campaign.owner,
        campaign.campaign_id,
        campaign.baseline,
        DesignCampaignBudget(1, 1, 0),
        campaign.scope,
    )

    result = DesignCampaignRunner(engine, artifact_root=tmp_path).start(too_small)

    assert result.termination is DesignCampaignTermination.COST_BUDGET
    assert result.iterations == ()

    time_limited = replace(
        campaign,
        budget=DesignCampaignBudget(1, 1, 1, 1),
    )
    expired = DesignCampaignRunner(engine, artifact_root=tmp_path).start(
        time_limited,
        elapsed_seconds=1,
    )
    assert expired.termination is DesignCampaignTermination.TIME_BUDGET
    assert expired.iterations == ()


def test_campaign_content_identity_covers_budget_and_scope(tmp_path: Path) -> None:
    engine, plan = _engine_and_plan(EvidenceConclusion.SATISFIED)
    campaign = _campaign(plan)
    changed_budget = replace(
        campaign,
        budget=DesignCampaignBudget(2, 1, 1),
    )
    changed_scope = replace(
        campaign,
        scope=replace(campaign.scope, decision_scope=("functional", "timing")),
    )
    runner = DesignCampaignRunner(engine, artifact_root=tmp_path)

    assert runner.campaign_identity(campaign) != runner.campaign_identity(changed_budget)
    assert runner.campaign_identity(campaign) != runner.campaign_identity(changed_scope)
    assert runner.plan_record(campaign) != runner.plan_record(changed_budget)
    assert runner.plan_record(campaign) != runner.plan_record(changed_scope)


def test_campaign_attempt_identity_includes_execution_context(tmp_path: Path) -> None:
    engine, plan = _engine_and_plan(EvidenceConclusion.SATISFIED)
    campaign = _campaign(plan)

    first = DesignCampaignRunner(
        engine,
        artifact_root=tmp_path,
        execution_context_identity="environment-first",
    ).start(campaign)
    second = DesignCampaignRunner(
        engine,
        artifact_root=tmp_path,
        execution_context_identity="environment-second",
    ).start(campaign)

    assert first.iterations[0].provenance.run_id != second.iterations[0].provenance.run_id


def test_campaign_caller_has_one_baseline_and_no_repair_input() -> None:
    from dataclasses import fields

    assert tuple(item.name for item in fields(DesignCampaignSpec)) == (
        "owner",
        "campaign_id",
        "baseline",
        "budget",
        "scope",
        "continuation",
    )
    assert "repair_plan" not in {
        item.name for item in fields(DesignCampaignAttemptSpec)
    }
    assert tuple(item.name for item in fields(DesignCampaignAttemptSpec)) == (
        "iteration_id",
        "target",
        "operation",
        "candidate",
        "artifacts",
        "stages",
    )


def test_continuation_rejects_an_adapter_that_did_not_declare_consumption(
    tmp_path: Path,
) -> None:
    engine, plan = _engine_and_plan(EvidenceConclusion.VIOLATED)
    campaign = _campaign(plan)
    continuation = DesignCampaignContinuation(
        plan,
        campaign.baseline.candidate,
        campaign.baseline.artifacts,
        campaign.baseline.stages,
        "attempt",
        TopologyRepairPolicy(
            OWNER,
            "topology-policy",
            ("BUF",),
            1,
            ("l0-functional",),
        ),
    )

    with pytest.raises(FlowContractError, match="typed Design Campaign continuation"):
        DesignCampaignRunner(engine, artifact_root=tmp_path).plan_record(
            replace(campaign, continuation=continuation)
        )


def test_campaign_owns_iteration_extension_schema_validation() -> None:
    payload = {
        "campaign_identity": "example:design-campaign:fixture",
        "iteration": 2,
        "parent_candidate_identity": "example:candidate:parent",
        "attribution_json": "{}",
        "proposal_json": "{}",
        "repair_plan_json": "{}",
        "schema": 1,
        "contract_kind": "design-campaign-iteration-input",
    }

    assert design_campaign_iteration_input(payload).iteration == 2
    with pytest.raises(ValueError, match="fields are invalid"):
        design_campaign_iteration_input({**payload, "flow_owned": True})
    with pytest.raises(ValueError, match="at least two"):
        design_campaign_iteration_input({**payload, "iteration": 1})


class FeedbackDrivenAttemptAdapter(AttemptAdapter):
    """Deterministic pilot whose second round is supplied by Campaign, not caller."""

    accepted_extensions = (DESIGN_CAMPAIGN_ITERATION_EXTENSION,)

    def __init__(self) -> None:
        super().__init__(EvidenceConclusion.VIOLATED)

    def execute(self, context: ActionContext) -> AdapterExecution:
        iteration_payload = context.extensions.get(
            DESIGN_CAMPAIGN_ITERATION_EXTENSION
        )
        if iteration_payload is not None:
            iteration = design_campaign_iteration_input(iteration_payload)
            from sigilicon.campaigns.repair import design_repair_proposal_from_json

            proposal = design_repair_proposal_from_json(iteration.proposal_json)
            assert proposal.proposed_topology is not None
            topology = proposal.proposed_topology
            parent = ArtifactReference(
                OWNER,
                DESIGN_CANDIDATE_KIND,
                iteration.parent_candidate_identity,
                None,
            )
            self.candidate, self.topology, self.evidence = _attempt_artifacts(
                EvidenceConclusion.SATISFIED,
                topology=topology,
                parent_candidate=parent,
            )
        return super().execute(context)


def test_feedback_driven_campaign_pauses_for_proposal_and_derives_second_round(
    tmp_path: Path,
) -> None:
    registry = FlowRegistry()
    registry.register_action(
        ActionContract(
            ACTION,
            outputs=(
                ArtifactPort("candidate", DESIGN_CANDIDATE_KIND),
                ArtifactPort("topology", CIRCUIT_TOPOLOGY_KIND),
                ArtifactPort("l0-evidence", DESIGN_EVIDENCE_KIND),
            ),
            adapters=("feedback-driven",),
            accepted_extensions=(DESIGN_CAMPAIGN_ITERATION_EXTENSION,),
        )
    )
    feedback_adapter = FeedbackDrivenAttemptAdapter()
    registry.register_adapter("feedback-driven", feedback_adapter)
    engine = FlowEngine(registry)
    plan = engine.plan(
        FlowSpec(
            owner=OWNER,
            flow_id="design-campaign-attempt",
            recipe_id="design-campaign-attempt-feedback-recipe",
            nodes=(FlowNode("attempt", ACTION),),
            targets=(FlowTarget("all", ("attempt",)),),
            action_bindings=(ActionBinding(ACTION, "feedback-driven"),),
        ),
        "all",
    )
    attempt = DesignCampaignAttempt(
        "baseline",
        plan,
        DesignArtifactBinding("candidate", "attempt", "candidate"),
        (
            DesignArtifactBinding("topology", "attempt", "topology"),
            DesignArtifactBinding("l0", "attempt", "l0-evidence"),
        ),
        (DesignStageBinding(DesignStage.L0, "l0"),),
        None,
    )
    policy = TopologyRepairPolicy(
        OWNER,
        "topology-policy",
        ("BUF",),
        1,
        ("l0-functional",),
    )
    campaign = DesignCampaign(
        OWNER,
        "feedback-campaign",
        attempt,
        DesignCampaignBudget(2, 2, 2),
        DesignCampaignScope(
            (DesignStage.L0,),
            EvidenceRole.DIAGNOSTIC,
            EvidenceLevel.L0,
            ("functional",),
            POLICY,
        ),
        DesignCampaignContinuation(
            plan,
            attempt.candidate,
            attempt.artifacts,
            attempt.stages,
            "attempt",
            policy,
        ),
    )
    runner = DesignCampaignRunner(engine, artifact_root=tmp_path)

    paused = runner.start(campaign)

    assert paused.phase is DesignCampaignPhase.PROPOSAL_REQUIRED
    assert DesignCampaignRunner(engine, artifact_root=tmp_path).start(campaign) == paused
    assert len(paused.iterations) == 1
    assert paused.iterations[0].decision.evidence
    assert runner._reload_iteration_values(
        campaign.baseline,
        paused.iterations[0],
    )[-1].producer.kind is EvidenceProducerKind.FAKE
    assert campaign.baseline.iteration_id == "baseline"
    assert paused.attribution is not None
    assert design_campaign_state_from_json(paused.canonical_json()) == paused
    drifted_state = json.loads(paused.canonical_json())
    drifted_state["model_verified"] = True
    with pytest.raises(ValueError, match="unknown"):
        design_campaign_state_from_json(
            json.dumps(drifted_state, indent=2, sort_keys=True) + "\n"
        )

    proposal = DesignRepairProposal(
        f"{OWNER}:proposal:feedback-round-2",
        OWNER,
        paused.campaign_identity,
        paused.iterations[-1].candidate.identity,
        paused.attribution.identity,
        tuple(item.identity for item in paused.attribution.evidence),
        proposed_topology=_topology(master="BUF", origin=TopologyOrigin.PROPOSED),
        proposed_sizing=None,
        requested_evaluations=None,
        required_regressions=("l0-functional",),
        provenance=ProposalProvenance(
            "test-semantic-client",
            "1",
            (paused.iterations[-1].candidate.identity,),
        ),
    )
    with pytest.raises(ValueError, match="Proposal rejected"):
        runner.resume(
            campaign,
            paused,
            replace(
                proposal,
                proposed_topology=_topology(
                    master="UNAPPROVED",
                    origin=TopologyOrigin.PROPOSED,
                ),
            ),
        )
    with pytest.raises(ValueError, match="identity drift"):
        runner.resume(
            campaign,
            paused,
            replace(proposal, evidence_identity=("forged-evidence",)),
        )
    for budget, elapsed, expected in (
        (
            DesignCampaignBudget(2, 1, 2),
            0,
            DesignCampaignTermination.ITERATION_BUDGET,
        ),
        (
            DesignCampaignBudget(1, 2, 2),
            0,
            DesignCampaignTermination.STATE_BUDGET,
        ),
        (
            DesignCampaignBudget(2, 2, 1),
            0,
            DesignCampaignTermination.COST_BUDGET,
        ),
        (
            DesignCampaignBudget(2, 2, 2, 1),
            2,
            DesignCampaignTermination.TIME_BUDGET,
        ),
    ):
        budgeted_campaign = replace(campaign, budget=budget)
        budgeted_identity = runner.campaign_identity(budgeted_campaign)
        budgeted_state = replace(paused, campaign_identity=budgeted_identity)
        budgeted_proposal = replace(proposal, campaign_identity=budgeted_identity)
        stopped = runner.resume(
            budgeted_campaign,
            budgeted_state,
            budgeted_proposal,
            elapsed_seconds=elapsed,
        )
        assert stopped.phase is DesignCampaignPhase.COMPLETED
        assert stopped.termination is expected
    completed = runner.resume(campaign, paused, proposal)

    assert completed.phase is DesignCampaignPhase.COMPLETED
    assert completed.termination is DesignCampaignTermination.PASSED
    assert len(completed.iterations) == 2
    assert feedback_adapter.evidence.producer.kind is EvidenceProducerKind.FAKE
    assert completed.iterations[-1].candidate.parent_candidate == paused.iterations[-1].candidate.reference()
    assert completed.iterations[-1].provenance.run_id != paused.iterations[-1].provenance.run_id
    assert design_campaign_state_from_json(completed.canonical_json()) == completed


def test_sizing_child_binds_proposed_point_result_and_verification(
    tmp_path: Path,
) -> None:
    from test_design_repair import (
        _candidate as repair_candidate,
        _evidence as repair_evidence,
        _problem as repair_problem,
        _result as repair_result,
        _topology as repair_topology,
    )
    from sigilicon.domain.circuit_design import (
        CIRCUIT_SIZING_PROBLEM_KIND,
        CIRCUIT_SIZING_RESULT_KIND,
        CircuitSizingResult,
        SizingCandidateOutcome,
        SizingCandidateResult,
    )
    from sigilicon.campaigns.repair import (
        SizingRepairPolicy,
        design_repair_proposal_from_json,
        sizing_repair_plan_from_json,
    )

    topology = repair_topology()
    baseline_problem = repair_problem(topology)
    baseline_result = repair_result(baseline_problem)
    baseline_evidence = repair_evidence(baseline_result.reference())
    baseline_candidate = repair_candidate(
        topology,
        baseline_evidence,
        problem=baseline_problem,
        result=baseline_result,
    )

    class SizingFeedbackAdapter(AttemptAdapter):
        accepted_extensions = (DESIGN_CAMPAIGN_ITERATION_EXTENSION,)

        def __init__(self) -> None:
            self.values = (
                baseline_candidate,
                topology,
                baseline_problem,
                baseline_result,
                baseline_evidence,
            )

        def execute(self, context: ActionContext) -> AdapterExecution:
            iteration_payload = context.extensions.get(
                DESIGN_CAMPAIGN_ITERATION_EXTENSION
            )
            if iteration_payload is not None:
                iteration = design_campaign_iteration_input(iteration_payload)
                proposal = design_repair_proposal_from_json(iteration.proposal_json)
                repair = sizing_repair_plan_from_json(iteration.repair_plan_json)
                assert proposal.proposed_sizing == repair.proposed_candidate
                child_problem = replace(
                    baseline_problem,
                    metadata=ArtifactMetadata(
                        ARTIFACT_SCHEMA,
                        CIRCUIT_SIZING_PROBLEM_KIND,
                        OWNER,
                        "example:sizing-problem:child",
                    ),
                )
                child_result = CircuitSizingResult(
                    ArtifactMetadata(
                        ARTIFACT_SCHEMA,
                        CIRCUIT_SIZING_RESULT_KIND,
                        OWNER,
                        "example:sizing-result:child",
                    ),
                    child_problem.reference(),
                    EvidenceRole.DIAGNOSTIC,
                    "typed-fake-sizing",
                    "1",
                    baseline_result.termination,
                    (
                        SizingCandidateResult(
                            repair.proposed_candidate.name,
                            SizingCandidateOutcome.SATISFIED,
                            1,
                            (),
                        ),
                    ),
                    repair.proposed_candidate.name,
                    1,
                    baseline_result.seed,
                )
                child_evidence = repair_evidence(
                    child_result.reference(),
                    conclusion=EvidenceConclusion.SATISFIED,
                )
                child_candidate = replace(
                    repair_candidate(
                        topology,
                        child_evidence,
                        problem=child_problem,
                        result=child_result,
                    ),
                    metadata=ArtifactMetadata(
                        ARTIFACT_SCHEMA,
                        DESIGN_CANDIDATE_KIND,
                        OWNER,
                        "example:candidate:sizing-child",
                    ),
                    parent_candidate=baseline_candidate.reference(),
                )
                self.values = (
                    child_candidate,
                    topology,
                    child_problem,
                    child_result,
                    child_evidence,
                )
            for role, value in zip(
                ("candidate", "topology", "problem", "result", "l0-evidence"),
                self.values,
                strict=True,
            ):
                context.output_path(role, f"{role}.json").write_text(
                    value.canonical_json(),
                    encoding="utf-8",
                )
            return AdapterExecution.succeeded()

        def collect_result(self, context, execution):
            return CollectedActionResult(
                artifacts=tuple(
                    ProducedArtifact(role, kind, context.output_path(role, f"{role}.json"))
                    for role, kind in (
                        ("candidate", DESIGN_CANDIDATE_KIND),
                        ("topology", CIRCUIT_TOPOLOGY_KIND),
                        ("problem", CIRCUIT_SIZING_PROBLEM_KIND),
                        ("result", CIRCUIT_SIZING_RESULT_KIND),
                        ("l0-evidence", DESIGN_EVIDENCE_KIND),
                    )
                ),
                facts=_empty_action_facts(context),
            )

    registry = FlowRegistry()
    registry.register_action(
        ActionContract(
            ACTION,
            outputs=(
                ArtifactPort("candidate", DESIGN_CANDIDATE_KIND),
                ArtifactPort("topology", CIRCUIT_TOPOLOGY_KIND),
                ArtifactPort("problem", CIRCUIT_SIZING_PROBLEM_KIND),
                ArtifactPort("result", CIRCUIT_SIZING_RESULT_KIND),
                ArtifactPort("l0-evidence", DESIGN_EVIDENCE_KIND),
            ),
            adapters=("sizing-feedback",),
            accepted_extensions=(DESIGN_CAMPAIGN_ITERATION_EXTENSION,),
        )
    )
    adapter = SizingFeedbackAdapter()
    registry.register_adapter("sizing-feedback", adapter)
    engine = FlowEngine(registry)
    plan = engine.plan(
        FlowSpec(
            owner=OWNER,
            flow_id="sizing-campaign-attempt",
            recipe_id="sizing-campaign-attempt-feedback-recipe",
            nodes=(FlowNode("attempt", ACTION),),
            targets=(FlowTarget("all", ("attempt",)),),
            action_bindings=(ActionBinding(ACTION, "sizing-feedback"),),
        ),
        "all",
    )
    attempt = DesignCampaignAttempt(
        "baseline",
        plan,
        DesignArtifactBinding("candidate", "attempt", "candidate"),
        (
            DesignArtifactBinding("topology", "attempt", "topology"),
            DesignArtifactBinding("problem", "attempt", "problem"),
            DesignArtifactBinding("result", "attempt", "result"),
            DesignArtifactBinding("l0", "attempt", "l0-evidence"),
        ),
        (DesignStageBinding(DesignStage.L0, "l0"),),
        None,
    )
    campaign = DesignCampaign(
        OWNER,
        "sizing-feedback-campaign",
        attempt,
        DesignCampaignBudget(2, 2, 2),
        DesignCampaignScope(
            (DesignStage.L0,),
            EvidenceRole.DIAGNOSTIC,
            EvidenceLevel.L0,
            ("functional",),
            POLICY,
        ),
        DesignCampaignContinuation(
            plan,
            attempt.candidate,
            attempt.artifacts,
            attempt.stages,
            "attempt",
            SizingRepairPolicy(
                OWNER,
                "sizing-policy",
                ("wn", "wp"),
                2,
                ("l1-functional",),
            ),
        ),
    )
    runner = DesignCampaignRunner(engine, artifact_root=tmp_path)
    paused = runner.start(campaign)
    assert paused.phase is DesignCampaignPhase.PROPOSAL_REQUIRED
    proposal = DesignRepairProposal(
        "example:proposal:sizing-round-2",
        OWNER,
        paused.campaign_identity,
        paused.iterations[-1].candidate.identity,
        paused.attribution.identity,
        tuple(item.identity for item in paused.attribution.evidence),
        None,
        baseline_problem.candidates[0],
        2,
        ("l1-functional",),
        ProposalProvenance(
            "test-semantic-client",
            "1",
            (paused.iterations[-1].candidate.identity,),
        ),
    )

    completed = runner.resume(campaign, paused, proposal)

    assert completed.termination is DesignCampaignTermination.PASSED
    assert completed.iterations[-1].candidate.parent_candidate == baseline_candidate.reference()
    assert completed.iterations[-1].candidate.sizing_problem.identity == "example:sizing-problem:child"
    assert completed.iterations[-1].candidate.sizing_result.identity == "example:sizing-result:child"
    assert adapter.values[3].selected_candidate == baseline_problem.candidates[0].name
