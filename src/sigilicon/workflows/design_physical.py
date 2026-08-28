"""Fail-closed projection of receipt-bound physical evidence into Design artifacts."""

from __future__ import annotations

import re
from typing import Iterable

from sigilicon.artifacts import read_nofollow_text
from sigilicon.domain.circuit_design import (
    ARTIFACT_SCHEMA,
    DESIGN_CANDIDATE_KIND,
    DESIGN_EVIDENCE_KIND,
    SOURCE_NETLIST_KIND,
    ArtifactMetadata,
    ArtifactReference,
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
    ProposalProvenance,
    circuit_topology_from_json,
    design_artifact_from_json,
    validate_design_candidate,
)
from sigilicon.domain.physical_verification import (
    DrcEvidence,
    LvsEvidence,
    PhysicalVerificationStatus,
    drc_evidence_id,
    drc_evidence_from_json,
    lvs_evidence_id,
    lvs_evidence_from_json,
)
from sigilicon.domain.post_layout import (
    PexEvidence,
    PexStatus,
    pex_evidence_from_json,
    pex_evidence_id,
)
from sigilicon.flow.model import (
    ActionContext,
    AdapterExecution,
    CollectedActionResult,
    FlowExecutionError,
    ProducedArtifact,
)


_PHYSICAL_STATUS = {
    PhysicalVerificationStatus.CLEAN: EvidenceConclusion.SATISFIED,
    PhysicalVerificationStatus.VIOLATED: EvidenceConclusion.VIOLATED,
    PhysicalVerificationStatus.UNSUPPORTED: EvidenceConclusion.UNSUPPORTED,
    PhysicalVerificationStatus.BACKEND_UNAVAILABLE: EvidenceConclusion.BACKEND_UNAVAILABLE,
    PhysicalVerificationStatus.EXECUTION_FAILED: EvidenceConclusion.EXECUTION_FAILED,
}
_PEX_STATUS = {
    PexStatus.EXTRACTED: EvidenceConclusion.SATISFIED,
    PexStatus.UNSUPPORTED: EvidenceConclusion.UNSUPPORTED,
    PexStatus.BACKEND_UNAVAILABLE: EvidenceConclusion.BACKEND_UNAVAILABLE,
    PexStatus.EXECUTION_FAILED: EvidenceConclusion.EXECUTION_FAILED,
}


def _input_identity(context: ActionContext, role: str) -> str:
    dimension = "source-identity" if role == "source" else "policy-identity"
    value = context.input(role).qualifiers.get(dimension)
    if not isinstance(value, str) or not value:
        raise FlowExecutionError(f"{role} input needs a semantic identity")
    return value


def _reference(owner: str, kind: str, identity: str) -> ArtifactReference:
    return ArtifactReference(owner, kind, identity, None)


def _finding_code(stage: str, identity: str) -> str:
    token = re.sub(r"[^a-z0-9]+", "-", identity.lower()).strip("-")
    return f"{stage}.{token[:48] or 'finding'}"


def _producer(stage: str, backend: str) -> EvidenceProducer:
    kind = (
        EvidenceProducerKind.OFFLINE
        if backend.startswith("sigilicon.offline")
        else EvidenceProducerKind.EXECUTED_BACKEND
    )
    return EvidenceProducer(kind, f"physical-verification.{stage}", backend)


def _completion(stage: str, value: object) -> EvidenceCompletion:
    return EvidenceCompletion(
        f"physical-verification.{stage}",
        value.executed,
        value.report_parsed,
        value.exit_code,
    )


class PhysicalDesignObservationAdapter:
    """Normalize DRC/LVS/PEX only as diagnostic or regression evidence.

    This Adapter deliberately cannot mint qualification or signoff authority. The
    authoritative Calibre parsers remain responsible for clean/violation/extraction
    conclusions; this layer only binds those conclusions to one Candidate identity.
    """

    @staticmethod
    def _policy(context: ActionContext) -> tuple[EvidenceRole, EvidenceLevel, tuple[str, ...]]:
        if context.adapter_config:
            raise FlowExecutionError("physical observation Adapter config must be empty")
        if set(context.action_config) != {"role", "level", "scope"}:
            raise FlowExecutionError(
                "physical observation config requires exactly role, level, and scope"
            )
        try:
            role = EvidenceRole(context.action_config["role"])
            level = EvidenceLevel(context.action_config["level"])
        except (TypeError, ValueError) as exc:
            raise FlowExecutionError("physical observation role or level is invalid") from exc
        scope_value = context.action_config["scope"]
        if not isinstance(scope_value, (tuple, list)):
            raise FlowExecutionError("physical observation scope must be an array")
        scope = tuple(scope_value)
        if (
            role not in {EvidenceRole.DIAGNOSTIC, EvidenceRole.REGRESSION}
            or level not in {EvidenceLevel.L0, EvidenceLevel.L1, EvidenceLevel.L2}
        ):
            raise FlowExecutionError(
                "physical observation is limited to diagnostic or regression L0-L2 evidence"
            )
        if (
            not scope
            or scope != tuple(sorted(set(scope)))
            or any(not isinstance(item, str) for item in scope)
        ):
            raise FlowExecutionError("physical observation scope must be non-empty and sorted")
        return role, level, scope

    @staticmethod
    def _typed_inputs(
        context: ActionContext,
    ) -> tuple[CircuitTopologyProposal, DrcEvidence, LvsEvidence, PexEvidence]:
        topology = circuit_topology_from_json(read_nofollow_text(context.input("topology").path))
        drc = drc_evidence_from_json(read_nofollow_text(context.input("drc").path))
        lvs = lvs_evidence_from_json(read_nofollow_text(context.input("lvs").path))
        pex = pex_evidence_from_json(read_nofollow_text(context.input("pex").path))
        for role, evidence_id in (
            ("drc", drc_evidence_id(drc)),
            ("lvs", lvs_evidence_id(lvs)),
            ("pex", pex_evidence_id(pex)),
        ):
            if context.input(role).qualifiers.get("evidence-identity") != evidence_id:
                raise FlowExecutionError(
                    f"physical observation {role.upper()} evidence identity drift"
                )
        if drc.layout != lvs.layout or drc.layout != pex.layout or lvs.source != pex.source:
            raise FlowExecutionError("physical observation evidence identity drift")
        source_input = context.input("source")
        policy_input = context.input("verification-policy")
        source_identity = _input_identity(context, "source")
        policy_identity = _input_identity(context, "verification-policy")
        expected_owner = topology.metadata.owner
        try:
            source_text = read_nofollow_text(source_input.path)
            declaration = re.search(
                rf"(?im)^\s*\.?subckt\s+{re.escape(topology.design)}\s+(?P<ports>[^\r\n]+)$",
                source_text,
            )
            if declaration is None:
                raise ValueError(f"source has no subcircuit {topology.design!r}")
            source_ports = tuple(declaration.group("ports").split())
        except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
            raise FlowExecutionError(
                f"physical observation source structure is invalid: {exc}"
            ) from exc
        if (
            source_ports != tuple(port.name for port in topology.ports)
            or topology.source_snapshot_identity != source_identity
            or lvs.source.artifact_identity != source_identity
            or lvs.source.owner != expected_owner
            or lvs.source.name != topology.design
            or drc.layout.owner != expected_owner
            or drc.layout.name != topology.design
            or source_input.qualifiers.get("owner") != expected_owner
            or source_input.qualifiers.get("name") != topology.design
            or source_input.qualifiers.get("source-identity") != source_identity
            or policy_input.qualifiers.get("owner") != expected_owner
            or policy_input.qualifiers.get("policy-identity") != policy_identity
        ):
            raise FlowExecutionError("physical observation source or owner identity drift")
        return topology, drc, lvs, pex

    @staticmethod
    def _evidence(
        *,
        topology: CircuitTopologyProposal,
        source: ArtifactReference,
        specification: ArtifactReference,
        role: EvidenceRole,
        level: EvidenceLevel,
        scope: tuple[str, ...],
        stage: str,
        physical: DrcEvidence | LvsEvidence | PexEvidence,
    ) -> DesignEvidence:
        if isinstance(physical, PexEvidence):
            conclusion = _PEX_STATUS[physical.status]
            raw_findings: Iterable[tuple[str, int]] = ()
        elif isinstance(physical, DrcEvidence):
            conclusion = _PHYSICAL_STATUS[physical.status]
            raw_findings = ((item.rule, item.count) for item in physical.violations)
        else:
            conclusion = _PHYSICAL_STATUS[physical.status]
            raw_findings = ((item.category, item.count) for item in physical.mismatches)
        findings = tuple(
            EvidenceFinding(
                _finding_code(stage, identity),
                count,
                f"{stage.upper()} reported {identity}",
            )
            for identity, count in sorted(raw_findings)
        )
        physical_id = (
            pex_evidence_id(physical)
            if isinstance(physical, PexEvidence)
            else drc_evidence_id(physical)
            if isinstance(physical, DrcEvidence)
            else lvs_evidence_id(physical)
        )
        return DesignEvidence(
            ArtifactMetadata(
                ARTIFACT_SCHEMA,
                DESIGN_EVIDENCE_KIND,
                topology.metadata.owner,
                f"{topology.metadata.owner}:design-evidence:{physical_id}",
            ),
            topology.reference(),
            source,
            specification,
            role,
            level,
            scope,
            _producer(stage, physical.completion.backend),
            _completion(stage, physical.completion),
            conclusion,
            findings,
            f"receipt-bound {stage.upper()} evidence normalized without changing authority",
        )

    def _compile(
        self,
        context: ActionContext,
    ) -> tuple[DesignCandidate, DesignEvidence, DesignEvidence, DesignEvidence]:
        role, level, scope = self._policy(context)
        topology, drc, lvs, pex = self._typed_inputs(context)
        owner = topology.metadata.owner
        source = _reference(owner, SOURCE_NETLIST_KIND, _input_identity(context, "source"))
        specification = _reference(
            owner,
            context.input("verification-policy").kind,
            _input_identity(context, "verification-policy"),
        )
        evidence = tuple(
            self._evidence(
                topology=topology,
                source=source,
                specification=specification,
                role=role,
                level=level,
                scope=scope,
                stage=stage,
                physical=physical,
            )
            for stage, physical in (("drc", drc), ("lvs", lvs), ("pex", pex))
        )
        evidence_ids = (
            drc_evidence_id(drc),
            lvs_evidence_id(lvs),
            pex_evidence_id(pex),
        )
        candidate = DesignCandidate(
            ArtifactMetadata(
                ARTIFACT_SCHEMA,
                DESIGN_CANDIDATE_KIND,
                owner,
                f"{owner}:physical-candidate:{':'.join(evidence_ids)}",
            ),
            topology.design,
            source,
            None,
            (specification,),
            topology.reference(),
            None,
            None,
            tuple(item.reference() for item in evidence),
            None,
            ProposalProvenance(
                "sigilicon.physical-observation",
                "1",
                tuple(
                    sorted(
                        (topology.identity, *evidence_ids)
                    )
                ),
            ),
        )
        validate_design_candidate(candidate, (topology, *evidence))
        return candidate, *evidence

    def validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        try:
            self._compile(context)
        except (FlowExecutionError, OSError, TypeError, ValueError) as exc:
            return (str(exc),)
        return ()

    def prepare(self, context: ActionContext) -> None:
        self._compile(context)

    def execute(self, context: ActionContext) -> AdapterExecution:
        values = self._compile(context)
        for role, value in zip(("candidate", "drc", "lvs", "pex"), values, strict=True):
            context.output_path(role, f"{role}.json").write_text(
                value.canonical_json(),
                encoding="utf-8",
            )
        return AdapterExecution.succeeded()

    def collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        if execution.status != "succeeded" or execution.exit_code != 0:
            raise FlowExecutionError("physical observation execution did not succeed")
        artifacts: list[ProducedArtifact] = []
        for role, kind in (
            ("candidate", DESIGN_CANDIDATE_KIND),
            ("drc", DESIGN_EVIDENCE_KIND),
            ("lvs", DESIGN_EVIDENCE_KIND),
            ("pex", DESIGN_EVIDENCE_KIND),
        ):
            path = context.output_path(role, f"{role}.json")
            value = design_artifact_from_json(read_nofollow_text(path))
            if value.metadata.kind != kind:
                raise FlowExecutionError("physical observation output kind drift")
            qualifiers = {"artifact-identity": value.identity}
            if isinstance(value, DesignEvidence):
                qualifiers["conclusion"] = value.conclusion.value
            artifacts.append(ProducedArtifact(role, kind, path, qualifiers))
        return CollectedActionResult(artifacts=tuple(artifacts))


__all__ = ["PhysicalDesignObservationAdapter"]
