"""Small typed interface for planning and running owner operations."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import math
from pathlib import Path, PurePosixPath
import re
import stat
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Callable, Mapping

from sigilicon.artifacts import (
    ensure_nofollow_directory,
    read_nofollow_text,
    write_immutable_text,
)
from sigilicon.canonical import canonical_digest
from sigilicon.paths import validate_artifact_component, validate_artifact_id

if TYPE_CHECKING:
    from sigilicon.execution.step_files import StepFiles


_BACKEND = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*\Z")
_DIGEST = re.compile(r"sha256-[0-9a-f]{64}\Z")
_RESOURCE = re.compile(r"[A-Za-z][A-Za-z0-9._:/-]{0,255}\Z")
_ROLES = frozenset({"diagnostic", "regression", "qualification", "signoff"})
_LEVELS = frozenset({"l0", "l1", "l2", "l3", "l4"})
_STEP_STATUSES = frozenset(
    {"succeeded", "failed", "blocked", "partial", "uncertain", "cancelled"}
)
_RUN_STATUSES = frozenset({"succeeded", "failed", "partial", "uncertain", "cancelled"})
_RUN_FAILURE_STATUSES = frozenset({"failed", "partial", "uncertain", "cancelled"})


class ContractError(ValueError):
    """An operation, plan, or backend value violates the execution contract."""


class ExecutionError(RuntimeError):
    """A managed operation could not be executed or restored safely."""


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{label} must be an identifier")
    try:
        return validate_artifact_component(value, label)
    except ValueError as exc:
        raise ContractError(str(exc)) from exc


def backend_identity(value: object) -> str:
    if not isinstance(value, str) or _BACKEND.fullmatch(value) is None:
        raise ContractError(f"invalid backend identity: {value!r}")
    return value


def resource_identity(value: object) -> str:
    if (
        not isinstance(value, str)
        or _RESOURCE.fullmatch(value) is None
        or value.startswith("/")
        or "\\" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ContractError(f"invalid external resource identity: {value!r}")
    return value


def resource_materialization_key(identity: str) -> str:
    logical = resource_identity(identity)
    return "resource-" + hashlib.sha256(logical.encode("utf-8")).hexdigest()


def _source_name(value: object) -> str:
    if not isinstance(value, str):
        raise ContractError("source name must be canonical relative text")
    relative = PurePosixPath(value)
    if (
        not value
        or relative.is_absolute()
        or "\\" in value
        or relative.as_posix() != value
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ContractError(f"source name must be canonical and relative: {value!r}")
    return value


def _freeze(value: Any, label: str) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractError(f"{label} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ContractError(f"{label} contains a non-string key")
        return MappingProxyType({key: _freeze(item, label) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item, label) for item in value)
    raise ContractError(f"{label} contains non-portable {type(value).__name__}")


def json_value(value: Any) -> Any:
    """Return a portable mutable projection of a frozen execution value."""

    if isinstance(value, Mapping):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [json_value(item) for item in value]
    return value


@dataclass(frozen=True)
class Evidence:
    """Cross-domain classification attached to one execution step."""

    role: str
    level: str
    scope: str

    def __post_init__(self) -> None:
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
class Source:
    """Exact no-follow snapshot of one project-owned source file."""

    path: str
    root: Path = field(repr=False, compare=False)
    text: str = field(repr=False, compare=False)
    executable: bool
    location: Path = field(repr=False, compare=False)
    scope: str = "project"
    device: int = field(default=-1, repr=False, compare=False)
    inode: int = field(default=-1, repr=False, compare=False)
    mtime_ns: int = field(default=-1, repr=False, compare=False)

    def __post_init__(self) -> None:
        relative = PurePosixPath(self.path)
        if (
            not self.path
            or relative.is_absolute()
            or "\\" in self.path
            or relative.as_posix() != self.path
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ContractError(f"source path must be canonical and relative: {self.path!r}")
        root = Path(self.root).resolve()
        location = Path(self.location).absolute()
        expected = root.joinpath(*relative.parts)
        if location != expected or location.resolve() != expected:
            raise ContractError("source location disagrees with its root or traverses a symlink")
        if not isinstance(self.text, str) or not isinstance(self.executable, bool):
            raise ContractError("source snapshot fields have invalid types")
        if not isinstance(self.scope, str) or _BACKEND.fullmatch(self.scope) is None:
            raise ContractError("source scope must be a semantic identity")
        if any(type(value) is not int for value in (self.device, self.inode, self.mtime_ns)):
            raise ContractError("source filesystem identity fields must be integers")
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "location", location)

    @classmethod
    def capture(
        cls,
        path: Path,
        *,
        root: Path,
        scope: str = "project",
    ) -> "Source":
        source_root = Path(root).resolve()
        configured = Path(path).absolute()
        resolved = configured.resolve()
        if configured != resolved or not resolved.is_relative_to(source_root):
            raise ContractError(f"source must be a non-symlink below {source_root}: {path}")
        metadata = resolved.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            raise ContractError(f"source must be a regular file: {path}")
        return cls(
            resolved.relative_to(source_root).as_posix(),
            source_root,
            read_nofollow_text(resolved),
            bool(metadata.st_mode & 0o111),
            resolved,
            scope,
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mtime_ns,
        )

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def record(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "path": self.path,
            "sha256": self.sha256,
            "executable": self.executable,
        }

    def current(self) -> bool:
        try:
            metadata = self.location.stat(follow_symlinks=False)
            return (
                self.location.absolute() == self.location.resolve()
                and stat.S_ISREG(metadata.st_mode)
                and read_nofollow_text(self.location) == self.text
                and bool(metadata.st_mode & 0o111) == self.executable
                and metadata.st_dev == self.device
                and metadata.st_ino == self.inode
                and metadata.st_mtime_ns == self.mtime_ns
            )
        except (OSError, RuntimeError, UnicodeError):
            return False


@dataclass(frozen=True)
class ExternalResource:
    """Exact host resource bound without exposing its path or content in records."""

    identity: str
    sha256: str
    text: str = field(repr=False, compare=False)
    location: Path = field(repr=False, compare=False)
    device: int = field(default=-1, repr=False, compare=False)
    inode: int = field(default=-1, repr=False, compare=False)
    mtime_ns: int = field(default=-1, repr=False, compare=False)

    def __post_init__(self) -> None:
        resource_identity(self.identity)
        if not isinstance(self.sha256, str) or re.fullmatch(
            r"[0-9a-f]{64}", self.sha256
        ) is None:
            raise ContractError("external resource digest must be lowercase SHA-256")
        if not isinstance(self.text, str):
            raise ContractError("external resource snapshot must be text")
        if hashlib.sha256(self.text.encode("utf-8")).hexdigest() != self.sha256:
            raise ContractError("external resource digest disagrees with its snapshot")
        location = Path(self.location).absolute()
        if location != location.resolve():
            raise ContractError("external resource must not traverse a symlink")
        if any(type(value) is not int for value in (self.device, self.inode, self.mtime_ns)):
            raise ContractError("external resource filesystem identity fields must be integers")
        object.__setattr__(self, "location", location)

    @classmethod
    def capture(cls, path: Path, *, identity: str) -> "ExternalResource":
        location = Path(path).absolute()
        if location != location.resolve():
            raise ContractError(f"external resource must not traverse a symlink: {path}")
        metadata = location.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            raise ContractError(f"external resource must be a regular file: {path}")
        text = read_nofollow_text(location)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return cls(
            resource_identity(identity),
            digest,
            text,
            location,
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mtime_ns,
        )

    @property
    def record(self) -> dict[str, str]:
        return {"identity": self.identity, "sha256": self.sha256}

    @property
    def materialization_key(self) -> str:
        return resource_materialization_key(self.identity)

    def current(self) -> bool:
        try:
            metadata = self.location.stat(follow_symlinks=False)
            return (
                self.location == self.location.resolve()
                and stat.S_ISREG(metadata.st_mode)
                and read_nofollow_text(self.location) == self.text
                and metadata.st_dev == self.device
                and metadata.st_ino == self.inode
                and metadata.st_mtime_ns == self.mtime_ns
            )
        except (OSError, RuntimeError, UnicodeError):
            return False


@dataclass(frozen=True)
class Operation:
    """Unprepared backend request compiled from an owner operation contract."""

    id: str
    uses: str
    config: Mapping[str, Any] = field(default_factory=dict)
    needs: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    evidence: Evidence | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _identifier(self.id, "step id"))
        object.__setattr__(self, "uses", backend_identity(self.uses))
        if not isinstance(self.config, Mapping):
            raise ContractError("step config must be a mapping")
        if not isinstance(self.needs, tuple):
            raise ContractError("step needs must be a tuple")
        needs = tuple(_identifier(value, "step dependency") for value in self.needs)
        if self.id in needs or len(needs) != len(set(needs)):
            raise ContractError(f"step {self.id!r} has invalid dependencies")
        if not isinstance(self.sources, tuple):
            raise ContractError("step sources must be a tuple")
        sources = tuple(_source_name(source) for source in self.sources)
        if len(sources) != len(set(sources)):
            raise ContractError("step sources contain duplicates")
        if self.evidence is not None and not isinstance(self.evidence, Evidence):
            raise ContractError("step evidence must be an Evidence value")
        object.__setattr__(self, "needs", needs)
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "config", _freeze(self.config, "step config"))

    @property
    def record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "uses": self.uses,
            "needs": list(self.needs),
            "with": json_value(self.config),
            "sources": list(self.sources),
            "evidence": None if self.evidence is None else self.evidence.record,
        }


@dataclass(frozen=True)
class Step:
    """Portable backend request authorized for preflight and execution."""

    id: str
    uses: str
    request: Mapping[str, Any] = field(default_factory=dict)
    needs: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    evidence: Evidence | None = None
    resources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _identifier(self.id, "step id"))
        object.__setattr__(self, "uses", backend_identity(self.uses))
        if not isinstance(self.request, Mapping):
            raise ContractError("prepared step request must be a mapping")
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
        resources = tuple(
            resource_identity(value)
            for value in self.resources
        )
        if len(resources) != len(set(resources)):
            raise ContractError("prepared step resources contain duplicates")
        object.__setattr__(self, "needs", needs)
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "resources", resources)
        object.__setattr__(self, "request", _freeze(self.request, "prepared step request"))

    @classmethod
    def from_operation(
        cls,
        step: Operation,
        *,
        request: Mapping[str, Any] | None = None,
        sources: tuple[str, ...] | None = None,
        resources: tuple[str, ...] = (),
    ) -> "Step":
        return cls(
            step.id,
            step.uses,
            step.config if request is None else request,
            step.needs,
            step.sources if sources is None else sources,
            step.evidence,
            resources,
        )

    @property
    def record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "uses": self.uses,
            "needs": list(self.needs),
            "request": json_value(self.request),
            "sources": list(self.sources),
            "resources": list(self.resources),
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
    ordered: list[Step] = []
    waiting = list(steps)
    while waiting:
        ready = [step for step in waiting if all(item in {done.id for done in ordered} for item in step.needs)]
        if not ready:
            raise ContractError("operation step graph contains a cycle")
        for step in ready:
            waiting.remove(step)
            ordered.append(step)
    return tuple(ordered)


@dataclass(frozen=True)
class OperationPlan:
    """Internal source snapshot awaiting package-owned Backend preparation."""

    project_identity: str
    owner: str
    operation: str
    variant: str | None
    operations: tuple[Operation, ...]
    sources: tuple[Source, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.project_identity, str) or _DIGEST.fullmatch(
            self.project_identity
        ) is None:
            raise ContractError("operation plan project identity must be a SHA-256 digest")
        object.__setattr__(self, "owner", _identifier(self.owner, "owner"))
        object.__setattr__(self, "operation", _identifier(self.operation, "operation"))
        if self.variant is not None:
            object.__setattr__(self, "variant", _identifier(self.variant, "variant"))
        if not isinstance(self.operations, tuple) or not self.operations:
            raise ContractError("operation plan must contain at least one step")
        if any(not isinstance(operation, Operation) for operation in self.operations):
            raise ContractError("operation plan must contain Operation values")
        if not isinstance(self.sources, tuple) or not self.sources:
            raise ContractError("operation plan must retain its operation source")
        if any(not isinstance(source, Source) for source in self.sources):
            raise ContractError("operation plan sources must be Source values")


@dataclass(frozen=True)
class ExecutionPlan:
    """Source-bound deterministic plan for exactly one owner operation."""

    project_identity: str
    owner: str
    operation: str
    variant: str | None
    steps: tuple[Step, ...]
    sources: tuple[Source, ...]
    resources: tuple[ExternalResource, ...] = field(repr=False, compare=False)
    _authorization: str = field(default="", repr=False, compare=False)

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
        if not isinstance(self.resources, tuple) or any(
            not isinstance(resource, ExternalResource) for resource in self.resources
        ):
            raise ContractError(
                "execution plan resources must be ExternalResource values"
            )
        if not isinstance(self._authorization, str):
            raise ContractError("execution plan authorization must be text")
        closure = {(source.root, source.path): source for source in self.sources}
        if len(closure) != len(self.sources):
            raise ContractError("execution plan contains duplicate source identities")
        if len({source.path for source in self.sources}) != len(self.sources):
            raise ContractError("execution plan source paths collide across scopes")
        for step in self.steps:
            for source in step.sources:
                if source not in {item.path for item in self.sources}:
                    raise ContractError(
                        f"step {step.id!r} source is outside the plan source closure"
                    )
        resources = {resource.identity: resource for resource in self.resources}
        if len(resources) != len(self.resources):
            raise ContractError("execution plan contains duplicate external resources")
        referenced = {resource for step in self.steps for resource in step.resources}
        if referenced != set(resources):
            raise ContractError(
                "execution plan external resource closure disagrees with its steps"
            )
        object.__setattr__(self, "steps", _topology(self.steps))

    @property
    def record(self) -> dict[str, Any]:
        return {
            "schema": 4,
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
        return canonical_digest(self.record)


@dataclass(frozen=True)
class Resources:
    """Explicit host facts supplied to backend preflight and execution."""

    capabilities: frozenset[str] = frozenset()
    environment: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.capabilities, frozenset) or any(
            not isinstance(item, str) or not item for item in self.capabilities
        ):
            raise ContractError("resource capabilities must be non-empty strings")
        if not isinstance(self.environment, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.environment.items()
        ):
            raise ContractError("resource environment must map strings to strings")
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))


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


@dataclass(frozen=True)
class Artifact:
    role: str
    kind: str
    path: Path
    qualifiers: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", _identifier(self.role, "artifact role"))
        object.__setattr__(self, "kind", backend_identity(self.kind))
        if not isinstance(self.qualifiers, Mapping):
            raise ContractError("artifact qualifiers must be a mapping")
        object.__setattr__(self, "path", Path(self.path).absolute())
        object.__setattr__(self, "qualifiers", _freeze(self.qualifiers, "artifact qualifiers"))


@dataclass(frozen=True)
class StepResult:
    status: str
    artifacts: tuple[Artifact, ...] = ()
    facts: Mapping[str, Any] = field(default_factory=dict)
    message: str = ""

    def __post_init__(self) -> None:
        if self.status not in _STEP_STATUSES:
            raise ContractError(f"invalid step result status: {self.status!r}")
        if not isinstance(self.artifacts, tuple) or any(
            not isinstance(artifact, Artifact) for artifact in self.artifacts
        ):
            raise ContractError("step result artifacts must be Artifact values")
        if self.status in {"blocked", "cancelled"} and self.artifacts:
            raise ContractError("blocked or cancelled steps cannot publish artifacts")
        if not isinstance(self.facts, Mapping):
            raise ContractError("step facts must be a mapping")
        if not isinstance(self.message, str):
            raise ContractError("step result message must be text")
        if self.status != "succeeded" and not self.message:
            raise ContractError("failed or blocked steps require a message")
        object.__setattr__(self, "facts", _freeze(self.facts, "step facts"))

    @classmethod
    def succeeded(
        cls,
        *,
        artifacts: tuple[Artifact, ...] = (),
        facts: Mapping[str, Any] | None = None,
    ) -> "StepResult":
        return cls("succeeded", artifacts, {} if facts is None else facts)

    @classmethod
    def failed(cls, message: str) -> "StepResult":
        return cls("failed", message=message)

    @classmethod
    def partial(cls, message: str, *, facts: Mapping[str, Any] | None = None) -> "StepResult":
        return cls("partial", facts={} if facts is None else facts, message=message)

    @classmethod
    def uncertain(
        cls,
        message: str,
        *,
        facts: Mapping[str, Any] | None = None,
    ) -> "StepResult":
        return cls("uncertain", facts={} if facts is None else facts, message=message)

    @classmethod
    def cancelled(cls, message: str) -> "StepResult":
        return cls("cancelled", message=message)


@dataclass(frozen=True)
class StepContext:
    """Managed filesystem and dependency view supplied to one Backend."""

    plan_identity: str
    step: Step
    run_id: str
    operation_id: str
    work_root: Path
    output_root: Path
    source_root: Path
    resources: Resources
    dependencies: Mapping[str, StepResult]
    project_root: Path | None = field(default=None, repr=False, compare=False)
    owner_root: Path | None = field(default=None, repr=False, compare=False)
    workspace_root: Path | None = field(default=None, repr=False, compare=False)
    source_scopes: Mapping[str, str] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )
    resource_root: Path | None = field(default=None, repr=False, compare=False)
    resource_digests: Mapping[str, str] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )
    _register_operation: Callable[[Any], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.step, Step):
            raise ContractError("step context requires a Step")
        validate_artifact_id(self.run_id, "run id")
        validate_artifact_id(self.operation_id, "operation id")
        validate_artifact_id(self.plan_identity, "plan identity")
        object.__setattr__(self, "work_root", Path(self.work_root).absolute())
        object.__setattr__(self, "output_root", Path(self.output_root).absolute())
        object.__setattr__(self, "source_root", Path(self.source_root).absolute())
        if self.resource_root is not None:
            object.__setattr__(
                self,
                "resource_root",
                Path(self.resource_root).absolute(),
            )
        runtime_roots = (self.project_root, self.owner_root, self.workspace_root)
        if any(root is None for root in runtime_roots) and any(
            root is not None for root in runtime_roots
        ):
            raise ContractError("step context runtime roots must be supplied together")
        if all(root is not None for root in runtime_roots):
            assert self.project_root is not None
            assert self.owner_root is not None
            assert self.workspace_root is not None
            project_root = Path(self.project_root).resolve()
            owner_root = Path(self.owner_root).resolve()
            workspace_root = Path(self.workspace_root).resolve()
            if not owner_root.is_relative_to(project_root):
                raise ContractError("step context owner root escaped its project")
            object.__setattr__(self, "project_root", project_root)
            object.__setattr__(self, "owner_root", owner_root)
            object.__setattr__(self, "workspace_root", workspace_root)
        if not isinstance(self.source_scopes, Mapping) or any(
            name not in self.step.sources or scope not in {"owner", "project"}
            for name, scope in self.source_scopes.items()
        ):
            raise ContractError("step context source scopes disagree with its step")
        if not isinstance(self.dependencies, Mapping) or any(
            not isinstance(name, str) or not isinstance(result, StepResult)
            for name, result in self.dependencies.items()
        ):
            raise ContractError("step dependencies must map names to StepResult values")
        if set(self.dependencies) != set(self.step.needs):
            raise ContractError("step context dependency closure disagrees with the plan")
        run_root = self.work_root.parent.parent
        if (
            self.work_root != run_root / "work" / self.step.id
            or self.output_root != run_root / "outputs" / self.step.id
            or self.source_root != run_root / "inputs" / "sources"
        ):
            raise ContractError("step context roots disagree with the managed run layout")
        expected_resource_root = run_root / "inputs" / "resources"
        if self.step.resources and self.resource_root != expected_resource_root:
            raise ContractError(
                "step context resource root disagrees with the managed run layout"
            )
        if (
            not isinstance(self.resource_digests, Mapping)
            or set(self.resource_digests) != set(self.step.resources)
            or any(
                not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                for digest in self.resource_digests.values()
            )
        ):
            raise ContractError(
                "step context resource digests disagree with its resource closure"
            )
        object.__setattr__(self, "dependencies", MappingProxyType(dict(self.dependencies)))
        object.__setattr__(
            self,
            "source_scopes",
            MappingProxyType(dict(self.source_scopes)),
        )
        object.__setattr__(
            self,
            "resource_digests",
            MappingProxyType(dict(self.resource_digests)),
        )

    def source_path(self, source: str) -> Path:
        """Return a run-local tool path for trusted package Backend code."""

        name = source
        relative = PurePosixPath(name)
        if (
            not name
            or relative.is_absolute()
            or "\\" in name
            or relative.as_posix() != name
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ExecutionError(f"source path must be canonical and relative: {name!r}")
        if name not in self.step.sources:
            raise ExecutionError(f"source is outside this step: {name!r}")
        result = self.source_root.joinpath(*relative.parts)
        if (
            result.absolute() != result
            or result.resolve() != result
            or not result.is_file()
            or result.is_symlink()
        ):
            raise ExecutionError(f"sealed source is missing or unsafe: {name!r}")
        return result

    def require_step(self, step: Step) -> None:
        """Reject a Backend call whose explicit request disagrees with this context."""

        if not isinstance(step, Step) or step != self.step:
            raise ExecutionError("backend Step disagrees with its StepContext")

    def source_text(self, source: str) -> str:
        """Read a step source through the held-fd no-follow input primitive."""

        return read_nofollow_text(self.source_path(source))

    def resource_path(self, resource: str) -> Path:
        """Return one sealed external resource selected by this Step."""

        name = resource_identity(resource)
        if name not in self.step.resources or self.resource_root is None:
            raise ExecutionError(f"external resource is outside this step: {name!r}")
        result = self.resource_root / resource_materialization_key(name)
        if (
            result.absolute() != result
            or result.resolve() != result
            or not result.is_file()
            or result.is_symlink()
        ):
            raise ExecutionError(f"sealed external resource is missing or unsafe: {name!r}")
        if hashlib.sha256(read_nofollow_text(result).encode("utf-8")).hexdigest() != (
            self.resource_digests[name]
        ):
            raise ExecutionError(
                f"sealed external resource identity drift: {name!r}"
            )
        return result

    def resource_text(self, resource: str) -> str:
        return read_nofollow_text(self.resource_path(resource))

    def scoped_source_path(self, scope: str, source: str) -> Path:
        """Resolve one owner- or project-relative source from the sealed closure."""

        matches = tuple(
            name
            for name in self.step.sources
            if self.source_scopes.get(name) == scope and name == source
        )
        if len(matches) != 1:
            raise ExecutionError(
                f"step source {scope}:{source} is missing or ambiguous"
            )
        return self.source_path(matches[0])

    def owner_source_path(self, source: str) -> Path:
        return self.scoped_source_path("owner", source)

    def project_source_path(self, source: str) -> Path:
        return self.scoped_source_path("project", source)

    def bind_workspace_operation(self, operation: Any) -> None:
        """Bind one trusted OA operation to this run before it accesses tools."""

        if self._register_operation is None:
            raise ExecutionError("step context cannot bind a workspace operation")
        if getattr(operation, "operation_id", None) != self.operation_id:
            raise ExecutionError("workspace operation identity disagrees with this run")
        self._register_operation(operation)

    def output_path(self, role: str, filename: str) -> Path:
        """Reserve a tool path; external tools must use held-fd output helpers."""

        role_name = _identifier(role, "output role")
        relative = PurePosixPath(filename)
        if (
            relative.is_absolute()
            or "\\" in filename
            or relative.as_posix() != filename
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ExecutionError(f"output filename must be canonical and relative: {filename!r}")
        role_root = ensure_nofollow_directory(self.output_root / role_name)
        result = role_root.joinpath(*relative.parts)
        ensure_nofollow_directory(result.parent)
        if not result.absolute().is_relative_to(self.output_root):
            raise ExecutionError("output path escaped its managed step root")
        return result

    def write_text(self, role: str, filename: str, value: str) -> Path:
        """Create one immutable text output without following path components."""

        result = self.output_path(role, filename)
        write_immutable_text(result, value)
        return result

    def files(
        self,
        output_role: str,
        source: Mapping[str, Any],
        *,
        tool_work_root: Path | None = None,
    ) -> "StepFiles":
        """Create the file view owned by this Step."""

        from sigilicon.execution.step_files import StepFiles

        return StepFiles.from_context(
            self,
            output_role,
            source,
            tool_work_root=tool_work_root,
        )

    def artifacts(self, dependency: str, role: str | None = None) -> tuple[Artifact, ...]:
        try:
            result = self.dependencies[dependency]
        except KeyError as exc:
            raise ExecutionError(f"step {self.step.id!r} has no dependency {dependency!r}") from exc
        return tuple(
            artifact
            for artifact in result.artifacts
            if role is None or artifact.role == role
        )


@dataclass(frozen=True)
class StepOutcome:
    step: str
    backend: str
    result: StepResult

    def __post_init__(self) -> None:
        object.__setattr__(self, "step", _identifier(self.step, "outcome step"))
        object.__setattr__(self, "backend", backend_identity(self.backend))
        if not isinstance(self.result, StepResult):
            raise ContractError("step outcome requires a StepResult")


@dataclass(frozen=True)
class RunResult:
    owner: str
    operation: str
    variant: str | None
    run_id: str
    operation_id: str
    plan_identity: str
    status: str
    outcomes: tuple[StepOutcome, ...]
    run_root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "owner", _identifier(self.owner, "run owner"))
        object.__setattr__(self, "operation", _identifier(self.operation, "run operation"))
        if self.variant is not None:
            object.__setattr__(self, "variant", _identifier(self.variant, "run variant"))
        if self.status not in _RUN_STATUSES:
            raise ContractError(f"invalid run status: {self.status!r}")
        validate_artifact_id(self.run_id, "run id")
        validate_artifact_id(self.operation_id, "operation id")
        validate_artifact_id(self.plan_identity, "plan identity")
        if not isinstance(self.outcomes, tuple) or not self.outcomes or any(
            not isinstance(outcome, StepOutcome) for outcome in self.outcomes
        ):
            raise ContractError("run outcomes must be a non-empty StepOutcome tuple")
        if len({outcome.step for outcome in self.outcomes}) != len(self.outcomes):
            raise ContractError("run outcomes contain duplicate steps")
        step_statuses = {outcome.result.status for outcome in self.outcomes}
        if step_statuses == {"succeeded"}:
            expected_status = "succeeded"
        elif "uncertain" in step_statuses:
            expected_status = "uncertain"
        elif "partial" in step_statuses:
            expected_status = "partial"
        elif "cancelled" in step_statuses:
            expected_status = "cancelled"
        else:
            expected_status = "failed"
        if self.status != expected_status:
            raise ContractError("run status disagrees with its step outcomes")
        root = Path(self.run_root).absolute()
        if root == Path(root.anchor):
            raise ContractError("run root cannot be a filesystem root")
        for outcome in self.outcomes:
            expected = root / "outputs" / outcome.step
            if any(not artifact.path.is_relative_to(expected) for artifact in outcome.result.artifacts):
                raise ContractError(
                    f"step {outcome.step!r} published outside its managed output root"
                )
        object.__setattr__(self, "run_root", root)

    @property
    def record(self) -> dict[str, Any]:
        return {
            "schema": 2,
            "contract_kind": "run-result",
            "owner": self.owner,
            "operation": self.operation,
            "variant": self.variant,
            "run_id": self.run_id,
            "operation_id": self.operation_id,
            "plan_identity": self.plan_identity,
            "status": self.status,
            "steps": [
                {
                    "id": outcome.step,
                    "uses": outcome.backend,
                    "status": outcome.result.status,
                    "message": outcome.result.message,
                    "facts": json_value(outcome.result.facts),
                    "artifacts": [
                        {
                            "role": artifact.role,
                            "kind": artifact.kind,
                            "path": artifact.path.relative_to(self.run_root).as_posix(),
                            "qualifiers": json_value(artifact.qualifiers),
                        }
                        for artifact in outcome.result.artifacts
                    ],
                }
                for outcome in self.outcomes
            ],
        }


@dataclass(frozen=True)
class RunFailure:
    """Typed terminal record for a run that failed before producing RunResult."""

    owner: str
    operation: str
    variant: str | None
    run_id: str
    operation_id: str | None
    plan_identity: str
    status: str
    error_type: str
    message: str
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "owner", _identifier(self.owner, "run owner"))
        object.__setattr__(self, "operation", _identifier(self.operation, "run operation"))
        if self.variant is not None:
            object.__setattr__(self, "variant", _identifier(self.variant, "run variant"))
        validate_artifact_id(self.run_id, "run id")
        validate_artifact_id(self.plan_identity, "plan identity")
        if self.operation_id is not None:
            validate_artifact_id(self.operation_id, "operation id")
        if self.status not in _RUN_FAILURE_STATUSES:
            raise ContractError(f"invalid run failure status: {self.status!r}")
        if not isinstance(self.error_type, str) or not self.error_type:
            raise ContractError("run failure error type must be non-empty text")
        if not isinstance(self.message, str):
            raise ContractError("run failure message must be text")
        if not isinstance(self.provenance, Mapping):
            raise ContractError("run failure provenance must be a mapping")
        object.__setattr__(
            self,
            "provenance",
            _freeze(self.provenance, "run failure provenance"),
        )

    @property
    def record(self) -> dict[str, Any]:
        return {
            "schema": 2,
            "contract_kind": "run-failure",
            "owner": self.owner,
            "operation": self.operation,
            "variant": self.variant,
            "run_id": self.run_id,
            "operation_id": self.operation_id,
            "plan_identity": self.plan_identity,
            "status": self.status,
            "error": {"type": self.error_type, "message": self.message},
            "provenance": json_value(self.provenance),
        }


__all__ = [
    "Artifact",
    "ContractError",
    "Evidence",
    "ExecutionError",
    "ExecutionPlan",
    "ExternalResource",
    "OperationPlan",
    "Operation",
    "Step",
    "PreflightCheck",
    "PreflightResult",
    "Resources",
    "RunResult",
    "RunFailure",
    "Source",
    "StepContext",
    "StepOutcome",
    "StepResult",
    "json_value",
    "backend_identity",
    "resource_identity",
    "resource_materialization_key",
]
