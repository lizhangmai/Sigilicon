from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

import pytest

from sigilicon.domain.circuit_design import (
    ARTIFACT_SCHEMA,
    CIRCUIT_TOPOLOGY_KIND,
    DESIGN_CANDIDATE_KIND,
    DESIGN_EVIDENCE_KIND,
    ArtifactMetadata,
    CircuitInstance,
    CircuitPort,
    CircuitTopologyProposal,
    EvidenceConclusion,
    EvidenceLevel,
    EvidenceRole,
    PortDirection,
    ProposalProvenance,
    TopologyOrigin,
    design_artifact_from_json,
)
from sigilicon.domain.physical_verification import (
    CheckedLayoutIdentity,
    CheckedSourceIdentity,
    DrcEvidence,
    LvsEvidence,
    PhysicalVerificationStatus,
    VerificationCompletion,
    drc_evidence_id,
    lvs_evidence_id,
)
from sigilicon.domain.post_layout import (
    DerivedArtifactIdentity,
    PexEvidence,
    PexStatus,
    pex_evidence_id,
)
from sigilicon.flow import (
    ActionContext,
    AdapterExecution,
    InputArtifact,
    builtin_registry,
)
from sigilicon.flow.circuit_design import PHYSICAL_DESIGN_OBSERVATION_ACTION
from sigilicon.workflows.design_physical import PhysicalDesignObservationAdapter


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _context(tmp_path: Path, *, offline: bool = False) -> ActionContext:
    source_text = ".SUBCKT pilot left right\n.ENDS pilot\n"
    source_identity = "owner:pilot:source"
    policy_text = "schema = 1\nowner = \"owner\"\n"
    policy_identity = "owner:policy.toml:verification-policy"
    topology = CircuitTopologyProposal(
        ArtifactMetadata(ARTIFACT_SCHEMA, CIRCUIT_TOPOLOGY_KIND, "owner", "owner:topology:physical-observation"),
        "pilot",
        source_identity,
        TopologyOrigin.SOURCE_AUTHORED,
        (
            CircuitPort("left", PortDirection.INPUT_OUTPUT, "signal"),
            CircuitPort("right", PortDirection.INPUT_OUTPUT, "signal"),
        ),
        (),
        (CircuitInstance("cap0", "cfmom_2t", ("left", "right"), (), "capacitor"),),
        (),
        ProposalProvenance("owner.source", "1"),
    )
    layout = CheckedLayoutIdentity(
        "layout-fixture",
        "plan-fixture",
        "result-fixture",
        "owner",
        "pilot",
        "source-fixture",
        "policy-fixture",
        "gdsii",
    )
    source = CheckedSourceIdentity(source_identity, "owner", "pilot")
    completion = VerificationCompletion(
        "sigilicon.offline-physical-verification" if offline else "calibre@2025.2",
        not offline,
        not offline,
        None if offline else 0,
    )
    status = (
        PhysicalVerificationStatus.BACKEND_UNAVAILABLE
        if offline
        else PhysicalVerificationStatus.CLEAN
    )
    drc = DrcEvidence(status, layout, completion, (), "DRC observation")
    lvs = LvsEvidence(status, layout, source, completion, (), "LVS observation")
    pex = PexEvidence(
        PexStatus.BACKEND_UNAVAILABLE if offline else PexStatus.EXTRACTED,
        layout,
        source,
        completion,
        None
        if offline
        else DerivedArtifactIdentity("parasitics", "netlist.pex", "parasitics-fixture"),
        "PEX observation",
    )
    registry = builtin_registry()
    action = registry.action(PHYSICAL_DESIGN_OBSERVATION_ACTION)
    files = {
        "topology": _write(tmp_path / "topology.json", topology.canonical_json()),
        "source": _write(tmp_path / "source.cdl", source_text),
        "verification-policy": _write(tmp_path / "policy.toml", policy_text),
        "drc": _write(tmp_path / "drc.json", drc.canonical_json()),
        "lvs": _write(tmp_path / "lvs.json", lvs.canonical_json()),
        "pex": _write(tmp_path / "pex.json", pex.canonical_json()),
    }
    kinds = {
        "topology": CIRCUIT_TOPOLOGY_KIND,
        "source": "netlist.canonical-source",
        "verification-policy": "policy.physical-verification",
        "drc": "evidence.drc",
        "lvs": "evidence.lvs",
        "pex": "evidence.pex",
    }
    qualifiers = {
        "source": {"owner": "owner", "name": "pilot", "source-identity": source_identity},
        "verification-policy": {
            "owner": "owner",
            "name": "pilot",
            "policy-identity": policy_identity,
        },
        "drc": {"evidence-identity": drc_evidence_id(drc)},
        "lvs": {"evidence-identity": lvs_evidence_id(lvs)},
        "pex": {"evidence-identity": pex_evidence_id(pex)},
    }
    inputs = MappingProxyType(
        {
            role: InputArtifact(role, kinds[role], path, "fixture", qualifiers.get(role, {}))
            for role, path in files.items()
        }
    )
    return ActionContext(
        "observe",
        action,
        tmp_path,
        tmp_path / "work",
        tmp_path / "outputs",
        tmp_path / "logs",
        inputs,
        MappingProxyType(
            {"role": "regression", "level": "l1", "scope": ("pilot",)}
        ),
        MappingProxyType({}),
        MappingProxyType({}),
        MappingProxyType({}),
    )


def test_real_physical_evidence_normalizes_to_candidate_bound_design_evidence(
    tmp_path: Path,
) -> None:
    adapter = PhysicalDesignObservationAdapter()
    context = _context(tmp_path)

    assert adapter.validate_inputs(context) == ()
    execution = adapter.execute(context)
    assert execution == AdapterExecution.succeeded()
    result = adapter.collect_result(context, execution)
    values = {
        artifact.role: design_artifact_from_json(artifact.path.read_text(encoding="utf-8"))
        for artifact in result.artifacts
    }

    assert values["candidate"].metadata.kind == DESIGN_CANDIDATE_KIND
    assert {values[role].metadata.kind for role in ("drc", "lvs", "pex")} == {
        DESIGN_EVIDENCE_KIND
    }
    assert all(
        values[role].conclusion is EvidenceConclusion.SATISFIED
        for role in ("drc", "lvs", "pex")
    )
    assert all(values[role].role is EvidenceRole.REGRESSION for role in ("drc", "lvs", "pex"))
    assert all(values[role].level is EvidenceLevel.L1 for role in ("drc", "lvs", "pex"))


def test_offline_physical_evidence_stays_non_conclusive(tmp_path: Path) -> None:
    adapter = PhysicalDesignObservationAdapter()
    context = _context(tmp_path, offline=True)

    adapter.execute(context)
    result = adapter.collect_result(context, AdapterExecution.succeeded())
    values = {
        artifact.role: design_artifact_from_json(artifact.path.read_text(encoding="utf-8"))
        for artifact in result.artifacts
    }

    assert all(
        values[role].conclusion is EvidenceConclusion.BACKEND_UNAVAILABLE
        for role in ("drc", "lvs", "pex")
    )


def test_physical_observation_rejects_qualification_authority_and_identity_drift(
    tmp_path: Path,
) -> None:
    adapter = PhysicalDesignObservationAdapter()
    context = _context(tmp_path)
    privileged = ActionContext(
        **{
            **context.__dict__,
            "action_config": MappingProxyType(
                {"role": "qualification", "level": "l3", "scope": ("pilot",)}
            ),
        }
    )
    assert any("diagnostic or regression" in item for item in adapter.validate_inputs(privileged))

    context.input("source").path.write_text("changed\n", encoding="utf-8")
    assert any("source" in item for item in adapter.validate_inputs(context))


def test_physical_observation_rejects_invalid_typed_evidence_conclusion(
    tmp_path: Path,
) -> None:
    adapter = PhysicalDesignObservationAdapter()
    context = _context(tmp_path)
    evidence_path = context.input("drc").path
    evidence_path.write_text(
        evidence_path.read_text(encoding="utf-8").replace(
            '"status": "clean"',
            '"status": "violated"',
        ),
        encoding="utf-8",
    )

    assert any("violated DRC" in item for item in adapter.validate_inputs(context))
