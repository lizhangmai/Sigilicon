"""Typed interfaces for deterministic design Flow planning and execution."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
from pathlib import Path, PurePosixPath
import re
from types import MappingProxyType
from typing import Any, Callable, Mapping, TypeVar

from sigilicon.identifiers import RUN_ID_PATTERN
from sigilicon.paths import ProjectScope, validate_artifact_id
from sigilicon.flow.errors import FlowContractError, FlowExecutionError
from sigilicon.flow.evidence import FactAtom, FactSchema, FactSet, FactSource, FactSpec


_IDENTIFIER_RE = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*\Z")
_OWNER_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[._-][A-Za-z0-9]+)*\Z")
_RUN_ID_RE = re.compile(RUN_ID_PATTERN + r"\Z")
_REQUIREMENTS = frozenset({"accepted", "valid"})
_RESULT_STATUSES = frozenset({"valid", "failed", "partial", "uncertain"})
_EXECUTION_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
_FLOW_PROGRESS_STATUSES = frozenset({"running", "accepted", "failed", "cancelled"})
EXECUTION_CAPABILITIES = frozenset({"execute-derived", "mutate-workspace"})
EVIDENCE_ROLES = frozenset(
    {"diagnostic", "regression", "qualification", "signoff"}
)
EVIDENCE_LEVELS = frozenset({"l0", "l1", "l2", "l3", "l4"})
RECIPE_INPUT_KINDS = frozenset(
    {
        "text",
        "boolean",
        "integer",
        "real",
        "owner-path",
        "semantic-identity",
        "scalar-map",
    }
)
_T = TypeVar("_T")


@dataclass(frozen=True)
class InputReference:
    """A whole-value reference resolved while compiling an Execution Recipe."""

    name: str

    def __post_init__(self) -> None:
        identifier(self.name, "recipe input reference")


@dataclass(frozen=True)
class RecipeInput:
    """One typed value accepted by an owner target operation."""

    name: str
    kind: str

    def __post_init__(self) -> None:
        identifier(self.name, "recipe input name")
        if self.kind not in RECIPE_INPUT_KINDS:
            raise FlowContractError(
                f"unsupported recipe input kind {self.kind!r}; "
                f"expected one of {sorted(RECIPE_INPUT_KINDS)}"
            )


@dataclass(frozen=True)
class ActionConfiguration(Mapping[str, Any]):
    """One Action-owned configuration compiled from its wire payload."""

    action_kind: str
    values: Mapping[str, Any]

    def __post_init__(self) -> None:
        identifier(self.action_kind, "action configuration kind")
        object.__setattr__(
            self,
            "values",
            _portable_mapping(
                self.values,
                "Action configuration",
                FlowContractError,
            ),
        )

    def __getitem__(self, name: str) -> Any:
        return self.values[name]

    def __iter__(self):
        return iter(self.values)

    def __len__(self) -> int:
        return len(self.values)


@dataclass(frozen=True)
class AdapterConfiguration(Mapping[str, Any]):
    """One Adapter-owned configuration compiled from an Action binding."""

    adapter: str
    values: Mapping[str, Any]

    def __post_init__(self) -> None:
        identifier(self.adapter, "Adapter configuration identity")
        object.__setattr__(
            self,
            "values",
            _portable_mapping(
                self.values,
                "Adapter configuration",
                FlowContractError,
            ),
        )

    def __getitem__(self, name: str) -> Any:
        return self.values[name]

    def __iter__(self):
        return iter(self.values)

    def __len__(self) -> int:
        return len(self.values)


@dataclass(frozen=True)
class EvidenceEnvelope:
    """Cross-domain evidence classification; domain payloads remain separate."""

    role: str
    level: str
    scope: str

    def __post_init__(self) -> None:
        if self.role not in EVIDENCE_ROLES:
            raise FlowContractError(f"unsupported evidence role: {self.role!r}")
        if self.level not in EVIDENCE_LEVELS:
            raise FlowContractError(f"unsupported evidence level: {self.level!r}")
        _semantic_identity(self.scope, "evidence scope")

    @classmethod
    def from_action_config(
        cls,
        config: Mapping[str, Any],
    ) -> "EvidenceEnvelope | None":
        fields = ("evidence_role", "evidence_level", "evidence_scope")
        present = tuple(name for name in fields if name in config)
        if not present:
            return None
        if len(present) != len(fields):
            missing = sorted(set(fields) - set(present))
            raise FlowContractError(
                f"evidence envelope is missing fields: {missing}"
            )
        values = tuple(config[name] for name in fields)
        if any(not isinstance(value, str) for value in values):
            raise FlowContractError("evidence envelope fields must be text")
        return cls(*values)


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
    if isinstance(value, InputReference):
        return value
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


def _contains_input_reference(value: Any) -> bool:
    """Return whether a value still contains an Execution Recipe reference."""

    if isinstance(value, InputReference):
        return True
    if isinstance(value, Mapping):
        return any(_contains_input_reference(item) for item in value.values())
    if isinstance(value, (tuple, list)):
        return any(_contains_input_reference(item) for item in value)
    return False


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
    """One scope-relative member selected from the current Git checkout."""

    path: str
    source_root: Path = field(repr=False)
    record_text: str = field(repr=False)
    executable: bool
    location: Path = field(repr=False, compare=False)
    scope: str = "project"

    def __post_init__(self) -> None:
        relative = PurePosixPath(self.path)
        if (
            not self.path
            or relative.is_absolute()
            or "\\" in self.path
            or relative.as_posix() != self.path
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise FlowContractError(
                f"source member must be scope-relative: {self.path!r}"
            )
        if not isinstance(self.record_text, str):
            raise FlowContractError("source member record must be exact UTF-8 text")
        if not isinstance(self.executable, bool):
            raise FlowContractError("source member executable flag must be boolean")
        identifier(self.scope, "source member scope")
        root = Path(self.source_root).resolve()
        location = Path(self.location).resolve()
        expected = root.joinpath(*relative.parts).resolve(strict=False)
        if location != expected or not location.is_relative_to(root):
            raise FlowContractError(
                "source member location disagrees with its source root"
            )
        object.__setattr__(self, "source_root", root)
        object.__setattr__(self, "location", location)


@dataclass(frozen=True)
class ActionPlan:
    """One explicit, source-bound domain plan consumed by an Action.

    ``record`` is the portable identity persisted with the Flow plan. ``value``
    is the already-resolved typed object used by the Adapter; it is deliberately
    not reconstructed from the persisted projection during the same execution.
    """

    kind: str
    value: object = field(repr=False, compare=False)
    record: Mapping[str, Any]
    sources: tuple[SourceMember, ...] = ()

    def __post_init__(self) -> None:
        identifier(self.kind, "Action Plan kind")
        if isinstance(self.sources, (str, bytes)):
            raise FlowContractError("Action Plan sources must be a SourceMember tuple")
        try:
            sources = tuple(self.sources)
        except TypeError as exc:
            raise FlowContractError(
                "Action Plan sources must be a SourceMember tuple"
            ) from exc
        if any(not isinstance(member, SourceMember) for member in sources):
            raise FlowContractError("Action Plan sources must be SourceMember values")
        if not sources:
            raise FlowContractError("Action Plan must declare explicit sources")
        object.__setattr__(self, "sources", sources)
        object.__setattr__(
            self,
            "record",
            _portable_mapping(
                self.record,
                "Action Plan record",
                FlowContractError,
            ),
        )
        identities = tuple(
            (member.scope, member.source_root, member.path) for member in self.sources
        )
        if len(identities) != len(set(identities)):
            raise FlowContractError("Action Plan sources contain duplicates")

    def require_value(self, expected_type: type[_T]) -> _T:
        if not isinstance(self.value, expected_type):
            raise FlowExecutionError(
                f"Action Plan {self.kind!r} has value {type(self.value).__name__}, "
                f"expected {expected_type.__name__}"
            )
        return self.value


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
    repository_root: Path = field(repr=False)
    scope_root: Path = field(repr=False)

    def __post_init__(self) -> None:
        if re.fullmatch(r"[0-9a-f]{40,64}", self.commit) is None:
            raise FlowContractError("Git source commit is invalid")
        repository_root = Path(self.repository_root).resolve()
        scope_root = Path(self.scope_root).resolve()
        if not scope_root.is_relative_to(repository_root):
            raise FlowContractError("Git source scope is outside its repository")
        object.__setattr__(self, "repository_root", repository_root)
        object.__setattr__(self, "scope_root", scope_root)

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
    contract_source: SourceMember
    owner_root: Path = field(repr=False)
    selection: str | None = None

    def __post_init__(self) -> None:
        owner_identity(self.owner, "source assets owner")
        identifier(self.name, "source assets identity")
        _unique(
            tuple(artifact.role for artifact in self.artifacts),
            "source artifact roles",
        )
        if not self.artifacts:
            raise FlowContractError("source assets declare no artifacts")
        if self.selection is not None:
            identifier(self.selection, "source assets selection")
        owner_root = Path(self.owner_root).resolve()
        if self.git.scope_root != owner_root:
            raise FlowContractError(
                "Git source scope disagrees with the Source Assets owner root"
            )
        if not isinstance(self.contract_source, SourceMember):
            raise FlowContractError("source assets contract source must be a SourceMember")
        if self.contract_source.source_root != owner_root:
            raise FlowContractError(
                "source assets contract source root disagrees with the owner root"
            )
        if any(
            member.source_root != owner_root
            for artifact in self.artifacts
            for member in artifact.members
        ):
            raise FlowContractError(
                "source member root disagrees with the Source Assets owner root"
            )
        object.__setattr__(self, "owner_root", owner_root)

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
    fact_schema: FactSchema | None = None
    required_capabilities: tuple[str, ...] = ()
    platform_assets: tuple[PlatformAssetRequirement, ...] = ()
    adapters: tuple[str, ...] = ()
    adapter_extensible: bool = False
    resolves_source_assets: bool = False
    execution_capability: str = "execute-derived"
    accepted_extensions: tuple[str, ...] = ()
    plan_input_kind: str | None = None

    def __post_init__(self) -> None:
        identifier(self.kind, "action kind")
        if self.fact_schema is None:
            object.__setattr__(self, "fact_schema", FactSchema(self.kind))
        elif not isinstance(self.fact_schema, FactSchema):
            raise FlowContractError("Action fact schema must be a FactSchema")
        elif self.fact_schema.action_kind != self.kind:
            raise FlowContractError(
                f"Action {self.kind!r} fact schema action kind disagrees"
            )
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
        _unique(self.required_capabilities, "Action required capabilities")
        for value in self.accepted_extensions:
            identifier(value, "Action extension")
        if self.plan_input_kind is not None:
            identifier(self.plan_input_kind, "Action Plan input kind")
        _unique(self.accepted_extensions, "Action extensions")
        _unique(
            tuple(requirement.role for requirement in self.platform_assets),
            "Action platform asset roles",
        )
        _unique(self.adapters, "Action Adapters")
        if not self.adapters and not self.adapter_extensible:
            raise FlowContractError(f"Action {self.kind!r} declares no Adapter")
        if self.resolves_source_assets and self.inputs:
            raise FlowContractError("source assets Action cannot declare inputs")

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
class ActionBinding:
    """Owner-selected Adapter binding for one Action kind in a Flow recipe.

    The binding is part of the Flow intent that is compiled for execution.  It
    is deliberately not a separate selection object: the selected Adapter, additional
    capabilities, platform identities and Adapter configuration are one
    source-owned decision at the Flow interface.
    """

    action_kind: str
    adapter: str
    config: Mapping[str, Any] = field(default_factory=dict)
    requires: tuple[str, ...] = ()
    platform_assets: Mapping[str, str | InputReference] = field(default_factory=dict)

    def __post_init__(self) -> None:
        identifier(self.action_kind, "Action binding kind")
        identifier(self.adapter, "Action binding Adapter")
        if isinstance(self.requires, (str, bytes)):
            raise FlowContractError("Action binding requires must be a string tuple")
        try:
            requires = tuple(self.requires)
        except TypeError as exc:
            raise FlowContractError(
                "Action binding requires must be a string tuple"
            ) from exc
        for capability in requires:
            identifier(capability, "Action binding capability")
        _unique(requires, "Action binding capabilities")
        object.__setattr__(self, "requires", requires)
        identities: dict[str, str | InputReference] = {}
        if not isinstance(self.platform_assets, Mapping):
            raise FlowContractError("Action binding platform assets must be a mapping")
        for role, identity in self.platform_assets.items():
            identifier(role, "Action binding platform asset role")
            if isinstance(identity, InputReference):
                identities[role] = identity
            else:
                identities[role] = _semantic_identity(
                    identity,
                    "Action binding platform asset identity",
                )
        object.__setattr__(
            self,
            "platform_assets",
            MappingProxyType(identities),
        )
        object.__setattr__(
            self,
            "config",
            _portable_mapping(
                self.config,
                "Action binding configuration",
                FlowContractError,
            ),
        )


@dataclass(frozen=True)
class FlowNode:
    node_id: str
    action_kind: str
    config: Mapping[str, Any] = field(default_factory=dict)
    bindings: tuple[ArtifactBinding, ...] = ()
    order_after: tuple[str, ...] = ()
    policy: str | None = None
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        identifier(self.node_id, "Flow node")
        identifier(self.action_kind, "action kind")
        if self.policy is not None:
            identifier(self.policy, "policy")
        for predecessor in self.order_after:
            identifier(predecessor, "ordering predecessor")
        _unique(self.order_after, "ordering dependencies")
        for name in self.extensions:
            identifier(name, "Flow node extension")
        object.__setattr__(
            self,
            "extensions",
            _portable_mapping(
                self.extensions,
                "Flow node extensions",
                FlowContractError,
            ),
        )
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
class ExecutionRecipe:
    """Owner-owned executable recipe without target selection.

    An owner target catalog selects an operation and supplies its goals.  This
    recipe contains only the operation graph, policies and Action bindings;
    target selection is intentionally compiled into :class:`FlowSpec` later.
    """

    owner: str
    recipe_id: str
    nodes: tuple[FlowNode, ...]
    policies: tuple[PolicySpec, ...] = ()
    action_bindings: tuple[ActionBinding, ...] = ()
    inputs: Mapping[str, RecipeInput] = field(default_factory=dict)
    owner_root: Path | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        owner_identity(self.owner, "Execution Recipe owner")
        identifier(self.recipe_id, "Execution Recipe identity")
        object.__setattr__(self, "nodes", tuple(self.nodes))
        object.__setattr__(self, "policies", tuple(self.policies))
        object.__setattr__(self, "action_bindings", tuple(self.action_bindings))
        if not isinstance(self.inputs, Mapping):
            raise FlowContractError("Execution Recipe inputs must be a mapping")
        inputs = dict(self.inputs)
        if any(
            not isinstance(name, str)
            or not isinstance(value, RecipeInput)
            or value.name != name
            for name, value in inputs.items()
        ):
            raise FlowContractError("Execution Recipe inputs must be named RecipeInput values")
        object.__setattr__(self, "inputs", MappingProxyType(inputs))
        _unique(tuple(node.node_id for node in self.nodes), "Execution Recipe nodes")
        _unique(
            tuple(policy.policy_id for policy in self.policies),
            "Execution Recipe policies",
        )
        _unique(
            tuple(binding.action_kind for binding in self.action_bindings),
            "Execution Recipe Action bindings",
        )
        if not self.nodes:
            raise FlowContractError("Execution Recipe must declare at least one node")
        if not self.action_bindings:
            raise FlowContractError(
                "Execution Recipe must declare at least one Action binding"
            )
        bound_actions = {binding.action_kind for binding in self.action_bindings}
        node_actions = {node.action_kind for node in self.nodes}
        missing = sorted(node_actions - bound_actions)
        if missing:
            raise FlowContractError(
                "Execution Recipe is missing Action bindings: "
                f"{missing}"
            )
        if self.owner_root is not None:
            object.__setattr__(self, "owner_root", Path(self.owner_root).resolve())

    def node(self, node_id: str) -> FlowNode:
        try:
            return next(node for node in self.nodes if node.node_id == node_id)
        except StopIteration as exc:
            raise FlowContractError(f"unknown Execution Recipe node: {node_id!r}") from exc

    def policy(self, policy_id: str) -> PolicySpec:
        try:
            return next(
                policy for policy in self.policies if policy.policy_id == policy_id
            )
        except StopIteration as exc:
            raise FlowContractError(
                f"unknown Execution Recipe policy: {policy_id!r}"
            ) from exc

    def action_binding(self, action_kind: str) -> ActionBinding:
        try:
            return next(
                binding
                for binding in self.action_bindings
                if binding.action_kind == action_kind
            )
        except StopIteration as exc:
            raise FlowContractError(
                f"Execution Recipe {self.recipe_id!r} has no Action binding for "
                f"{action_kind!r}"
            ) from exc


@dataclass(frozen=True)
class FlowSpec:
    owner: str
    flow_id: str
    recipe_id: str
    nodes: tuple[FlowNode, ...]
    targets: tuple[FlowTarget, ...]
    policies: tuple[PolicySpec, ...] = ()
    action_bindings: tuple[ActionBinding, ...] = ()
    inputs: Mapping[str, Any] = field(default_factory=dict)
    source_members: tuple[SourceMember, ...] = ()
    owner_root: Path | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        owner_identity(self.owner, "Flow owner")
        identifier(self.flow_id, "Flow identity")
        identifier(self.recipe_id, "Flow recipe identity")
        object.__setattr__(self, "nodes", tuple(self.nodes))
        object.__setattr__(self, "targets", tuple(self.targets))
        object.__setattr__(self, "policies", tuple(self.policies))
        object.__setattr__(self, "action_bindings", tuple(self.action_bindings))
        object.__setattr__(
            self,
            "inputs",
            _portable_mapping(self.inputs, "Flow inputs", FlowContractError),
        )
        if _contains_input_reference(self.inputs):
            raise FlowContractError("compiled Flow inputs contain an unresolved reference")
        if any(
            _contains_input_reference(node.config)
            or _contains_input_reference(node.extensions)
            for node in self.nodes
        ):
            raise FlowContractError("compiled Flow nodes contain an unresolved input reference")
        if any(
            _contains_input_reference(binding.config)
            or _contains_input_reference(binding.platform_assets)
            for binding in self.action_bindings
        ):
            raise FlowContractError(
                "compiled Flow Action bindings contain an unresolved input reference"
            )
        object.__setattr__(self, "source_members", tuple(self.source_members))
        _unique(tuple(node.node_id for node in self.nodes), "Flow nodes")
        _unique(tuple(target.target_id for target in self.targets), "Flow targets")
        _unique(tuple(policy.policy_id for policy in self.policies), "Flow policies")
        _unique(
            tuple(binding.action_kind for binding in self.action_bindings),
            "Flow Action bindings",
        )
        if any(
            not isinstance(member, SourceMember) for member in self.source_members
        ):
            raise FlowContractError("Flow source members must be SourceMember values")
        source_identities = tuple(
            (member.scope, member.source_root, member.path)
            for member in self.source_members
        )
        if len(source_identities) != len(set(source_identities)):
            raise FlowContractError("duplicate Flow source members")
        if not self.nodes:
            raise FlowContractError("Flow must declare at least one node")
        if not self.targets:
            raise FlowContractError("Flow must declare at least one target")
        if not self.action_bindings:
            raise FlowContractError("Flow must declare Action bindings")
        bound_actions = {binding.action_kind for binding in self.action_bindings}
        node_actions = {node.action_kind for node in self.nodes}
        missing = sorted(node_actions - bound_actions)
        if missing:
            raise FlowContractError(
                "Flow is missing Action bindings: "
                f"{missing}"
            )
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

    def action_binding(self, action_kind: str) -> ActionBinding:
        try:
            return next(
                binding
                for binding in self.action_bindings
                if binding.action_kind == action_kind
            )
        except StopIteration as exc:
            raise FlowContractError(
                f"Flow {self.flow_id!r} has no Action binding for {action_kind!r}"
            ) from exc

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
    action_config: ActionConfiguration
    adapter_config: AdapterConfiguration
    evidence: EvidenceEnvelope | None
    required_capabilities: tuple[str, ...]
    platform_assets: tuple[PlatformAssetRequirement, ...]
    dependencies: tuple[str, ...]
    execution_capability: str
    fact_schema: FactSchema
    policy: BoundPolicySpec | None = None
    source_assets: SourceAssets | None = None
    action_plan: ActionPlan | None = None

    def __post_init__(self) -> None:
        if self.execution_capability not in EXECUTION_CAPABILITIES:
            raise FlowContractError("planned node has an invalid execution capability")


@dataclass(frozen=True)
class FlowPlan:
    spec: FlowSpec
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
    expected: object = None

    def __post_init__(self) -> None:
        identifier(self.check_id, "policy check")
        identifier(self.fact, "policy fact")
        if self.operator not in {"exists", "equals", "at_least", "at_most"}:
            raise FlowContractError(f"unsupported policy operator: {self.operator!r}")
        if self.operator == "exists" and self.expected is not None:
            raise FlowContractError(
                "exists policy checks cannot declare an expected value"
            )
        if self.expected is not None and type(self.expected) not in {
            bool,
            int,
            float,
            str,
        }:
            raise FlowContractError(
                "policy expectation must be a scalar or null"
            )
        if isinstance(self.expected, float) and not math.isfinite(self.expected):
            raise FlowContractError("policy expectation must be finite")
        object.__setattr__(
            self,
            "expected",
            self.expected,
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
class BoundPolicyCheck:
    """One policy check resolved against an Action's FactSchema."""

    check_id: str
    fact: FactSpec
    operator: str
    expected: FactAtom | None = None

    def __post_init__(self) -> None:
        identifier(self.check_id, "bound policy check")
        if not isinstance(self.fact, FactSpec):
            raise FlowContractError("bound policy fact must be a FactSpec")
        if self.operator not in {"exists", "equals", "at_least", "at_most"}:
            raise FlowContractError(
                f"unsupported bound policy operator: {self.operator!r}"
            )


@dataclass(frozen=True)
class BoundPolicySpec:
    """A PolicySpec compiled for one concrete Action fact schema."""

    policy_id: str
    schema: FactSchema
    checks: tuple[BoundPolicyCheck, ...]

    def __post_init__(self) -> None:
        identifier(self.policy_id, "bound policy identity")
        if not isinstance(self.schema, FactSchema):
            raise FlowContractError("bound policy schema must be a FactSchema")
        checks = tuple(self.checks)
        if not checks:
            raise FlowContractError("Bound Policy must declare at least one check")
        _unique(tuple(check.check_id for check in checks), "bound policy checks")
        if any(check.fact not in self.schema.fields for check in checks):
            raise FlowContractError("bound policy fact is outside its schema")
        object.__setattr__(self, "checks", checks)


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

    def __post_init__(self) -> None:
        if self.status not in _EXECUTION_STATUSES:
            raise FlowExecutionError(f"invalid execution status: {self.status!r}")

    @classmethod
    def succeeded(
        cls,
        *,
        exit_code: int = 0,
    ) -> "AdapterExecution":
        return cls("succeeded", exit_code)


@dataclass(frozen=True)
class CollectedActionResult:
    facts: FactSet
    status: str = "valid"
    artifacts: tuple[ProducedArtifact, ...] = ()
    evidence: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in _RESULT_STATUSES:
            raise FlowExecutionError(f"invalid result status: {self.status!r}")
        if not isinstance(self.facts, FactSet):
            raise FlowExecutionError("Action facts must be a FactSet")
        object.__setattr__(self, "evidence", tuple(Path(path) for path in self.evidence))


@dataclass(frozen=True)
class AdapterResult:
    """One complete Adapter invocation returned across the Engine seam.

    Backend execution and collected design evidence remain distinct facts, but
    callers no longer coordinate a public prepare/execute/collect lifecycle.
    """

    execution: AdapterExecution
    collected: CollectedActionResult | None = None

    def __post_init__(self) -> None:
        if self.execution.status == "succeeded" and self.collected is None:
            raise FlowExecutionError(
                "successful Adapter execution must include a collected result"
            )
        if self.execution.status != "succeeded" and self.collected is not None:
            raise FlowExecutionError(
                "unsuccessful Adapter execution cannot include a collected result"
            )

    @classmethod
    def succeeded(
        cls,
        collected: CollectedActionResult,
        *,
        exit_code: int = 0,
    ) -> "AdapterResult":
        return cls(
            AdapterExecution.succeeded(exit_code=exit_code),
            collected,
        )


class AdapterResultError(FlowExecutionError):
    """Evidence collection failed after a backend execution had completed."""

    def __init__(self, execution: AdapterExecution, cause: Exception) -> None:
        self.execution = execution
        self.cause = cause
        super().__init__(f"{type(cause).__name__}: {cause}")


@dataclass(frozen=True)
class ActionContext:
    node_id: str
    action: ActionContract
    run_root: Path
    work_root: Path
    output_root: Path
    log_root: Path
    inputs: Mapping[str, InputArtifact]
    action_config: ActionConfiguration
    adapter_config: AdapterConfiguration
    capabilities: Mapping[str, ResolvedCapability]
    platform_assets: Mapping[str, ResolvedPlatformAsset]
    source_assets: SourceAssets | None = None
    action_plan: ActionPlan | None = None
    evidence: EvidenceEnvelope | None = None
    extensions: Mapping[str, Any] = field(default_factory=dict)
    project_scope: ProjectScope | None = None
    operation_id: str | None = None
    _bind_workspace_operation: Callable[[Any], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if self.operation_id is not None:
            validate_artifact_id(self.operation_id, "Action operation id")

    def bind_workspace_operation(self, operation: Any) -> None:
        """Attach one real workspace safety boundary to this Action record."""

        if self.operation_id is None or self._bind_workspace_operation is None:
            raise FlowExecutionError(
                f"Action {self.node_id!r} has no managed operation record"
            )
        if getattr(operation, "operation_id", None) != self.operation_id:
            raise FlowExecutionError(
                f"Action {self.node_id!r} workspace operation identity drift"
            )
        self._bind_workspace_operation(operation)

    def require_evidence(self) -> EvidenceEnvelope:
        """Return the plan-validated cross-domain evidence classification."""

        if self.evidence is None:
            raise FlowExecutionError(
                f"Action {self.node_id!r} requires an evidence envelope"
            )
        return self.evidence

    def require_project_scope(self) -> ProjectScope:
        if self.project_scope is None:
            raise FlowExecutionError(
                f"Action {self.node_id!r} requires an explicit project owner scope"
            )
        return self.project_scope

    def require_action_plan(
        self,
        kind: str,
        expected_type: type[_T],
    ) -> _T:
        """Return the exact typed domain plan selected before preflight."""

        if self.action.plan_input_kind != kind:
            raise FlowExecutionError(
                f"Action {self.node_id!r} does not declare Plan input {kind!r}"
            )
        if self.action_plan is None or self.action_plan.kind != kind:
            raise FlowExecutionError(
                f"Action {self.node_id!r} is missing Plan input {kind!r}"
            )
        return self.action_plan.require_value(expected_type)

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
    facts: FactSet | None
    reason: str | None = None
    operation_id: str | None = None
    incident_reference: str | None = None


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
