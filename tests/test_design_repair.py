from __future__ import annotations

from dataclasses import replace

import pytest

from sigilicon.domain.circuit_design import (
    ARTIFACT_SCHEMA,
    CIRCUIT_SIZING_PROBLEM_KIND,
    CIRCUIT_SIZING_RESULT_KIND,
    CIRCUIT_TOPOLOGY_KIND,
    DESIGN_CANDIDATE_KIND,
    DESIGN_EVIDENCE_KIND,
    ArtifactMetadata,
    ArtifactReference,
    CircuitInstance,
    CircuitPort,
    CircuitSizingProblem,
    CircuitSizingResult,
    CircuitTopologyProposal,
    DesignCandidate,
    DesignEvidence,
    EvidenceCompletion,
    EvidenceConclusion,
    EvidenceFinding,
    EvidenceLevel,
    EvidenceProducer,
    EvidenceProducerKind,
    EvidenceRole,
    NamedQuantity,
    PortDirection,
    ProposalProvenance,
    SizingBudget,
    SizingCandidate,
    SizingCandidateOutcome,
    SizingCandidateResult,
    SizingCondition,
    SizingMatchingGroup,
    SizingParameter,
    SizingTermination,
    StateSemantic,
    TopologyOrigin,
    exact_quantity,
)
from sigilicon.workflows.design_repair import (
    RepairCompileDecision,
    SizingRepairPolicy,
    TopologyRepairPolicy,
    compile_sizing_repair,
    compile_topology_repair,
    sizing_repair_plan_from_json,
    topology_repair_plan_from_json,
)


OWNER = "example"
SOURCE = ArtifactReference(OWNER, "source.netlist", "1" * 64, None)
POLICY = ArtifactReference(OWNER, "spec.design-policy", "2" * 64, None)


def _topology(*, master: str = "INV", origin: TopologyOrigin = TopologyOrigin.SOURCE_AUTHORED) -> CircuitTopologyProposal:
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
        (CircuitInstance("X0", master, ("IN", "OUT"), (), "gain-stage"),),
        (StateSemantic("functional", "preserve the owner-authored functional state"),),
        ProposalProvenance("test-proposal", "1"),
    )


def _evidence(subject: ArtifactReference, *, conclusion: EvidenceConclusion = EvidenceConclusion.VIOLATED) -> DesignEvidence:
    return DesignEvidence(
        ArtifactMetadata(ARTIFACT_SCHEMA, DESIGN_EVIDENCE_KIND, OWNER),
        subject,
        SOURCE,
        POLICY,
        EvidenceRole.DIAGNOSTIC,
        EvidenceLevel.L0,
        ("functional",),
        EvidenceProducer(EvidenceProducerKind.EXECUTED_BACKEND, "test-backend", "1"),
        EvidenceCompletion("test-backend", True, True, 0),
        conclusion,
        (
            ()
            if conclusion is EvidenceConclusion.SATISFIED
            else (EvidenceFinding("functional-failure", 1, "typed failure"),)
        ),
        "typed evidence for repair compilation",
    )


def _candidate(
    topology: CircuitTopologyProposal,
    evidence: DesignEvidence,
    *,
    problem: CircuitSizingProblem | None = None,
    result: CircuitSizingResult | None = None,
) -> DesignCandidate:
    return DesignCandidate(
        ArtifactMetadata(ARTIFACT_SCHEMA, DESIGN_CANDIDATE_KIND, OWNER),
        "pilot",
        SOURCE,
        None,
        (POLICY, *((problem.specification,) if problem is not None else ())),
        topology.reference(),
        None if problem is None else problem.reference(),
        None if result is None else result.reference(),
        (evidence.reference(),),
        None,
        ProposalProvenance("test-candidate", "1"),
    )


def _problem(topology: CircuitTopologyProposal) -> CircuitSizingProblem:
    values = (exact_quantity("1e-7", "m"), exact_quantity("2e-7", "m"))
    parameters = tuple(
        SizingParameter(name, "m", values[0], values[1], values, 1, "matched-pair")
        for name in ("wn", "wp")
    )
    candidates = tuple(
        SizingCandidate(
            name,
            tuple(NamedQuantity(parameter, value) for parameter in ("wn", "wp")),
            name,
        )
        for name, value in (("minimum", values[0]), ("baseline", values[1]))
    )
    return CircuitSizingProblem(
        ArtifactMetadata(ARTIFACT_SCHEMA, CIRCUIT_SIZING_PROBLEM_KIND, OWNER),
        "pilot",
        topology.reference(),
        ArtifactReference(OWNER, "spec.sizing", "3" * 64, None),
        "tb_pilot",
        EvidenceRole.DIAGNOSTIC,
        parameters,
        (SizingMatchingGroup("matched-pair", ("wn", "wp")),),
        (SizingCondition("nominal", ()),),
        candidates,
        SizingBudget(4, 7),
    )


def _result(problem: CircuitSizingProblem) -> CircuitSizingResult:
    return CircuitSizingResult(
        ArtifactMetadata(ARTIFACT_SCHEMA, CIRCUIT_SIZING_RESULT_KIND, OWNER),
        problem.reference(),
        EvidenceRole.DIAGNOSTIC,
        "test-sweep",
        "1",
        SizingTermination.COMPLETED,
        (
            SizingCandidateResult(
                "baseline", SizingCandidateOutcome.SATISFIED, 1, ()
            ),
        ),
        "baseline",
        1,
        7,
    )


def test_topology_repair_compiler_binds_parent_evidence_policy_and_regressions() -> None:
    parent = _topology()
    evidence = _evidence(parent.reference())
    candidate = _candidate(parent, evidence)
    proposed = _topology(master="BUF", origin=TopologyOrigin.PROPOSED)
    policy = TopologyRepairPolicy(
        OWNER,
        "topology-policy",
        ("BUF",),
        1,
        ("l0-interface", "l1-functional"),
    )

    plan = compile_topology_repair(
        candidate=candidate,
        parent=parent,
        proposed=proposed,
        evidence=(evidence,),
        policy=policy,
        required_regressions=("l0-interface", "l1-functional"),
    )

    assert plan.decision is RepairCompileDecision.ACCEPTED
    assert plan.changed_instances == ("X0",)
    assert plan.proposed_topology == proposed
    assert topology_repair_plan_from_json(plan.canonical_json()) == plan

    drifted = replace(
        proposed,
        ports=(CircuitPort("OTHER", PortDirection.INPUT, "signal"),),
    )
    rejected = compile_topology_repair(
        candidate=candidate,
        parent=parent,
        proposed=drifted,
        evidence=(evidence,),
        policy=policy,
        required_regressions=("l0-interface", "l1-functional"),
    )
    assert rejected.decision is RepairCompileDecision.ILLEGAL_CHANGE


def test_sizing_repair_compiler_checks_discrete_range_matching_and_budget() -> None:
    topology = _topology()
    problem = _problem(topology)
    result = _result(problem)
    evidence = _evidence(result.reference())
    candidate = _candidate(topology, evidence, problem=problem, result=result)
    policy = SizingRepairPolicy(
        OWNER,
        "sizing-policy",
        ("wn", "wp"),
        2,
        ("l1-functional",),
    )
    proposed = problem.candidates[0]

    plan = compile_sizing_repair(
        candidate=candidate,
        problem=problem,
        parent_result=result,
        proposed=proposed,
        requested_evaluations=2,
        evidence=(evidence,),
        policy=policy,
        required_regressions=("l1-functional",),
    )

    assert plan.decision is RepairCompileDecision.ACCEPTED
    assert plan.proposed_candidate == proposed
    assert sizing_repair_plan_from_json(plan.canonical_json()) == plan

    unmatched = SizingCandidate(
        "unmatched",
        (
            NamedQuantity("wn", exact_quantity("1e-7", "m")),
            NamedQuantity("wp", exact_quantity("2e-7", "m")),
        ),
        "break matching",
    )
    rejected = compile_sizing_repair(
        candidate=candidate,
        problem=problem,
        parent_result=result,
        proposed=unmatched,
        requested_evaluations=2,
        evidence=(evidence,),
        policy=policy,
        required_regressions=("l1-functional",),
    )
    assert rejected.decision is RepairCompileDecision.ILLEGAL_CHANGE


def test_repair_compilers_fail_closed_on_owner_or_evidence_drift() -> None:
    parent = _topology()
    evidence = _evidence(parent.reference())
    candidate = _candidate(parent, evidence)
    policy = TopologyRepairPolicy(
        "other",
        "topology-policy",
        ("BUF",),
        1,
        ("l0-interface",),
    )
    plan = compile_topology_repair(
        candidate=candidate,
        parent=parent,
        proposed=_topology(master="BUF", origin=TopologyOrigin.PROPOSED),
        evidence=(evidence,),
        policy=policy,
        required_regressions=("l0-interface",),
    )
    assert plan.decision is RepairCompileDecision.INVALID_IDENTITY

    with pytest.raises(ValueError, match="unknown"):
        topology_repair_plan_from_json(
            plan.canonical_json().replace('\n}', ',\n  "model_passed": true\n}')
        )
