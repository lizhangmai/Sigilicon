"""Typed interfaces for deterministic design Flow planning and execution."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Mapping

from sigilicon.identifiers import RUN_ID_PATTERN
from sigilicon.paths import ProjectScope


_IDENTIFIER_RE = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*\Z")
_OWNER_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[._-][A-Za-z0-9]+)*\Z")
_RUN_ID_RE = re.compile(RUN_ID_PATTERN + r"\Z")
_REQUIREMENTS = frozenset({"accepted", "valid"})
_RESULT_STATUSES = frozenset({"valid", "failed", "partial", "uncertain"})
_EXECUTION_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
_FLOW_PROGRESS_STATUSES = frozenset({"running", "accepted", "failed", "cancelled"})
EXECUTION_CAPABILITIES = frozenset({"execute-derived", "mutate-workspace"})


class FlowContractError(ValueError):
    """A source Flow or one of its typed interfaces is invalid."""


class FlowExecutionError(RuntimeError):
    """A planned Action could not produce a valid result."""


def identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise FlowContractError(f"invalid {label}: {value!r}")
    return value


def owner_identity(value: str, label: str) -> str:
    if not isinstance(value, str) or _OWNER_RE.fullmatch(value) is None:
        raise FlowContractError(f"invalid {label}: {value!r}")
    return value


def run_identity(value: str) -> str:
    if not isinstance(value, str) or _RUN_ID_RE.fullmatch(value) is None:
        raise FlowContractError(f"invalid Flow Run identity: {value!r}")
    return value


def _semantic_identity(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        raise FlowContractError(f"{label} must be a non-empty semantic identity")
    if Path(value).is_absolute() or re.match(r"[A-Za-z]:[\\/]", value):
        raise FlowContractError(f"{label} cannot be an absolute site path")
    return value


def _unique(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    if len(values) != len(set(values)):
        raise FlowContractError(f"duplicate {label}")
    return values


def _portable_value(
    value: Any,
    label: str,
    error_type: type[FlowContractError] | type[FlowExecutionError],
) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise error_type(f"{label} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise error_type(f"{label} contains a non-string key")
        return MappingProxyType(
            {
                key: _portable_value(item, label, error_type)
                for key, item in value.items()
            }
        )
    if isinstance(value, (tuple, list)):
        return tuple(_portable_value(item, label, error_type) for item in value)
    raise error_type(
        f"{label} contains a non-portable {type(value).__name__} value"
    )


def _portable_mapping(
    value: Any,
    label: str,
    error_type: type[FlowContractError] | type[FlowExecutionError],
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise error_type(f"{label} must be a mapping")
    return _portable_value(value, label, error_type)


def _qualifier_mapping(
    value: Any,
    label: str,
    error_type: type[FlowContractError] | type[FlowExecutionError],
) -> Mapping[str, Any]:
    result = _portable_mapping(value, label, error_type)
    for name in result:
        if _IDENTIFIER_RE.fullmatch(name) is None:
            raise error_type(f"{label} contains invalid dimension {name!r}")
    return result


@dataclass(frozen=True)
class ArtifactPort:
    """One semantic input or output role at an Action interface."""

    role: str
    kind: str
    required: bool = True
    multiple: bool = False
    accepted_kinds: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        identifier(self.role, "artifact port role")
        identifier(self.kind, "artifact kind")
        for kind in self.accepted_kinds:
            identifier(kind, "accepted artifact kind")
        _unique(self.accepted_kinds, "accepted artifact kinds")

    def accepts(self, kind: str) -> bool:
        return kind == self.kind or kind in self.accepted_kinds


@dataclass(frozen=True)
class PlatformAssetRequirement:
    """A semantic platform view required by an ActionContract."""

    role: str
    kind: str
    members: tuple[str, ...] = ()
    identity: str | None = None

    def __post_init__(self) -> None:
        identifier(self.role, "platform asset role")
        identifier(self.kind, "platform asset kind")
        for member in self.members:
            identifier(member, "platform asset member role")
        _unique(self.members, "platform asset member roles")
        if self.identity is not None:
            _semantic_identity(self.identity, "platform asset identity")


@dataclass(frozen=True)
class SourceMember:
    """One owner-relative member selected from the current Git checkout."""

    path: str
    record_text: str = field(repr=False)
    executable: bool
    location: Path = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        relative = Path(self.path)
        if (
            not self.path
            or relative.is_absolute()
            or "\\" in self.path
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise FlowContractError(
                f"source member must be owner-relative: {self.path!r}"
            )
        if not isinstance(self.record_text, str):
            raise FlowContractError("source member record must be exact UTF-8 text")
        if not isinstance(self.executable, bool):
            raise FlowContractError("source member executable flag must be boolean")
        object.__setattr__(self, "location", Path(self.location).resolve())


@dataclass(frozen=True)
class SourceArtifact:
    """Members implementing one typed source artifact role."""

    role: str
    kind: str
    materialization: str
    qualifiers: Mapping[str, Any]
    members: tuple[SourceMember, ...]

    def __post_init__(self) -> None:
        identifier(self.role, "source artifact role")
        identifier(self.kind, "source artifact kind")
        if self.materialization not in {"file", "manifest"}:
            raise FlowContractError(
                "source artifact materialization must be 'file' or 'manifest'"
            )
        if self.materialization == "file" and len(self.members) != 1:
            raise FlowContractError(
                f"source artifact {self.role!r} file materialization needs one member"
            )
        if not self.members:
            raise FlowContractError(
                f"source artifact {self.role!r} declares no members"
            )
        _unique(tuple(member.path for member in self.members), "source artifact members")
        object.__setattr__(
            self,
            "qualifiers",
            _qualifier_mapping(
                self.qualifiers,
                "source artifact qualifiers",
                FlowContractError,
            ),
        )


@dataclass(frozen=True)
class GitSource:
    """Readable identity of the Git checkout used by an owner Flow."""

    commit: str
    changes: tuple[str, ...]
    repository_root: Path = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if re.fullmatch(r"[0-9a-f]{40,64}", self.commit) is None:
            raise FlowContractError("Git source commit is invalid")
        object.__setattr__(self, "repository_root", Path(self.repository_root).resolve())

    @property
    def dirty(self) -> bool:
        return bool(self.changes)


@dataclass(frozen=True)
class SourceAssets:
    """One Git-owned selection of typed source artifacts."""

    owner: str
    name: str
    git: GitSource
    artifacts: tuple[SourceArtifact, ...]
    owner_root: Path = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        owner_identity(self.owner, "source assets owner")
        identifier(self.name, "source assets identity")
        _unique(
            tuple(artifact.role for artifact in self.artifacts),
            "source artifact roles",
        )
        if not self.artifacts:
            raise FlowContractError("source assets declare no artifacts")
        object.__setattr__(self, "owner_root", Path(self.owner_root).resolve())

    def artifact(self, role: str) -> SourceArtifact:
        try:
            return next(artifact for artifact in self.artifacts if artifact.role == role)
        except StopIteration as exc:
            raise FlowContractError(
                f"source assets {self.name!r} have no role {role!r}"
            ) from exc


@dataclass(frozen=True)
class ActionContract:
    """Tool-independent interface implemented by one or more Adapters."""

    kind: str
    inputs: tuple[ArtifactPort, ...] = ()
    outputs: tuple[ArtifactPort, ...] = ()
    facts: tuple[str, ...] = ()
    optional_facts: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    platform_assets: tuple[PlatformAssetRequirement, ...] = ()
    adapters: tuple[str, ...] = ()
    adapter_extensible: bool = False
    resolves_source_assets: bool = False
    execution_capability: str = "execute-derived"
    accepts_design_campaign_iteration: bool = False

    def __post_init__(self) -> None:
        identifier(self.kind, "action kind")
        for value in self.facts:
            identifier(value, "fact name")
        for value in self.optional_facts:
            identifier(value, "optional fact name")
        for value in self.adapters:
            identifier(value, "Adapter name")
        for value in self.required_capabilities:
            identifier(value, "required capability")
        if self.execution_capability not in EXECUTION_CAPABILITIES:
            raise FlowContractError(
                "Action execution capability must be one of "
                f"{sorted(EXECUTION_CAPABILITIES)}"
            )
        _unique(tuple(port.role for port in self.inputs), "Action input roles")
        _unique(tuple(port.role for port in self.outputs), "Action output roles")
        _unique(self.facts, "Action facts")
        _unique(self.optional_facts, "Action optional facts")
        overlap = set(self.facts) & set(self.optional_facts)
        if overlap:
            raise FlowContractError(
                "Action required and optional facts overlap: "
                f"{sorted(overlap)}"
            )
        _unique(self.required_capabilities, "Action required capabilities")
        _unique(
            tuple(requirement.role for requirement in self.platform_assets),
            "Action platform asset roles",
        )
        _unique(self.adapters, "Action Adapters")
        if not self.adapters and not self.adapter_extensible:
            raise FlowContractError(f"Action {self.kind!r} declares no Adapter")
        if self.resolves_source_assets and self.inputs:
            raise FlowContractError("source assets Action cannot declare inputs")
        if type(self.accepts_design_campaign_iteration) is not bool:
            raise FlowContractError(
                "Action design Campaign iteration declaration must be boolean"
            )

    def input(self, role: str) -> ArtifactPort:
        try:
            return next(port for port in self.inputs if port.role == role)
        except StopIteration as exc:
            raise FlowContractError(
                f"Action {self.kind!r} has no input role {role!r}"
            ) from exc

    def output(self, role: str) -> ArtifactPort:
        try:
            return next(port for port in self.outputs if port.role == role)
        except StopIteration as exc:
            raise FlowContractError(
                f"Action {self.kind!r} has no output role {role!r}"
            ) from exc


@dataclass(frozen=True)
class ArtifactBinding:
    """A producer output connected to a consumer semantic input."""

    input: str
    producer: str
    output: str
    requires: str = "accepted"

    def __post_init__(self) -> None:
        identifier(self.input, "binding input")
        identifier(self.producer, "binding producer")
        identifier(self.output, "binding output")
        if self.requires not in _REQUIREMENTS:
            raise FlowContractError(
                f"binding requires must be one of {sorted(_REQUIREMENTS)}"
            )


@dataclass(frozen=True)
class AdapterSelection:
    """One tool selection and its private configuration in an ExecutionProfile."""

    action_kind: str
    adapter: str
    config: Mapping[str, Any] = field(default_factory=dict)
    required_capabilities: tuple[str, ...] = ()
    platform_asset_identities: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        identifier(self.action_kind, "profile Action kind")
        identifier(self.adapter, "profile Adapter")
        for capability in self.required_capabilities:
            identifier(capability, "profile required capability")
        _unique(self.required_capabilities, "profile required capabilities")
        identities: dict[str, str] = {}
        for role, identity in self.platform_asset_identities.items():
            identifier(role, "profile platform asset role")
            identities[role] = _semantic_identity(
                identity,
                "profile platform asset identity",
            )
        object.__setattr__(
            self,
            "platform_asset_identities",
            MappingProxyType(identities),
        )
        object.__setattr__(
            self,
            "config",
            _portable_mapping(
                self.config,
                "Adapter configuration",
                FlowContractError,
            ),
        )


@dataclass(frozen=True)
class ExecutionProfile:
    """Owner-selected Adapter bindings kept separate from Flow intent."""

    owner: str
    profile_id: str
    selections: tuple[AdapterSelection, ...]

    def __post_init__(self) -> None:
        owner_identity(self.owner, "Execution Profile owner")
        identifier(self.profile_id, "Execution Profile identity")
        _unique(
            tuple(selection.action_kind for selection in self.selections),
            "Execution Profile Action selections",
        )
        if not self.selections:
            raise FlowContractError(
                "Execution Profile must declare at least one Adapter selection"
            )

    def selection(self, action_kind: str) -> AdapterSelection:
        try:
            return next(
                selection
                for selection in self.selections
                if selection.action_kind == action_kind
            )
        except StopIteration as exc:
            raise FlowContractError(
                f"Execution Profile {self.profile_id!r} has no selection for "
                f"Action {action_kind!r}"
            ) from exc


@dataclass(frozen=True)
class FlowCatalogEntry:
    flow_id: str
    contract: Path
    default_profile: str
    profiles: Mapping[str, Path]

    def __post_init__(self) -> None:
        identifier(self.flow_id, "Flow Catalog entry")
        identifier(self.default_profile, "default Execution Profile")
        for profile_id in self.profiles:
            identifier(profile_id, "cataloged Execution Profile")
        if self.default_profile not in self.profiles:
            raise FlowContractError(
                f"Flow {self.flow_id!r} default profile is not cataloged"
            )
        object.__setattr__(self, "contract", Path(self.contract))
        object.__setattr__(
            self,
            "profiles",
            MappingProxyType(
                {name: Path(path) for name, path in self.profiles.items()}
            ),
        )


@dataclass(frozen=True)
class FlowCatalog:
    owner: str
    owner_root: Path
    entries: tuple[FlowCatalogEntry, ...]

    def __post_init__(self) -> None:
        owner_identity(self.owner, "Flow Catalog owner")
        _unique(
            tuple(entry.flow_id for entry in self.entries),
            "Flow Catalog entries",
        )
        if not self.entries:
            raise FlowContractError("Flow Catalog must declare at least one Flow")
        object.__setattr__(self, "owner_root", Path(self.owner_root).resolve())

    def entry(self, flow_id: str) -> FlowCatalogEntry:
        try:
            return next(entry for entry in self.entries if entry.flow_id == flow_id)
        except StopIteration as exc:
            raise FlowContractError(f"unknown cataloged Flow: {flow_id!r}") from exc


@dataclass(frozen=True)
class DesignCampaignIterationInput:
    """Exact cross-round input accepted only by declared design Actions."""

    campaign_identity: str
    iteration: int
    parent_candidate_identity: str
    attribution_json: str
    proposal_json: str
    repair_plan_json: str
    schema: int = 1
    contract_kind: str = "design-campaign-iteration-input"

    def __post_init__(self) -> None:
        if self.schema != 1 or self.contract_kind != "design-campaign-iteration-input":
            raise FlowContractError("invalid Design Campaign iteration input contract")
        _semantic_identity(self.campaign_identity, "Design Campaign identity")
        _semantic_identity(self.parent_candidate_identity, "parent Candidate identity")
        if type(self.iteration) is not int or self.iteration < 2:
            raise FlowContractError("Design Campaign child iteration must be at least two")
        for value, label in (
            (self.attribution_json, "attribution record"),
            (self.proposal_json, "proposal record"),
            (self.repair_plan_json, "Repair Plan record"),
        ):
            if not isinstance(value, str) or not value:
                raise FlowContractError(f"Design Campaign {label} must be exact JSON text")


@dataclass(frozen=True)
class FlowNode:
    node_id: str
    action_kind: str
    config: Mapping[str, Any] = field(default_factory=dict)
    bindings: tuple[ArtifactBinding, ...] = ()
    order_after: tuple[str, ...] = ()
    policy: str | None = None
    design_campaign_iteration: DesignCampaignIterationInput | None = None

    def __post_init__(self) -> None:
        identifier(self.node_id, "Flow node")
        identifier(self.action_kind, "action kind")
        if self.policy is not None:
            identifier(self.policy, "policy")
        for predecessor in self.order_after:
            identifier(predecessor, "ordering predecessor")
        _unique(self.order_after, "ordering dependencies")
        if self.design_campaign_iteration is not None and not isinstance(
            self.design_campaign_iteration,
            DesignCampaignIterationInput,
        ):
            raise FlowContractError("Flow node Design Campaign iteration input must be typed")
        object.__setattr__(
            self,
            "config",
            _portable_mapping(self.config, "Flow node config", FlowContractError),
        )


@dataclass(frozen=True)
class FlowTarget:
    target_id: str
    goals: tuple[str, ...]

    def __post_init__(self) -> None:
        identifier(self.target_id, "Flow target")
        for goal in self.goals:
            identifier(goal, "target goal")
        _unique(self.goals, "target goals")
        if not self.goals:
            raise FlowContractError("Flow target must declare at least one goal")


@dataclass(frozen=True)
class FlowSpec:
    owner: str
    flow_id: str
    nodes: tuple[FlowNode, ...]
    targets: tuple[FlowTarget, ...]
    policies: tuple[PolicySpec, ...] = ()
    owner_root: Path | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        owner_identity(self.owner, "Flow owner")
        identifier(self.flow_id, "Flow identity")
        _unique(tuple(node.node_id for node in self.nodes), "Flow nodes")
        _unique(tuple(target.target_id for target in self.targets), "Flow targets")
        _unique(tuple(policy.policy_id for policy in self.policies), "Flow policies")
        if not self.nodes:
            raise FlowContractError("Flow must declare at least one node")
        if not self.targets:
            raise FlowContractError("Flow must declare at least one target")
        if self.owner_root is not None:
            object.__setattr__(self, "owner_root", Path(self.owner_root).resolve())

    def node(self, node_id: str) -> FlowNode:
        try:
            return next(node for node in self.nodes if node.node_id == node_id)
        except StopIteration as exc:
            raise FlowContractError(f"unknown Flow node: {node_id!r}") from exc

    def target(self, target_id: str) -> FlowTarget:
        try:
            return next(target for target in self.targets if target.target_id == target_id)
        except StopIteration as exc:
            raise FlowContractError(f"unknown Flow target: {target_id!r}") from exc

    def policy(self, policy_id: str) -> PolicySpec:
        try:
            return next(
                policy for policy in self.policies if policy.policy_id == policy_id
            )
        except StopIteration as exc:
            raise FlowContractError(f"unknown Flow policy: {policy_id!r}") from exc


@dataclass(frozen=True)
class PlannedNode:
    node: FlowNode
    adapter: str
    adapter_config: Mapping[str, Any]
    required_capabilities: tuple[str, ...]
    platform_assets: tuple[PlatformAssetRequirement, ...]
    dependencies: tuple[str, ...]
    execution_capability: str
    source_assets: SourceAssets | None = None

    def __post_init__(self) -> None:
        if self.execution_capability not in EXECUTION_CAPABILITIES:
            raise FlowContractError("planned node has an invalid execution capability")


@dataclass(frozen=True)
class FlowPlan:
    spec: FlowSpec
    profile: ExecutionProfile
    target: FlowTarget
    nodes: tuple[PlannedNode, ...]
    topology: tuple[str, ...]

    def planned_node(self, node_id: str) -> PlannedNode:
        try:
            return next(item for item in self.nodes if item.node.node_id == node_id)
        except StopIteration as exc:
            raise FlowContractError(f"node {node_id!r} is not in the plan") from exc


@dataclass(frozen=True)
class PolicyCheck:
    check_id: str
    fact: str
    operator: str
    expected: Any = None

    def __post_init__(self) -> None:
        identifier(self.check_id, "policy check")
        identifier(self.fact, "policy fact")
        if self.operator not in {"exists", "equals", "at_least", "at_most"}:
            raise FlowContractError(f"unsupported policy operator: {self.operator!r}")
        object.__setattr__(
            self,
            "expected",
            _portable_value(self.expected, "policy expectation", FlowContractError),
        )


@dataclass(frozen=True)
class PolicySpec:
    policy_id: str
    checks: tuple[PolicyCheck, ...]

    def __post_init__(self) -> None:
        identifier(self.policy_id, "policy identity")
        _unique(tuple(check.check_id for check in self.checks), "policy checks")
        if not self.checks:
            raise FlowContractError("Policy must declare at least one check")


@dataclass(frozen=True)
class CatalogSelection:
    spec: FlowSpec
    profile: ExecutionProfile


@dataclass(frozen=True)
class ResolvedCapability:
    """One current-site capability with a private executable location."""

    identity: str
    executable: Path | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        _semantic_identity(self.identity, "resolved capability identity")
        if self.executable is not None:
            # Tool launchers may be multi-call binaries whose selected mode is
            # derived from argv[0] (for example Synopsys dc_shell).  Normalize
            # the site path without dereferencing that semantic launcher.
            object.__setattr__(
                self,
                "executable",
                Path(os.path.abspath(self.executable)),
            )


@dataclass(frozen=True)
class ResolvedPlatformAssetMember:
    """One private site file or bounded directory selected for a platform asset."""

    role: str
    location: Path = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        identifier(self.role, "resolved platform asset member role")
        object.__setattr__(self, "location", Path(self.location).resolve())


@dataclass(frozen=True)
class ResolvedPlatformAsset:
    role: str
    kind: str
    identity: str
    members: tuple[ResolvedPlatformAssetMember, ...] = ()

    def __post_init__(self) -> None:
        identifier(self.role, "resolved platform asset role")
        identifier(self.kind, "resolved platform asset kind")
        _semantic_identity(self.identity, "resolved platform asset identity")
        _unique(
            tuple(member.role for member in self.members),
            "resolved platform asset member roles",
        )

    def member(self, role: str) -> ResolvedPlatformAssetMember | None:
        return next((member for member in self.members if member.role == role), None)


@dataclass(frozen=True)
class ExecutionEnvironment:
    """Semantic current-site identity supplied to preflight and execution."""

    capabilities: Mapping[str, ResolvedCapability] = field(default_factory=dict)
    platform_assets: tuple[ResolvedPlatformAsset, ...] = ()

    def __post_init__(self) -> None:
        for capability, resolved in self.capabilities.items():
            identifier(capability, "site capability")
            if not isinstance(resolved, ResolvedCapability):
                raise FlowContractError(
                    f"site capability {capability!r} must be resolved"
                )
        _unique(
            tuple(asset.role for asset in self.platform_assets),
            "resolved platform asset roles",
        )
        object.__setattr__(
            self,
            "capabilities",
            MappingProxyType(dict(self.capabilities)),
        )

    def platform_asset(self, role: str) -> ResolvedPlatformAsset | None:
        return next(
            (asset for asset in self.platform_assets if asset.role == role),
            None,
        )


@dataclass(frozen=True)
class PreflightCheck:
    requirement: str
    requirement_kind: str
    status: str
    expected: str | None = None
    identity: str | None = None


@dataclass(frozen=True)
class PreflightResult:
    status: str
    checks: tuple[PreflightCheck, ...]


@dataclass(frozen=True)
class InputArtifact:
    role: str
    kind: str
    path: Path
    producer: str
    qualifiers: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        identifier(self.role, "input artifact role")
        identifier(self.kind, "input artifact kind")
        object.__setattr__(self, "path", Path(self.path).resolve())
        object.__setattr__(
            self,
            "qualifiers",
            _qualifier_mapping(
                self.qualifiers,
                "input artifact qualifiers",
                FlowExecutionError,
            ),
        )


@dataclass(frozen=True)
class ProducedArtifact:
    role: str
    kind: str
    path: Path
    qualifiers: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        identifier(self.role, "produced artifact role")
        identifier(self.kind, "produced artifact kind")
        object.__setattr__(self, "path", Path(self.path).resolve())
        object.__setattr__(
            self,
            "qualifiers",
            _qualifier_mapping(
                self.qualifiers,
                "produced artifact qualifiers",
                FlowExecutionError,
            ),
        )


@dataclass(frozen=True)
class ActionArtifact:
    role: str
    kind: str
    path: Path
    relative_path: str
    producer: str
    qualifiers: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        identifier(self.role, "Action artifact role")
        identifier(self.kind, "Action artifact kind")
        object.__setattr__(self, "path", Path(self.path).resolve())
        object.__setattr__(
            self,
            "qualifiers",
            _qualifier_mapping(
                self.qualifiers,
                "Action artifact qualifiers",
                FlowExecutionError,
            ),
        )


@dataclass(frozen=True)
class AdapterExecution:
    status: str
    exit_code: int | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in _EXECUTION_STATUSES:
            raise FlowExecutionError(f"invalid execution status: {self.status!r}")
        object.__setattr__(
            self,
            "details",
            _portable_mapping(self.details, "execution details", FlowExecutionError),
        )

    @classmethod
    def succeeded(
        cls,
        *,
        exit_code: int = 0,
        details: Mapping[str, Any] | None = None,
    ) -> "AdapterExecution":
        return cls("succeeded", exit_code, details or {})


@dataclass(frozen=True)
class CollectedActionResult:
    status: str = "valid"
    artifacts: tuple[ProducedArtifact, ...] = ()
    facts: Mapping[str, Any] = field(default_factory=dict)
    evidence: tuple[Path, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in _RESULT_STATUSES:
            raise FlowExecutionError(f"invalid result status: {self.status!r}")
        object.__setattr__(
            self,
            "facts",
            _portable_mapping(self.facts, "Action Facts", FlowExecutionError),
        )
        object.__setattr__(
            self,
            "details",
            _portable_mapping(
                self.details,
                "Action result details",
                FlowExecutionError,
            ),
        )
        object.__setattr__(self, "evidence", tuple(Path(path) for path in self.evidence))


@dataclass(frozen=True)
class ActionContext:
    node_id: str
    action: ActionContract
    run_root: Path
    work_root: Path
    output_root: Path
    log_root: Path
    inputs: Mapping[str, InputArtifact]
    action_config: Mapping[str, Any]
    adapter_config: Mapping[str, Any]
    capabilities: Mapping[str, ResolvedCapability]
    platform_assets: Mapping[str, ResolvedPlatformAsset]
    source_assets: SourceAssets | None = None
    design_campaign_iteration: DesignCampaignIterationInput | None = None
    project_scope: ProjectScope | None = None

    def require_project_scope(self) -> ProjectScope:
        if self.project_scope is None:
            raise FlowExecutionError(
                f"Action {self.node_id!r} requires an explicit project owner scope"
            )
        return self.project_scope

    def input(self, role: str) -> InputArtifact:
        try:
            return self.inputs[role]
        except KeyError as exc:
            raise FlowExecutionError(
                f"Action {self.node_id!r} has no materialized input {role!r}"
            ) from exc

    def output_path(self, role: str, filename: str) -> Path:
        port = self.action.output(role)
        identifier(port.role, "output role")
        if (
            not isinstance(filename, str)
            or not filename
            or Path(filename).name != filename
            or filename in {".", ".."}
            or "/" in filename
            or "\\" in filename
        ):
            raise FlowExecutionError(f"unsafe output filename: {filename!r}")
        root = self.output_root / role
        root.mkdir(parents=True, exist_ok=True)
        result = root / filename
        if not result.resolve(strict=False).is_relative_to(self.output_root.resolve()):
            raise FlowExecutionError("Action output escaped its managed node directory")
        return result


@dataclass(frozen=True)
class NodeOutcome:
    node_id: str
    status: str
    execution_status: str | None
    result_status: str | None
    policy_status: str | None
    artifacts: Mapping[str, ActionArtifact]
    facts: Mapping[str, Any]
    reason: str | None = None


@dataclass(frozen=True)
class FlowResult:
    owner: str
    flow_id: str
    target: str
    run_id: str
    run_root: Path
    status: str
    interrupted: bool
    nodes: Mapping[str, NodeOutcome]


@dataclass(frozen=True)
class FlowProgress:
    run_id: str
    status: str
    completed_nodes: int
    total_nodes: int
    current_node: str | None

    def __post_init__(self) -> None:
        run_identity(self.run_id)
        if self.status not in _FLOW_PROGRESS_STATUSES:
            raise FlowExecutionError(f"invalid Flow progress status: {self.status!r}")
        if (
            type(self.completed_nodes) is not int
            or type(self.total_nodes) is not int
            or self.completed_nodes < 0
            or self.total_nodes < 0
            or self.completed_nodes > self.total_nodes
        ):
            raise FlowExecutionError("invalid Flow progress node counts")
        if self.current_node is not None:
            identifier(self.current_node, "Flow progress current node")
