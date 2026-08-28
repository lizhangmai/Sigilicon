"""Immutable front-end circuit-design artifacts and their exact identities."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
import json
import re

from sigilicon.canonical import (
    canonical_from_exact_json,
    canonical_json,
    canonical_sha256,
)


ARTIFACT_SCHEMA = 1
CIRCUIT_TOPOLOGY_KIND = "circuit.topology-proposal.v1"
CIRCUIT_SIZING_PROBLEM_KIND = "circuit.sizing-problem.v1"
CIRCUIT_SIZING_RESULT_KIND = "circuit.sizing-result.v1"
SOURCE_NETLIST_KIND = "source.netlist"
DESIGN_BRIEF_KIND = "design.brief.v1"
DESIGN_CANDIDATE_KIND = "design.candidate.v1"
DESIGN_EVIDENCE_KIND = "design.evidence.v1"
DESIGN_DECISION_KIND = "design.decision.v1"

_OWNER = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[._-][A-Za-z0-9]+)*\Z")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*(?:[._-][A-Za-z0-9_$]+)*\Z")
_SEMANTIC_IDENTITY = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*\Z")
_TOKEN = re.compile(r"[^\s;\\/]+\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _owner(value: str, label: str = "artifact owner") -> None:
    if not isinstance(value, str) or _OWNER.fullmatch(value) is None:
        raise ValueError(f"invalid {label}: {value!r}")


def _identifier(value: str, label: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"invalid {label}: {value!r}")


def _semantic_identity(value: str, label: str) -> None:
    if not isinstance(value, str) or _SEMANTIC_IDENTITY.fullmatch(value) is None:
        raise ValueError(f"invalid {label}: {value!r}")


def _token(value: str, label: str) -> None:
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        raise ValueError(f"invalid {label}: {value!r}")


def _sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be a SHA-256 identity")


def _unique(values: tuple[str, ...], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"duplicate {label}")


@dataclass(frozen=True)
class ArtifactMetadata:
    schema: int
    kind: str
    owner: str

    def __post_init__(self) -> None:
        if type(self.schema) is not int or self.schema != ARTIFACT_SCHEMA:
            raise ValueError(f"artifact schema must be {ARTIFACT_SCHEMA}")
        _semantic_identity(self.kind, "artifact kind")
        _owner(self.owner)


@dataclass(frozen=True)
class ArtifactReference:
    owner: str
    kind: str
    sha256: str
    release_identity: str | None

    def __post_init__(self) -> None:
        _owner(self.owner, "artifact reference owner")
        _semantic_identity(self.kind, "artifact reference kind")
        _sha256(self.sha256, "artifact reference")
        if self.release_identity is not None:
            _semantic_identity(self.release_identity, "release identity")


class CanonicalDesignArtifact:
    """Shared behavior; artifact payloads remain stage-specific dataclasses."""

    metadata: ArtifactMetadata

    def canonical_json(self) -> str:
        return canonical_json(self)

    @property
    def identity(self) -> str:
        return canonical_sha256(self)

    def reference(self, *, release_identity: str | None = None) -> ArtifactReference:
        return ArtifactReference(
            self.metadata.owner,
            self.metadata.kind,
            self.identity,
            release_identity,
        )


def _reference_for_owner(
    reference: ArtifactReference,
    owner: str,
    label: str,
) -> None:
    if reference.owner != owner and reference.release_identity is None:
        raise ValueError(
            f"cross-owner {label} requires an explicit release identity"
        )


class TopologyOrigin(str, Enum):
    SOURCE_AUTHORED = "source_authored"
    PROPOSED = "proposed"


class PortDirection(str, Enum):
    INPUT = "input"
    OUTPUT = "output"
    INPUT_OUTPUT = "inputOutput"


class EvidenceRole(str, Enum):
    DIAGNOSTIC = "diagnostic"
    REGRESSION = "regression"
    QUALIFICATION = "qualification"
    SIGNOFF = "signoff"


@dataclass(frozen=True)
class ProposalProvenance:
    producer: str
    version: str
    parent_sha256: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _semantic_identity(self.producer, "proposal producer")
        _token(self.version, "proposal producer version")
        for identity in self.parent_sha256:
            _sha256(identity, "proposal parent")
        _unique(self.parent_sha256, "proposal parents")


@dataclass(frozen=True)
class CircuitPort:
    name: str
    direction: PortDirection
    role: str

    def __post_init__(self) -> None:
        _identifier(self.name, "circuit port")
        if not isinstance(self.direction, PortDirection):
            raise ValueError("circuit port direction must be typed")
        _semantic_identity(self.role, "circuit port role")


@dataclass(frozen=True)
class CircuitParameter:
    name: str
    expression: str

    def __post_init__(self) -> None:
        _identifier(self.name, "circuit parameter")
        _token(self.expression, "circuit parameter expression")


@dataclass(frozen=True)
class CircuitInstance:
    name: str
    master: str
    nodes: tuple[str, ...]
    parameters: tuple[CircuitParameter, ...]
    role: str | None

    def __post_init__(self) -> None:
        _identifier(self.name, "circuit instance")
        _identifier(self.master, "circuit instance master")
        if not self.nodes:
            raise ValueError("circuit instance must connect at least one node")
        for node in self.nodes:
            _token(node, "circuit instance node")
        _unique(
            tuple(parameter.name for parameter in self.parameters),
            f"parameters on circuit instance {self.name}",
        )
        if self.role is not None:
            _semantic_identity(self.role, "instance role")


@dataclass(frozen=True)
class StateSemantic:
    name: str
    description: str

    def __post_init__(self) -> None:
        _semantic_identity(self.name, "state semantic")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("state semantic description must be non-empty text")


@dataclass(frozen=True)
class CircuitTopologyProposal(CanonicalDesignArtifact):
    metadata: ArtifactMetadata
    design: str
    source_snapshot_sha256: str
    origin: TopologyOrigin
    ports: tuple[CircuitPort, ...]
    parameters: tuple[CircuitParameter, ...]
    instances: tuple[CircuitInstance, ...]
    states: tuple[StateSemantic, ...]
    provenance: ProposalProvenance

    def __post_init__(self) -> None:
        if self.metadata.kind != CIRCUIT_TOPOLOGY_KIND:
            raise ValueError(
                f"topology artifact kind must be {CIRCUIT_TOPOLOGY_KIND!r}"
            )
        _identifier(self.design, "topology design")
        _sha256(self.source_snapshot_sha256, "topology source snapshot")
        if not isinstance(self.origin, TopologyOrigin):
            raise ValueError("topology origin must be typed")
        if not self.ports:
            raise ValueError("topology must declare ports")
        _unique(tuple(port.name for port in self.ports), "topology ports")
        _unique(tuple(item.name for item in self.parameters), "topology parameters")
        _unique(tuple(item.name for item in self.instances), "topology instances")
        _unique(tuple(item.name for item in self.states), "topology states")


def _canonical_decimal(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("exact quantity value must be decimal text or a number")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"invalid exact decimal quantity: {value!r}") from exc
    if not number.is_finite():
        raise ValueError("exact quantity must be finite")
    if number == 0:
        return "0"
    text = format(number.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


@dataclass(frozen=True)
class ExactQuantity:
    value: str
    unit: str

    def __post_init__(self) -> None:
        if self.value != _canonical_decimal(self.value):
            raise ValueError("exact quantity value is not canonical decimal text")
        _semantic_identity(self.unit, "quantity unit")

    @property
    def decimal(self) -> Decimal:
        return Decimal(self.value)


def exact_quantity(value: object, unit: str) -> ExactQuantity:
    return ExactQuantity(_canonical_decimal(value), unit)


@dataclass(frozen=True)
class NamedQuantity:
    name: str
    quantity: ExactQuantity

    def __post_init__(self) -> None:
        _semantic_identity(self.name, "named quantity")


@dataclass(frozen=True)
class SizingParameter:
    name: str
    unit: str
    lower: ExactQuantity
    upper: ExactQuantity
    discrete_values: tuple[ExactQuantity, ...]
    multiplicity: int
    matching_group: str | None

    def __post_init__(self) -> None:
        _semantic_identity(self.name, "sizing parameter")
        _semantic_identity(self.unit, "sizing parameter unit")
        if self.lower.unit != self.unit or self.upper.unit != self.unit:
            raise ValueError("sizing parameter bounds must use its declared unit")
        if self.lower.decimal > self.upper.decimal:
            raise ValueError("sizing parameter lower bound exceeds upper bound")
        if not self.discrete_values:
            raise ValueError("sizing parameter needs discrete values")
        seen: set[str] = set()
        for value in self.discrete_values:
            if value.unit != self.unit:
                raise ValueError("sizing parameter values must use its declared unit")
            if not self.lower.decimal <= value.decimal <= self.upper.decimal:
                raise ValueError("sizing parameter value is outside declared bounds")
            if value.value in seen:
                raise ValueError("duplicate sizing parameter value")
            seen.add(value.value)
        if type(self.multiplicity) is not int or self.multiplicity <= 0:
            raise ValueError("sizing parameter multiplicity must be positive")
        if self.matching_group is not None:
            _semantic_identity(self.matching_group, "sizing matching group")


@dataclass(frozen=True)
class SizingMatchingGroup:
    name: str
    parameters: tuple[str, ...]

    def __post_init__(self) -> None:
        _semantic_identity(self.name, "sizing matching group")
        if len(self.parameters) < 2:
            raise ValueError("sizing matching group needs at least two parameters")
        for parameter in self.parameters:
            _semantic_identity(parameter, "matched sizing parameter")
        _unique(self.parameters, "matched sizing parameters")


@dataclass(frozen=True)
class SizingCondition:
    name: str
    values: tuple[NamedQuantity, ...]

    def __post_init__(self) -> None:
        _semantic_identity(self.name, "sizing condition")
        _unique(tuple(item.name for item in self.values), "sizing condition values")


@dataclass(frozen=True)
class SizingCandidate:
    name: str
    values: tuple[NamedQuantity, ...]
    description: str

    def __post_init__(self) -> None:
        _semantic_identity(self.name, "sizing candidate")
        if not self.values:
            raise ValueError("sizing candidate needs parameter values")
        _unique(tuple(item.name for item in self.values), "sizing candidate values")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("sizing candidate description must be non-empty text")


@dataclass(frozen=True)
class SizingBudget:
    maximum_evaluations: int
    seed: int | None

    def __post_init__(self) -> None:
        if type(self.maximum_evaluations) is not int or self.maximum_evaluations <= 0:
            raise ValueError("sizing evaluation budget must be positive")
        if self.seed is not None and type(self.seed) is not int:
            raise ValueError("sizing seed must be an integer or null")


@dataclass(frozen=True)
class CircuitSizingProblem(CanonicalDesignArtifact):
    metadata: ArtifactMetadata
    design: str
    topology: ArtifactReference
    specification: ArtifactReference
    testbench: str
    evidence_role: EvidenceRole
    parameters: tuple[SizingParameter, ...]
    matching_groups: tuple[SizingMatchingGroup, ...]
    conditions: tuple[SizingCondition, ...]
    candidates: tuple[SizingCandidate, ...]
    budget: SizingBudget

    def __post_init__(self) -> None:
        if self.metadata.kind != CIRCUIT_SIZING_PROBLEM_KIND:
            raise ValueError(
                f"sizing problem artifact kind must be {CIRCUIT_SIZING_PROBLEM_KIND!r}"
            )
        _identifier(self.design, "sizing design")
        if self.topology.kind != CIRCUIT_TOPOLOGY_KIND:
            raise ValueError("sizing problem must reference a topology proposal")
        _reference_for_owner(self.topology, self.metadata.owner, "topology")
        _reference_for_owner(self.specification, self.metadata.owner, "specification")
        _identifier(self.testbench, "sizing testbench")
        if not isinstance(self.evidence_role, EvidenceRole):
            raise ValueError("sizing evidence role must be typed")
        if not self.parameters or not self.conditions or not self.candidates:
            raise ValueError("sizing problem needs parameters, conditions, and candidates")
        parameter_names = tuple(item.name for item in self.parameters)
        _unique(parameter_names, "sizing parameters")
        group_names = tuple(item.name for item in self.matching_groups)
        _unique(group_names, "sizing matching groups")
        for group in self.matching_groups:
            unknown = sorted(set(group.parameters) - set(parameter_names))
            if unknown:
                raise ValueError(f"matching group has unknown parameters: {unknown}")
        _unique(tuple(item.name for item in self.conditions), "sizing conditions")
        _unique(tuple(item.name for item in self.candidates), "sizing candidates")
        parameters = {item.name: item for item in self.parameters}
        for candidate in self.candidates:
            names = tuple(item.name for item in candidate.values)
            if set(names) != set(parameters) or len(names) != len(parameters):
                raise ValueError(
                    f"sizing candidate {candidate.name!r} must bind every parameter"
                )
            for item in candidate.values:
                parameter = parameters[item.name]
                if item.quantity.unit != parameter.unit or item.quantity not in parameter.discrete_values:
                    raise ValueError(
                        f"sizing candidate {candidate.name!r} has undeclared value for {item.name!r}"
                    )


class SizingTermination(str, Enum):
    COMPLETED = "completed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    EXECUTION_FAILED = "execution_failed"


class SizingCandidateOutcome(str, Enum):
    SATISFIED = "satisfied"
    VIOLATED = "violated"
    NOT_EVALUATED = "not_evaluated"


@dataclass(frozen=True)
class SizingCandidateResult:
    candidate: str
    outcome: SizingCandidateOutcome
    evaluations: int
    observations: tuple[NamedQuantity, ...]

    def __post_init__(self) -> None:
        _semantic_identity(self.candidate, "sizing result candidate")
        if not isinstance(self.outcome, SizingCandidateOutcome):
            raise ValueError("sizing candidate outcome must be typed")
        if type(self.evaluations) is not int or self.evaluations < 0:
            raise ValueError("sizing candidate evaluation count must be non-negative")
        _unique(tuple(item.name for item in self.observations), "sizing observations")
        if self.outcome is SizingCandidateOutcome.NOT_EVALUATED and self.evaluations:
            raise ValueError("a non-evaluated sizing candidate has no evaluations")


@dataclass(frozen=True)
class CircuitSizingResult(CanonicalDesignArtifact):
    metadata: ArtifactMetadata
    problem: ArtifactReference
    evidence_role: EvidenceRole
    optimizer: str
    optimizer_version: str
    termination: SizingTermination
    candidates: tuple[SizingCandidateResult, ...]
    selected_candidate: str | None
    evaluations: int
    seed: int | None

    def __post_init__(self) -> None:
        if self.metadata.kind != CIRCUIT_SIZING_RESULT_KIND:
            raise ValueError(
                f"sizing result artifact kind must be {CIRCUIT_SIZING_RESULT_KIND!r}"
            )
        if self.problem.kind != CIRCUIT_SIZING_PROBLEM_KIND:
            raise ValueError("sizing result must reference a sizing problem")
        _reference_for_owner(self.problem, self.metadata.owner, "sizing problem")
        if not isinstance(self.evidence_role, EvidenceRole):
            raise ValueError("sizing result evidence role must be typed")
        _semantic_identity(self.optimizer, "sizing optimizer")
        _token(self.optimizer_version, "sizing optimizer version")
        if not isinstance(self.termination, SizingTermination):
            raise ValueError("sizing termination must be typed")
        _unique(tuple(item.candidate for item in self.candidates), "sizing result candidates")
        if type(self.evaluations) is not int or self.evaluations < 0:
            raise ValueError("sizing result evaluation count must be non-negative")
        if self.seed is not None and type(self.seed) is not int:
            raise ValueError("sizing result seed must be an integer or null")
        if sum(item.evaluations for item in self.candidates) != self.evaluations:
            raise ValueError("sizing result total evaluations do not match candidates")
        if self.selected_candidate is not None:
            _semantic_identity(self.selected_candidate, "selected sizing candidate")
            matches = tuple(
                item for item in self.candidates if item.candidate == self.selected_candidate
            )
            if not matches or matches[0].outcome is not SizingCandidateOutcome.SATISFIED:
                raise ValueError("selected sizing candidate must have a satisfied result")
        if self.termination in {
            SizingTermination.BACKEND_UNAVAILABLE,
            SizingTermination.EXECUTION_FAILED,
        } and (self.candidates or self.selected_candidate is not None):
            raise ValueError("failed sizing execution cannot publish candidate results")


class EvidenceLevel(str, Enum):
    L0 = "l0"
    L1 = "l1"
    L2 = "l2"
    L3 = "l3"
    L4 = "l4"


class EvidenceProducerKind(str, Enum):
    DETERMINISTIC_CHECK = "deterministic_check"
    EXECUTED_BACKEND = "executed_backend"
    OFFLINE = "offline"
    FAKE = "fake"
    LLM = "llm"


class EvidenceConclusion(str, Enum):
    SATISFIED = "satisfied"
    VIOLATED = "violated"
    NOT_EVALUATED = "not_evaluated"
    UNSUPPORTED = "unsupported"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    EXECUTION_FAILED = "execution_failed"
    INVALID_IDENTITY = "invalid_identity"


@dataclass(frozen=True)
class EvidenceProducer:
    kind: EvidenceProducerKind
    name: str
    version: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, EvidenceProducerKind):
            raise ValueError("evidence producer kind must be typed")
        _semantic_identity(self.name, "evidence producer")
        _token(self.version, "evidence producer version")


@dataclass(frozen=True)
class EvidenceCompletion:
    backend: str
    executed: bool
    authoritative_output_parsed: bool
    exit_code: int | None

    def __post_init__(self) -> None:
        _semantic_identity(self.backend, "evidence backend")
        if type(self.executed) is not bool or type(self.authoritative_output_parsed) is not bool:
            raise ValueError("evidence completion flags must be booleans")
        if self.exit_code is not None and type(self.exit_code) is not int:
            raise ValueError("evidence exit code must be an integer or null")
        if self.authoritative_output_parsed and not self.executed:
            raise ValueError("unexecuted evidence cannot have parsed output")
        if self.exit_code is not None and not self.executed:
            raise ValueError("unexecuted evidence cannot have an exit code")

    @property
    def proven(self) -> bool:
        return self.executed and self.authoritative_output_parsed and self.exit_code == 0


@dataclass(frozen=True)
class EvidenceFinding:
    code: str
    count: int
    message: str

    def __post_init__(self) -> None:
        _semantic_identity(self.code, "evidence finding code")
        if type(self.count) is not int or self.count <= 0:
            raise ValueError("evidence finding count must be positive")
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("evidence finding message must be non-empty text")


@dataclass(frozen=True)
class DesignEvidence(CanonicalDesignArtifact):
    metadata: ArtifactMetadata
    subject: ArtifactReference
    source: ArtifactReference
    specification: ArtifactReference | None
    role: EvidenceRole
    level: EvidenceLevel
    scope: tuple[str, ...]
    producer: EvidenceProducer
    completion: EvidenceCompletion
    conclusion: EvidenceConclusion
    findings: tuple[EvidenceFinding, ...]
    message: str

    def __post_init__(self) -> None:
        if self.metadata.kind != DESIGN_EVIDENCE_KIND:
            raise ValueError(
                f"evidence artifact kind must be {DESIGN_EVIDENCE_KIND!r}"
            )
        for label, reference in (
            ("subject", self.subject),
            ("source", self.source),
        ):
            _reference_for_owner(reference, self.metadata.owner, f"evidence {label}")
        if self.specification is not None:
            _reference_for_owner(
                self.specification,
                self.metadata.owner,
                "evidence specification",
            )
        if not isinstance(self.role, EvidenceRole):
            raise ValueError("evidence role must be typed")
        if not isinstance(self.level, EvidenceLevel):
            raise ValueError("evidence level must be typed")
        if not self.scope:
            raise ValueError("evidence scope must not be empty")
        for item in self.scope:
            _semantic_identity(item, "evidence scope")
        _unique(self.scope, "evidence scope")
        if not isinstance(self.conclusion, EvidenceConclusion):
            raise ValueError("evidence conclusion must be typed")
        if self.role in {EvidenceRole.QUALIFICATION, EvidenceRole.SIGNOFF} and self.specification is None:
            raise ValueError("qualification/signoff evidence requires a specification")
        if self.producer.kind in {
            EvidenceProducerKind.OFFLINE,
            EvidenceProducerKind.FAKE,
            EvidenceProducerKind.LLM,
        } and self.conclusion in {
            EvidenceConclusion.SATISFIED,
            EvidenceConclusion.VIOLATED,
        }:
            raise ValueError("offline/fake/LLM evidence cannot make a conclusive claim")
        if self.conclusion is EvidenceConclusion.SATISFIED:
            if not self.completion.proven:
                raise ValueError("satisfied evidence requires proven completion")
            if self.role in {EvidenceRole.QUALIFICATION, EvidenceRole.SIGNOFF} and self.producer.kind is not EvidenceProducerKind.EXECUTED_BACKEND:
                raise ValueError(
                    "qualification/signoff pass requires an executed backend"
                )
        if self.conclusion is EvidenceConclusion.VIOLATED and not self.findings:
            raise ValueError("violated evidence requires typed findings")
        _unique(tuple(item.code for item in self.findings), "evidence findings")
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("evidence message must be non-empty text")


def _statements(values: tuple[str, ...], label: str, *, required: bool = False) -> None:
    if required and not values:
        raise ValueError(f"{label} must not be empty")
    for value in values:
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise ValueError(f"{label} must contain non-empty text")
    _unique(values, label)


@dataclass(frozen=True)
class DesignBrief(CanonicalDesignArtifact):
    metadata: ArtifactMetadata
    design: str
    intent: tuple[str, ...]
    confirmed_requirements: tuple[str, ...]
    provisional_assumptions: tuple[str, ...]
    preferences: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.metadata.kind != DESIGN_BRIEF_KIND:
            raise ValueError(f"brief artifact kind must be {DESIGN_BRIEF_KIND!r}")
        _identifier(self.design, "Design Brief design")
        _statements(self.intent, "Design Brief intent", required=True)
        _statements(
            self.confirmed_requirements,
            "Design Brief confirmed requirements",
        )
        _statements(
            self.provisional_assumptions,
            "Design Brief provisional assumptions",
        )
        _statements(self.preferences, "Design Brief preferences")


@dataclass(frozen=True)
class DesignCandidate(CanonicalDesignArtifact):
    metadata: ArtifactMetadata
    design: str
    source: ArtifactReference
    brief: ArtifactReference | None
    canonical_specifications: tuple[ArtifactReference, ...]
    topology: ArtifactReference
    sizing_problem: ArtifactReference | None
    sizing_result: ArtifactReference | None
    evidence: tuple[ArtifactReference, ...]
    parent_candidate: ArtifactReference | None
    provenance: ProposalProvenance

    def __post_init__(self) -> None:
        if self.metadata.kind != DESIGN_CANDIDATE_KIND:
            raise ValueError(
                f"candidate artifact kind must be {DESIGN_CANDIDATE_KIND!r}"
            )
        _identifier(self.design, "candidate design")
        references = [("source", self.source), ("topology", self.topology)]
        if self.brief is not None:
            references.append(("brief", self.brief))
        references.extend(
            ("specification", item) for item in self.canonical_specifications
        )
        if self.sizing_problem is not None:
            references.append(("sizing problem", self.sizing_problem))
        if self.sizing_result is not None:
            references.append(("sizing result", self.sizing_result))
        references.extend(("evidence", item) for item in self.evidence)
        if self.parent_candidate is not None:
            references.append(("parent Candidate", self.parent_candidate))
        for label, reference in references:
            assert reference is not None
            _reference_for_owner(reference, self.metadata.owner, label)
        if self.topology.kind != CIRCUIT_TOPOLOGY_KIND:
            raise ValueError("Candidate topology has the wrong artifact kind")
        if self.source.kind != SOURCE_NETLIST_KIND:
            raise ValueError("Candidate source has the wrong artifact kind")
        if self.brief is not None and self.brief.kind != DESIGN_BRIEF_KIND:
            raise ValueError("Candidate Design Brief has the wrong artifact kind")
        if self.sizing_problem is not None and self.sizing_problem.kind != CIRCUIT_SIZING_PROBLEM_KIND:
            raise ValueError("Candidate sizing problem has the wrong artifact kind")
        if self.sizing_result is not None:
            if self.sizing_problem is None:
                raise ValueError("Candidate sizing result requires its sizing problem")
            if self.sizing_result.kind != CIRCUIT_SIZING_RESULT_KIND:
                raise ValueError("Candidate sizing result has the wrong artifact kind")
        if any(item.kind != DESIGN_EVIDENCE_KIND for item in self.evidence):
            raise ValueError("Candidate evidence has the wrong artifact kind")
        if self.parent_candidate is not None and self.parent_candidate.kind != DESIGN_CANDIDATE_KIND:
            raise ValueError("Candidate parent has the wrong artifact kind")
        _unique(
            tuple(item.sha256 for item in self.canonical_specifications),
            "Candidate specification references",
        )
        _unique(tuple(item.sha256 for item in self.evidence), "Candidate evidence references")


class DesignDecisionConclusion(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    NON_CONCLUSION = "non_conclusion"


@dataclass(frozen=True)
class DesignDecision(CanonicalDesignArtifact):
    metadata: ArtifactMetadata
    candidate: ArtifactReference
    policy: ArtifactReference
    role: EvidenceRole
    level: EvidenceLevel
    scope: tuple[str, ...]
    evidence: tuple[ArtifactReference, ...]
    conclusion: DesignDecisionConclusion
    rationale: str

    def __post_init__(self) -> None:
        if self.metadata.kind != DESIGN_DECISION_KIND:
            raise ValueError(
                f"decision artifact kind must be {DESIGN_DECISION_KIND!r}"
            )
        if self.candidate.kind != DESIGN_CANDIDATE_KIND:
            raise ValueError("Design Decision must reference a Candidate")
        _reference_for_owner(self.candidate, self.metadata.owner, "decision Candidate")
        _reference_for_owner(self.policy, self.metadata.owner, "decision policy")
        if not isinstance(self.role, EvidenceRole) or not isinstance(self.level, EvidenceLevel):
            raise ValueError("decision role and level must be typed")
        if not self.scope:
            raise ValueError("decision scope must not be empty")
        for item in self.scope:
            _semantic_identity(item, "decision scope")
        _unique(self.scope, "decision scope")
        if any(item.kind != DESIGN_EVIDENCE_KIND for item in self.evidence):
            raise ValueError("Design Decision evidence has the wrong artifact kind")
        for item in self.evidence:
            _reference_for_owner(item, self.metadata.owner, "decision evidence")
        _unique(tuple(item.sha256 for item in self.evidence), "decision evidence")
        if not isinstance(self.conclusion, DesignDecisionConclusion):
            raise ValueError("decision conclusion must be typed")
        if self.conclusion in {
            DesignDecisionConclusion.PASSED,
            DesignDecisionConclusion.FAILED,
        } and not self.evidence:
            raise ValueError("a conclusive Design Decision requires evidence")
        if not isinstance(self.rationale, str) or not self.rationale.strip():
            raise ValueError("decision rationale must be non-empty text")


@dataclass(frozen=True)
class CandidateValidation:
    candidate_sha256: str
    resolved_artifacts: tuple[str, ...]

    def __post_init__(self) -> None:
        _sha256(self.candidate_sha256, "validated Candidate")
        for identity in self.resolved_artifacts:
            _sha256(identity, "resolved Candidate artifact")
        _unique(self.resolved_artifacts, "resolved Candidate artifacts")


def _reference_key(reference: ArtifactReference) -> tuple[str, str, str]:
    return reference.owner, reference.kind, reference.sha256


def validate_design_candidate(
    candidate: DesignCandidate,
    artifacts: tuple[CanonicalDesignArtifact, ...],
) -> CandidateValidation:
    """Resolve every stage reference through canonical bytes and validate lineage."""

    available = {
        _reference_key(artifact.reference()): artifact for artifact in artifacts
    }
    required = tuple(
        reference
        for reference in (
            candidate.brief,
            candidate.topology,
            candidate.sizing_problem,
            candidate.sizing_result,
            *candidate.evidence,
            candidate.parent_candidate,
        )
        if reference is not None
    )
    resolved: list[CanonicalDesignArtifact] = []
    for reference in required:
        artifact = available.get(_reference_key(reference))
        if artifact is None:
            raise ValueError(
                f"Candidate lacks an identity-matched artifact for {reference.kind}"
            )
        resolved.append(artifact)
    topology = available[_reference_key(candidate.topology)]
    if not isinstance(topology, CircuitTopologyProposal):
        raise ValueError("Candidate topology reference resolved to the wrong type")
    if topology.design != candidate.design:
        raise ValueError("Candidate topology design identity drift")
    if topology.source_snapshot_sha256 != candidate.source.sha256:
        raise ValueError("Candidate source snapshot identity drift")
    if candidate.sizing_problem is not None:
        problem = available[_reference_key(candidate.sizing_problem)]
        if not isinstance(problem, CircuitSizingProblem) or problem.topology != candidate.topology:
            raise ValueError("Candidate sizing problem topology identity drift")
        if problem.design != candidate.design:
            raise ValueError("Candidate sizing problem design identity drift")
        if problem.specification not in candidate.canonical_specifications:
            raise ValueError("Candidate sizing problem specification is not declared")
    if candidate.sizing_result is not None:
        result = available[_reference_key(candidate.sizing_result)]
        if not isinstance(result, CircuitSizingResult) or result.problem != candidate.sizing_problem:
            raise ValueError("Candidate sizing result problem identity drift")
        assert isinstance(problem, CircuitSizingProblem)
        if result.evidence_role is not problem.evidence_role:
            raise ValueError("Candidate sizing result evidence role drift")
    subject_references = {
        _reference_key(reference)
        for reference in (
            candidate.topology,
            candidate.sizing_problem,
            candidate.sizing_result,
        )
        if reference is not None
    }
    for reference in candidate.evidence:
        evidence = available[_reference_key(reference)]
        if not isinstance(evidence, DesignEvidence):
            raise ValueError("Candidate evidence reference resolved to the wrong type")
        if _reference_key(evidence.subject) not in subject_references:
            raise ValueError("Candidate evidence subject is outside the Candidate stages")
        if evidence.source != candidate.source:
            raise ValueError("Candidate evidence source identity drift")
        if (
            evidence.specification is not None
            and evidence.specification not in candidate.canonical_specifications
        ):
            raise ValueError("Candidate evidence specification is not declared")
    return CandidateValidation(
        candidate.identity,
        tuple(sorted(artifact.identity for artifact in resolved)),
    )


def validate_design_decision(
    decision: DesignDecision,
    candidate: DesignCandidate,
    evidences: tuple[DesignEvidence, ...],
) -> None:
    """Prove one policy decision from exact Candidate-bound evidence."""

    if decision.candidate != candidate.reference():
        raise ValueError("Design Decision Candidate identity drift")
    if decision.policy not in candidate.canonical_specifications:
        raise ValueError("Design Decision policy is not a Candidate specification")
    available = {_reference_key(item.reference()): item for item in evidences}
    candidate_evidence = {_reference_key(item) for item in candidate.evidence}
    resolved: list[DesignEvidence] = []
    for reference in decision.evidence:
        if _reference_key(reference) not in candidate_evidence:
            raise ValueError("Design Decision evidence is not bound by the Candidate")
        evidence = available.get(_reference_key(reference))
        if evidence is None:
            raise ValueError("Design Decision lacks identity-matched evidence")
        if evidence.role is not decision.role or evidence.level is not decision.level:
            raise ValueError("Design Decision evidence role or level drift")
        if not set(decision.scope).issubset(evidence.scope):
            raise ValueError("Design Decision evidence does not cover its scope")
        if evidence.specification != decision.policy:
            raise ValueError("Design Decision evidence policy identity drift")
        resolved.append(evidence)
    if decision.conclusion is DesignDecisionConclusion.PASSED and any(
        item.conclusion is not EvidenceConclusion.SATISFIED for item in resolved
    ):
        raise ValueError("passing Design Decision requires satisfied evidence")
    if decision.conclusion is DesignDecisionConclusion.FAILED and not any(
        item.conclusion is EvidenceConclusion.VIOLATED for item in resolved
    ):
        raise ValueError("failed Design Decision requires violated evidence")


def circuit_topology_from_json(text: str) -> CircuitTopologyProposal:
    return canonical_from_exact_json(text, CircuitTopologyProposal)


def circuit_sizing_problem_from_json(text: str) -> CircuitSizingProblem:
    return canonical_from_exact_json(text, CircuitSizingProblem)


def circuit_sizing_result_from_json(text: str) -> CircuitSizingResult:
    return canonical_from_exact_json(text, CircuitSizingResult)


def design_evidence_from_json(text: str) -> DesignEvidence:
    return canonical_from_exact_json(text, DesignEvidence)


def design_brief_from_json(text: str) -> DesignBrief:
    return canonical_from_exact_json(text, DesignBrief)


def design_candidate_from_json(text: str) -> DesignCandidate:
    return canonical_from_exact_json(text, DesignCandidate)


def design_decision_from_json(text: str) -> DesignDecision:
    return canonical_from_exact_json(text, DesignDecision)


def design_artifact_from_json(text: str) -> CanonicalDesignArtifact:
    """Decode one exact stage artifact through its owned kind discriminator."""

    if not isinstance(text, str):
        raise ValueError("design artifact input must be text")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid design artifact JSON: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("metadata"), dict):
        raise ValueError("design artifact must contain metadata")
    kind = raw["metadata"].get("kind")
    decoder = {
        CIRCUIT_TOPOLOGY_KIND: circuit_topology_from_json,
        CIRCUIT_SIZING_PROBLEM_KIND: circuit_sizing_problem_from_json,
        CIRCUIT_SIZING_RESULT_KIND: circuit_sizing_result_from_json,
        DESIGN_BRIEF_KIND: design_brief_from_json,
        DESIGN_CANDIDATE_KIND: design_candidate_from_json,
        DESIGN_EVIDENCE_KIND: design_evidence_from_json,
        DESIGN_DECISION_KIND: design_decision_from_json,
    }.get(kind)
    if decoder is None:
        raise ValueError(f"unknown design artifact kind: {kind!r}")
    return decoder(text)


__all__ = [
    "ARTIFACT_SCHEMA",
    "CIRCUIT_SIZING_PROBLEM_KIND",
    "CIRCUIT_SIZING_RESULT_KIND",
    "CIRCUIT_TOPOLOGY_KIND",
    "DESIGN_BRIEF_KIND",
    "DESIGN_CANDIDATE_KIND",
    "DESIGN_DECISION_KIND",
    "DESIGN_EVIDENCE_KIND",
    "ArtifactMetadata",
    "ArtifactReference",
    "CanonicalDesignArtifact",
    "CircuitInstance",
    "CircuitParameter",
    "CircuitPort",
    "CircuitSizingProblem",
    "CircuitSizingResult",
    "CircuitTopologyProposal",
    "CandidateValidation",
    "DesignCandidate",
    "DesignBrief",
    "DesignDecision",
    "DesignDecisionConclusion",
    "DesignEvidence",
    "EvidenceCompletion",
    "EvidenceConclusion",
    "EvidenceFinding",
    "EvidenceLevel",
    "EvidenceProducer",
    "EvidenceProducerKind",
    "EvidenceRole",
    "ExactQuantity",
    "NamedQuantity",
    "PortDirection",
    "ProposalProvenance",
    "SizingBudget",
    "SizingCandidate",
    "SizingCandidateOutcome",
    "SizingCandidateResult",
    "SizingCondition",
    "SizingMatchingGroup",
    "SizingParameter",
    "SizingTermination",
    "SOURCE_NETLIST_KIND",
    "StateSemantic",
    "TopologyOrigin",
    "circuit_sizing_problem_from_json",
    "circuit_sizing_result_from_json",
    "circuit_topology_from_json",
    "design_candidate_from_json",
    "design_artifact_from_json",
    "design_brief_from_json",
    "design_decision_from_json",
    "design_evidence_from_json",
    "exact_quantity",
    "validate_design_candidate",
    "validate_design_decision",
]
