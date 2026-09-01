from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from sigilicon.domain.repository import Project

from sigilicon.domain.circuit_design import (
    ARTIFACT_SCHEMA,
    CIRCUIT_SIZING_PROBLEM_KIND,
    CIRCUIT_TOPOLOGY_KIND,
    DESIGN_CANDIDATE_KIND,
    DESIGN_BRIEF_KIND,
    DESIGN_DECISION_KIND,
    DESIGN_EVIDENCE_KIND,
    SOURCE_NETLIST_KIND,
    ArtifactMetadata,
    ArtifactReference,
    CircuitSizingProblem,
    DesignCandidate,
    DesignBrief,
    DesignDecision,
    DesignDecisionConclusion,
    DesignEvidence,
    EvidenceCompletion,
    EvidenceConclusion,
    EvidenceLevel,
    EvidenceProducer,
    EvidenceProducerKind,
    EvidenceRole,
    NamedQuantity,
    SizingBudget,
    SizingCandidate,
    SizingCondition,
    SizingParameter,
    TopologyOrigin,
    design_brief_from_json,
    design_evidence_from_json,
    exact_quantity,
    validate_design_candidate,
    validate_design_decision,
)
from sigilicon.domain.design import load_design_spec
from sigilicon.workflows.design_frontend import SourceAuthoredTopologyAdapter


def test_design_artifacts_use_explicit_owner_scoped_ids() -> None:
    first = ArtifactReference(
        "example",
        CIRCUIT_TOPOLOGY_KIND,
        "example.inv.topology.baseline",
        None,
    )
    second = ArtifactReference(
        "example",
        CIRCUIT_TOPOLOGY_KIND,
        "example.inv.topology.repaired",
        None,
    )

    assert first != second
    assert first.identity != second.identity
    assert ArtifactMetadata(
        ARTIFACT_SCHEMA,
        CIRCUIT_TOPOLOGY_KIND,
        "example",
        "example.inv.topology.baseline",
    ).artifact_id == first.identity


def test_explicit_ids_do_not_substitute_different_typed_records(
    project_factory,
) -> None:
    project_root, design_path = project_factory()
    design = load_design_spec(
        design_path, project=Project.from_project_root(project_root)
    )
    topology = SourceAuthoredTopologyAdapter().read(design, owner="example")
    changed = replace(
        topology,
        instances=(replace(topology.instances[0], master="different_master"),),
    )
    source = ArtifactReference(
        "example",
        SOURCE_NETLIST_KIND,
        topology.source_snapshot_identity,
        None,
    )
    candidate = DesignCandidate(
        ArtifactMetadata(
            ARTIFACT_SCHEMA,
            DESIGN_CANDIDATE_KIND,
            "example",
            "example:candidate:substitution-check",
        ),
        topology.design,
        source,
        None,
        (),
        topology.reference(),
        None,
        None,
        (),
        None,
        topology.provenance,
    )

    assert ArtifactReference("example", "kind.one", "first", None) != ArtifactReference(
        "example", "kind.one", "second", None
    )
    with pytest.raises(ValueError, match="same artifact ID.*different typed records"):
        validate_design_candidate(candidate, (topology, changed))


def test_source_authored_topology_is_immutable_canonical_and_strict(
    project_factory,
) -> None:
    project_root, design_path = project_factory()
    design = load_design_spec(
        design_path, project=Project.from_project_root(project_root)
    )

    topology = SourceAuthoredTopologyAdapter().read(
        design,
        owner="example",
        instance_roles=(
            ("MP0", "pull-up"),
            ("MN0", "pull-down"),
        ),
    )

    assert topology.metadata.kind == CIRCUIT_TOPOLOGY_KIND
    assert topology.metadata.owner == "example"
    assert topology.design == "inv"
    assert topology.origin is TopologyOrigin.SOURCE_AUTHORED
    assert tuple((port.name, port.role) for port in topology.ports) == (
        ("IN", "signal"),
        ("OUT", "signal"),
        ("VDD", "primary-supply"),
        ("VSS", "ground-supply"),
    )
    assert tuple((item.name, item.master, item.role) for item in topology.instances) == (
        ("MP0", "pch_mac", "pull-up"),
        ("MN0", "nch_mac", "pull-down"),
    )
    assert topology.identity == "example:source-topology:ip/example/inv/design.toml"

    with pytest.raises(FrozenInstanceError):
        topology.design = "changed"  # type: ignore[misc]



def test_source_authored_topology_rejects_owner_and_role_injection(
    project_factory,
) -> None:
    project_root, design_path = project_factory()
    design = load_design_spec(
        design_path, project=Project.from_project_root(project_root)
    )
    adapter = SourceAuthoredTopologyAdapter()

    with pytest.raises(ValueError, match="owner"):
        adapter.read(design, owner="example; rm -rf work")
    with pytest.raises(ValueError, match="instance role"):
        adapter.read(
            design,
            owner="example",
            instance_roles=(("MP0", "../../foreign"),),
        )
    with pytest.raises(ValueError, match="unknown topology instance"):
        adapter.read(
            design,
            owner="example",
            instance_roles=(("MISSING", "load"),),
        )


def test_candidate_evidence_and_decision_bind_exact_identities(
    project_factory,
) -> None:
    project_root, design_path = project_factory()
    design = load_design_spec(
        design_path, project=Project.from_project_root(project_root)
    )
    topology = SourceAuthoredTopologyAdapter().read(design, owner="example")
    source = ArtifactReference(
        "example",
        SOURCE_NETLIST_KIND,
        topology.source_snapshot_identity,
        None,
    )
    specification = ArtifactReference("example", "spec.sizing", "sizing-specification", None)
    problem = CircuitSizingProblem(
        ArtifactMetadata(ARTIFACT_SCHEMA, CIRCUIT_SIZING_PROBLEM_KIND, "example", "example:sizing-problem:candidate"),
        "inv",
        topology.reference(),
        specification,
        "tb_inv_sizing",
        EvidenceRole.DIAGNOSTIC,
        (
            SizingParameter(
                "wn",
                "m",
                exact_quantity("1e-7", "m"),
                exact_quantity("1e-7", "m"),
                (exact_quantity("1e-7", "m"),),
                1,
                None,
            ),
        ),
        (),
        (SizingCondition("nominal", ()),),
        (
            SizingCandidate(
                "minimum",
                (NamedQuantity("wn", exact_quantity("1e-7", "m")),),
                "only source-authored point",
            ),
        ),
        SizingBudget(1, None),
    )
    evidence = DesignEvidence(
        ArtifactMetadata(ARTIFACT_SCHEMA, DESIGN_EVIDENCE_KIND, "example", "example:evidence:candidate"),
        problem.reference(),
        source,
        specification,
        EvidenceRole.DIAGNOSTIC,
        EvidenceLevel.L1,
        ("nominal",),
        EvidenceProducer(EvidenceProducerKind.DETERMINISTIC_CHECK, "source-check", "1"),
        EvidenceCompletion("source-check", True, True, 0),
        EvidenceConclusion.SATISFIED,
        (),
        "the source-authored diagnostic contract is internally consistent",
    )
    candidate = DesignCandidate(
        ArtifactMetadata(ARTIFACT_SCHEMA, DESIGN_CANDIDATE_KIND, "example", "example:candidate:frontend"),
        "inv",
        source,
        None,
        (specification,),
        topology.reference(),
        problem.reference(),
        None,
        (evidence.reference(),),
        None,
        topology.provenance,
    )
    decision = DesignDecision(
        ArtifactMetadata(ARTIFACT_SCHEMA, DESIGN_DECISION_KIND, "example", "example:decision:frontend"),
        candidate.reference(),
        specification,
        EvidenceRole.DIAGNOSTIC,
        EvidenceLevel.L1,
        ("nominal",),
        (evidence.reference(),),
        DesignDecisionConclusion.PASSED,
        "the one diagnostic gate is satisfied",
    )

    validated = validate_design_candidate(candidate, (topology, problem, evidence))
    validate_design_decision(decision, candidate, (evidence,))

    assert validated.candidate_identity == candidate.identity
    assert "candidate_identity" not in candidate.canonical_json()

    drifted = replace(
        candidate,
        topology=ArtifactReference("example", CIRCUIT_TOPOLOGY_KIND, "forged-topology", None),
    )
    with pytest.raises(ValueError, match="identity-matched"):
        validate_design_candidate(drifted, (topology, problem, evidence))
    with pytest.raises(ValueError, match="policy is not a Candidate specification"):
        validate_design_decision(
            replace(
                decision,
                policy=ArtifactReference("example", "spec.sizing", "forged-policy", None),
            ),
            candidate,
            (evidence,),
        )


def test_cross_owner_and_fake_authority_fail_closed(project_factory) -> None:
    project_root, design_path = project_factory()
    design = load_design_spec(
        design_path, project=Project.from_project_root(project_root)
    )
    topology = SourceAuthoredTopologyAdapter().read(design, owner="example")
    source = ArtifactReference(
        "example",
        SOURCE_NETLIST_KIND,
        topology.source_snapshot_identity,
        None,
    )
    specification = ArtifactReference("example", "spec.qualification", "qualification-specification", None)

    with pytest.raises(ValueError, match="cross-owner topology"):
        DesignCandidate(
            ArtifactMetadata(ARTIFACT_SCHEMA, DESIGN_CANDIDATE_KIND, "example", "example:candidate:cross-owner"),
            "inv",
            source,
            None,
            (specification,),
            ArtifactReference("foreign", CIRCUIT_TOPOLOGY_KIND, topology.identity, None),
            None,
            None,
            (),
            None,
            topology.provenance,
        )

    with pytest.raises(ValueError, match="offline/fake"):
        DesignEvidence(
            ArtifactMetadata(ARTIFACT_SCHEMA, DESIGN_EVIDENCE_KIND, "example", "example:evidence:fake-signoff"),
            topology.reference(),
            source,
            specification,
            EvidenceRole.QUALIFICATION,
            EvidenceLevel.L3,
            ("nominal",),
            EvidenceProducer(EvidenceProducerKind.FAKE, "fixture", "1"),
            EvidenceCompletion("fixture", False, False, None),
            EvidenceConclusion.SATISFIED,
            (),
            "fake success",
        )

    non_conclusion = DesignEvidence(
        ArtifactMetadata(ARTIFACT_SCHEMA, DESIGN_EVIDENCE_KIND, "example", "example:evidence:fake-diagnostic"),
        topology.reference(),
        source,
        specification,
        EvidenceRole.QUALIFICATION,
        EvidenceLevel.L3,
        ("nominal",),
        EvidenceProducer(EvidenceProducerKind.OFFLINE, "offline", "1"),
        EvidenceCompletion("offline", False, False, None),
        EvidenceConclusion.NOT_EVALUATED,
        (),
        "real backend was not run",
    )
    assert non_conclusion.conclusion is EvidenceConclusion.NOT_EVALUATED

    with pytest.raises(ValueError, match="invalid artifact reference kind"):
        ArtifactReference("example", "../../source", "unsafe-kind-fixture", None)
    with pytest.raises(ValueError, match="canonical"):
        design_evidence_from_json(non_conclusion.canonical_json().rstrip())


def test_design_brief_keeps_confirmed_and_provisional_statements_distinct() -> None:
    brief = DesignBrief(
        ArtifactMetadata(ARTIFACT_SCHEMA, DESIGN_BRIEF_KIND, "example", "example:brief:inv"),
        "inv",
        ("preserve the source-authored inverter interface",),
        ("use the existing owner qualification specification",),
        ("a wider device may improve the diagnostic edge",),
        ("prefer the smallest evidence-supported candidate",),
    )

    assert design_brief_from_json(brief.canonical_json()) == brief
    assert brief.confirmed_requirements != brief.provisional_assumptions
    assert "passed" not in brief.canonical_json()
