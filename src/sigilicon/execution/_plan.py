"""Compile immutable typed steps and validate the execution DAG."""

from __future__ import annotations
from collections import deque
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Protocol, runtime_checkable
from sigilicon.canonical import canonical_digest
from sigilicon.paths import validate_artifact_id
from sigilicon.execution._source import Source
from sigilicon.execution._resources import ResourceBinding
from sigilicon.execution._values import (
    ContractError,
    ExecutionError,
    JsonValue,
    _DIGEST,
    _ENVIRONMENT,
    _LEVELS,
    _ROLES,
    _freeze,
    _identifier,
    _source_name,
    adapter_identity,
    json_value,
    resource_identity,
)


@dataclass(frozen=True)
class Evidence:
    """Cross-domain classification attached to one execution step."""

    role: str
    level: str
    scope: str

    def __post_init__(self) -> None:
        if not isinstance(self.role, str):
            raise ContractError("evidence role must be text")
        if not isinstance(self.level, str):
            raise ContractError("evidence level must be text")
        if self.role not in _ROLES:
            raise ContractError(f"unsupported evidence role: {self.role!r}")
        if self.level not in _LEVELS:
            raise ContractError(f"unsupported evidence level: {self.level!r}")
        if not isinstance(self.scope, str) or not self.scope.strip():
            raise ContractError("evidence scope must be non-empty text")

    @property
    def record(self) -> dict[str, str]:
        return {"role": self.role, "level": self.level, "scope": self.scope}


@dataclass(frozen=True)
class RuntimeEnvironment:
    """Owner-declared mapping from runner environment names to resource identities."""

    tools: Mapping[str, str] = field(default_factory=dict)
    files: Mapping[str, str] = field(default_factory=dict)
    directories: Mapping[str, str] = field(default_factory=dict)
    values: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        names: set[str] = set()
        for label in ("tools", "files", "directories", "values"):
            raw = getattr(self, label)
            if not isinstance(raw, Mapping):
                raise ContractError(f"runtime environment {label} must be a mapping")
            checked: dict[str, str] = {}
            for name, identity in raw.items():
                if not isinstance(name, str) or _ENVIRONMENT.fullmatch(name) is None:
                    raise ContractError(
                        f"invalid runtime environment name in {label}: {name!r}"
                    )
                if name in names:
                    raise ContractError(
                        f"runtime environment name is bound more than once: {name}"
                    )
                names.add(name)
                checked[name] = resource_identity(identity)
            object.__setattr__(self, label, MappingProxyType(checked))

    @property
    def record(self) -> dict[str, dict[str, str]]:
        return {
            label: dict(sorted(getattr(self, label).items()))
            for label in ("tools", "files", "directories", "values")
        }


@runtime_checkable
class PlannedAction(Protocol):
    """Adapter-owned typed action recorded as part of one Step."""

    @property
    def record(self) -> Mapping[str, JsonValue]: ...


@dataclass(frozen=True)
class Step:
    """One typed adapter action and its exact input closure."""

    id: str
    uses: str
    config: Mapping[str, JsonValue]
    needs: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    evidence: Evidence | None = None
    resources: tuple[str, ...] = ()
    runtime: RuntimeEnvironment = field(default_factory=RuntimeEnvironment)
    action: PlannedAction | None = field(default=None, repr=False, compare=False)
    source_closure: tuple[Source, ...] = field(default=(), repr=False, compare=False)
    resource_closure: tuple[ResourceBinding, ...] = field(
        default=(), repr=False, compare=False
    )
    _action_identity: str | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _identifier(self.id, "step id"))
        object.__setattr__(self, "uses", adapter_identity(self.uses))
        if not isinstance(self.config, Mapping):
            raise ContractError("step config must be a mapping")
        if not isinstance(self.needs, tuple):
            raise ContractError("prepared step needs must be a tuple")
        needs = tuple(_identifier(value, "step dependency") for value in self.needs)
        if self.id in needs or len(needs) != len(set(needs)):
            raise ContractError(f"step {self.id!r} has invalid dependencies")
        if not isinstance(self.sources, tuple):
            raise ContractError("prepared step sources must be a tuple")
        sources = tuple(_source_name(source) for source in self.sources)
        if len(sources) != len(set(sources)):
            raise ContractError("prepared step sources contain duplicates")
        if self.evidence is not None and not isinstance(self.evidence, Evidence):
            raise ContractError("prepared step evidence must be an Evidence value")
        if not isinstance(self.resources, tuple):
            raise ContractError("prepared step resources must be a tuple")
        resources = tuple(resource_identity(value) for value in self.resources)
        if len(resources) != len(set(resources)):
            raise ContractError("prepared step resources contain duplicates")
        if not isinstance(self.runtime, RuntimeEnvironment):
            raise ContractError("prepared step runtime must be a RuntimeEnvironment")
        if self.action is not None:
            if not isinstance(self.action, PlannedAction):
                raise ContractError("step action must expose one portable record")
            action_record = _freeze(self.action.record, "step action")
            object.__setattr__(
                self,
                "_action_identity",
                canonical_digest(json_value(action_record)),
            )
        if not isinstance(self.source_closure, tuple) or any(
            not isinstance(source, Source) for source in self.source_closure
        ):
            raise ContractError("step source closure must contain Source values")
        if len({source.path for source in self.source_closure}) != len(
            self.source_closure
        ):
            raise ContractError("step source closure contains duplicate names")
        captured_sources = tuple(source.path for source in self.source_closure)
        if captured_sources:
            if sources and set(sources) != set(captured_sources):
                raise ContractError("step source names disagree with their exact closure")
            if not sources:
                sources = captured_sources
        if not isinstance(self.resource_closure, tuple) or any(
            not isinstance(resource, ResourceBinding)
            for resource in self.resource_closure
        ):
            raise ContractError(
                "step resource closure must contain ResourceBinding values"
            )
        if len({resource.identity for resource in self.resource_closure}) != len(
            self.resource_closure
        ):
            raise ContractError("step resource closure contains duplicate identities")
        captured_resources = tuple(
            resource.identity for resource in self.resource_closure
        )
        if captured_resources:
            if resources and set(resources) != set(captured_resources):
                raise ContractError(
                    "step resource names disagree with their exact closure"
                )
            if not resources:
                resources = captured_resources
        object.__setattr__(self, "needs", needs)
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "resources", resources)
        object.__setattr__(self, "config", _freeze(self.config, "step config"))

    def validate_action(self) -> None:
        """Reject mutation of an adapter-owned action after planning."""

        if self.action is None:
            if self._action_identity is not None:
                raise ExecutionError("step action identity drift")
            return
        current = canonical_digest(json_value(self.action.record))
        if current != self._action_identity:
            raise ExecutionError("step action identity drift")

    @property
    def record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "uses": self.uses,
            "needs": list(self.needs),
            "config": json_value(self.config),
            "action": None if self.action is None else json_value(self.action.record),
            "sources": list(self.sources),
            "resources": list(self.resources),
            "runtime": self.runtime.record,
            "evidence": None if self.evidence is None else self.evidence.record,
        }


def _topology(steps: tuple[Step, ...]) -> tuple[Step, ...]:
    by_id = {step.id: step for step in steps}
    if len(by_id) != len(steps):
        raise ContractError("operation contains duplicate step ids")
    unknown = {
        dependency
        for step in steps
        for dependency in step.needs
        if dependency not in by_id
    }
    if unknown:
        raise ContractError(f"operation references unknown step dependencies: {sorted(unknown)}")
    indegree = {step.id: len(step.needs) for step in steps}
    dependents: dict[str, list[Step]] = {step.id: [] for step in steps}
    for step in steps:
        for dependency in step.needs:
            dependents[dependency].append(step)
    ready = deque(step for step in steps if indegree[step.id] == 0)
    ordered: list[Step] = []
    while ready:
        step = ready.popleft()
        ordered.append(step)
        for dependent in dependents[step.id]:
            indegree[dependent.id] -= 1
            if indegree[dependent.id] == 0:
                ready.append(dependent)
    if len(ordered) != len(steps):
        raise ContractError("operation step graph contains a cycle")
    return tuple(ordered)


@dataclass(frozen=True)
class ExecutionPlan:
    """Complete immutable source and runtime closure for one operation."""

    project_identity: str
    owner: str
    operation: str
    variant: str | None
    steps: tuple[Step, ...]
    sources: tuple[Source, ...]
    resources: tuple[ResourceBinding, ...] = field(repr=False)
    _composition_sources: tuple[Source, ...] = field(
        default=(),
        init=False,
        repr=False,
        compare=False,
    )
    _authority: object | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    _identity: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.project_identity, str) or _DIGEST.fullmatch(
            self.project_identity
        ) is None:
            raise ContractError("execution plan project identity must be a SHA-256 digest")
        object.__setattr__(self, "owner", _identifier(self.owner, "owner"))
        object.__setattr__(self, "operation", _identifier(self.operation, "operation"))
        if self.variant is not None:
            object.__setattr__(self, "variant", _identifier(self.variant, "variant"))
        if not isinstance(self.steps, tuple) or not self.steps:
            raise ContractError("execution plan must contain at least one step")
        if any(not isinstance(step, Step) for step in self.steps):
            raise ContractError("execution plan steps must be Step values")
        if not isinstance(self.sources, tuple) or not self.sources:
            raise ContractError("execution plan must retain its operation source")
        if any(not isinstance(source, Source) for source in self.sources):
            raise ContractError("execution plan sources must be Source values")
        if not isinstance(self._composition_sources, tuple) or any(
            not isinstance(source, Source) for source in self._composition_sources
        ):
            raise ContractError("execution plan composition monitor is invalid")
        closure = {(source.root, source.path): source for source in self.sources}
        if len(closure) != len(self.sources):
            raise ContractError("execution plan contains duplicate source identities")
        if len({source.path for source in self.sources}) != len(self.sources):
            raise ContractError("execution plan source paths collide across scopes")
        source_names = {item.path for item in self.sources}
        for step in self.steps:
            for source in step.sources:
                if source not in source_names:
                    raise ContractError(
                        f"step {step.id!r} source is outside the plan source closure"
                    )
        if not isinstance(self.resources, tuple) or any(
            not isinstance(resource, ResourceBinding) for resource in self.resources
        ):
            raise ContractError("execution plan resources must be ResourceBinding values")
        resource_closure = {
            resource.identity: resource for resource in self.resources
        }
        if len(resource_closure) != len(self.resources):
            raise ContractError("execution plan contains duplicate resource identities")
        referenced = {resource for step in self.steps for resource in step.resources}
        if referenced != set(resource_closure):
            raise ContractError(
                "execution plan resource closure disagrees with its steps"
            )
        object.__setattr__(self, "steps", _topology(self.steps))
        object.__setattr__(self, "_identity", canonical_digest(self.record))

    @property
    def record(self) -> dict[str, Any]:
        return {
            "schema": 16,
            "contract_kind": "execution-plan",
            "project_identity": self.project_identity,
            "owner": self.owner,
            "operation": self.operation,
            "variant": self.variant,
            "sources": [source.record for source in self.sources],
            "resources": [resource.record for resource in self.resources],
            "steps": [step.record for step in self.steps],
        }

    @property
    def identity(self) -> str:
        return self._identity


def _bind_execution_plan(
    plan: ExecutionPlan,
    *,
    composition_sources: tuple[Source, ...],
    authority: object,
) -> ExecutionPlan:
    """Bind Project-only monitoring and authority to a new public plan value."""

    if not isinstance(composition_sources, tuple) or any(
        not isinstance(source, Source) for source in composition_sources
    ):
        raise ContractError("execution plan composition monitor is invalid")
    object.__setattr__(plan, "_composition_sources", composition_sources)
    object.__setattr__(plan, "_authority", authority)
    return plan


@dataclass(frozen=True)
class PreflightCheck:
    kind: str
    subject: str
    status: str
    detail: str = ""

    def __post_init__(self) -> None:
        if self.status not in {"ready", "blocked"}:
            raise ContractError(f"invalid preflight status: {self.status!r}")
        if not all(isinstance(value, str) and value for value in (self.kind, self.subject)):
            raise ContractError("preflight check kind and subject must be non-empty")
        if not isinstance(self.detail, str):
            raise ContractError("preflight detail must be text")

    @property
    def record(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "subject": self.subject,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class PreflightResult:
    plan_identity: str
    checks: tuple[PreflightCheck, ...]

    def __post_init__(self) -> None:
        validate_artifact_id(self.plan_identity, "plan identity")
        if not isinstance(self.checks, tuple) or any(
            not isinstance(check, PreflightCheck) for check in self.checks
        ):
            raise ContractError("preflight checks must be PreflightCheck values")

    @property
    def ready(self) -> bool:
        return all(check.status == "ready" for check in self.checks)

    @property
    def status(self) -> str:
        return "ready" if self.ready else "blocked"

    @property
    def record(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "contract_kind": "preflight-result",
            "plan_identity": self.plan_identity,
            "status": self.status,
            "checks": [check.record for check in self.checks],
        }
