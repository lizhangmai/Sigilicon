from __future__ import annotations

from dataclasses import replace

import pytest

from sigilicon.domain.circuit_design import (
    ARTIFACT_SCHEMA,
    DESIGN_DECISION_KIND,
    ArtifactMetadata,
    DesignDecision,
    DesignDecisionConclusion,
    EvidenceLevel,
    EvidenceRole,
)
from sigilicon.workflows.design_promotion import (
    PromotionChange,
    PromotionRequest,
    PromotionRequirements,
    compile_promotion_plan,
    promotion_plan_from_json,
    promotion_request_from_json,
)

from test_design_campaign import OWNER, POLICY, _attempt_artifacts
from sigilicon.domain.circuit_design import EvidenceConclusion


def _inputs():
    candidate, topology, evidence = _attempt_artifacts(EvidenceConclusion.SATISFIED)
    decision = DesignDecision(
        ArtifactMetadata(ARTIFACT_SCHEMA, DESIGN_DECISION_KIND, OWNER, "example:decision:promotion"),
        candidate.reference(),
        POLICY,
        EvidenceRole.DIAGNOSTIC,
        EvidenceLevel.L0,
        ("functional",),
        (evidence.reference(),),
        DesignDecisionConclusion.PASSED,
        "exact owner policy accepted the satisfied L0 evidence",
    )
    requirements = PromotionRequirements(
        OWNER,
        POLICY,
        EvidenceRole.DIAGNOSTIC,
        EvidenceLevel.L0,
        ("functional",),
        ("l0-functional",),
    )
    request = PromotionRequest(
        OWNER,
        candidate.identity,
        decision.identity,
        requirements,
        (
            PromotionChange(
                "topology",
                topology.reference(),
                "review the validated topology Candidate for ordinary Git promotion",
            ),
        ),
        ("human review has not yet approved any source change",),
    )
    return candidate, topology, evidence, decision, request


def test_promotion_plan_is_immutable_review_only_and_requires_accepted_evidence() -> None:
    candidate, topology, evidence, decision, request = _inputs()

    plan = compile_promotion_plan(
        candidate=candidate,
        artifacts=(topology, evidence),
        decision=decision,
        request=request,
    )

    assert plan.human_approval_required is True
    assert plan.writes_canonical_source is False
    assert promotion_plan_from_json(plan.canonical_json()) == plan
    assert promotion_request_from_json(request.canonical_json()) == request

    insufficient = replace(
        request,
        requirements=replace(request.requirements, role=EvidenceRole.QUALIFICATION),
    )
    with pytest.raises(ValueError, match="insufficient accepted evidence"):
        compile_promotion_plan(
            candidate=candidate,
            artifacts=(topology, evidence),
            decision=decision,
            request=insufficient,
        )


def test_promotion_plan_accepts_semantic_identity_and_rejects_cross_owner_evidence() -> None:
    candidate, topology, evidence, decision, request = _inputs()
    plan = compile_promotion_plan(
        candidate=candidate,
        artifacts=(topology, evidence),
        decision=decision,
        request=request,
    )

    forged = replace(plan, decision_identity="forged-decision")
    assert forged.decision_identity == "forged-decision"
    with pytest.raises(ValueError, match="evidence owner"):
        replace(
            plan,
            evidence=(replace(plan.evidence[0], owner="different-owner"),),
        )
