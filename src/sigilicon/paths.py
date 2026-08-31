"""Explicit project context and the only constructors for artifact paths."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
import tomllib
from types import MappingProxyType
from typing import Mapping


_NAME_RE = re.compile(r"[A-Za-z0-9_$][A-Za-z0-9_$.-]*\Z")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def _reject_unknown_fields(
    raw: Mapping[str, object],
    allowed: set[str],
    field: str,
) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"{field} contains unknown fields: {sorted(unknown)}")


def validate_artifact_component(value: str, label: str) -> str:
    """Validate one literal path component, including Windows separators."""

    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or not _NAME_RE.fullmatch(value)
        or Path(value).is_absolute()
    ):
        raise ValueError(f"invalid artifact {label}: {value!r}")
    return value


def validate_artifact_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ValueError(f"invalid artifact {label}: {value!r}")
    return value


def operation_incident_reference(operation_id: str) -> Path:
    """Return the canonical artifact-relative locator for one incident."""

    operation = validate_artifact_id(operation_id, "operation id")
    return Path("system") / "operations" / operation / "incident.json"


def _create_artifact_identity_directory(artifact_root: Path, root: Path) -> int:
    """Exclusively create one contained identity directory and return its dirfd."""

    artifact_root.mkdir(parents=True, exist_ok=True)
    relative = root.relative_to(artifact_root)
    if not relative.parts:
        raise RuntimeError("artifact identity path cannot equal artifact root")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(artifact_root, flags)
    try:
        for component in relative.parts[:-1]:
            try:
                os.mkdir(component, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        os.mkdir(relative.parts[-1], dir_fd=descriptor)
        return os.open(relative.parts[-1], flags, dir_fd=descriptor)
    except FileExistsError:
        raise
    except OSError as exc:
        raise RuntimeError(
            f"artifact path contains an unsafe filesystem component: {root}"
        ) from exc
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class ArtifactExecutionPaths:
    """One run/attempt directory with an explicit set of file roles."""

    artifact_root: Path
    namespace_root: Path
    root: Path
    artifact_kind: str
    identity_kind: str
    identity: str
    _roles: Mapping[str, Path]

    @classmethod
    def build(
        cls,
        *,
        artifact_root: Path,
        namespace_root: Path,
        root: Path,
        artifact_kind: str,
        identity_kind: str,
        identity: str,
        roles: tuple[str, ...],
    ) -> "ArtifactExecutionPaths":
        resolved_artifacts = artifact_root.resolve()
        namespace = Path(os.path.abspath(namespace_root))
        candidate = Path(os.path.abspath(root))
        if candidate == resolved_artifacts or not candidate.is_relative_to(resolved_artifacts):
            raise RuntimeError(f"artifact execution path escapes artifact root: {candidate}")
        if (
            namespace == resolved_artifacts
            or not namespace.is_relative_to(resolved_artifacts)
            or not candidate.is_relative_to(namespace)
        ):
            raise RuntimeError(f"artifact namespace escapes artifact root: {namespace}")
        if identity_kind not in {"run_id", "attempt_id"}:
            raise ValueError(f"invalid artifact identity kind: {identity_kind!r}")
        validate_artifact_id(identity, identity_kind)
        checked_roles = {
            validate_artifact_component(role, "role"): candidate / role for role in roles
        }
        return cls(
            artifact_root=resolved_artifacts,
            namespace_root=namespace,
            root=candidate,
            artifact_kind=artifact_kind,
            identity_kind=identity_kind,
            identity=identity,
            _roles=MappingProxyType(checked_roles),
        )

    @property
    def manifest(self) -> Path:
        return self.root / "manifest.json"

    @property
    def roles(self) -> tuple[str, ...]:
        return tuple(self._roles)

    def role(self, role: str) -> Path:
        try:
            return self._roles[role]
        except KeyError as exc:
            raise ValueError(
                f"role {role!r} is not valid for {self.artifact_kind}"
            ) from exc

    def path(self, role: str, *components: str) -> Path:
        result = self.role(role)
        for component in components:
            result /= validate_artifact_component(component, f"{role} path component")
        return result

    def create(self) -> None:
        """Exclusively create this identity and all declared role directories."""

        root_descriptor = _create_artifact_identity_directory(
            self.artifact_root,
            self.root,
        )
        try:
            for role in self._roles:
                os.mkdir(role, dir_fd=root_descriptor)
        finally:
            os.close(root_descriptor)

    def complete_partial_create(self) -> None:
        """Safely finish an interrupted identity/role directory creation."""

        if not self.root.exists():
            try:
                self.create()
            except FileExistsError:
                pass
        if (
            not self.root.is_dir()
            or self.root.is_symlink()
            or not self.root.resolve().is_relative_to(self.artifact_root)
        ):
            raise ValueError(f"{self.artifact_kind} partial create path is unsafe")
        known_roles = set(self.roles)
        entries = {item.name: item for item in os.scandir(self.root)}
        if set(entries) - known_roles:
            raise ValueError(f"{self.artifact_kind} partial create inventory conflicts")
        for role in self.roles:
            target = self.role(role)
            if not target.exists():
                target.mkdir()
            if not target.is_dir() or target.is_symlink():
                raise ValueError(f"{self.artifact_kind} partial create role conflicts")


@dataclass(frozen=True)
class OperationIncidentPaths:
    artifact_root: Path
    operation_id: str

    @property
    def root(self) -> Path:
        return self.artifact_root / operation_incident_reference(self.operation_id).parent

    @property
    def incident(self) -> Path:
        return self.artifact_root / operation_incident_reference(self.operation_id)

    def create(self) -> None:
        descriptor = _create_artifact_identity_directory(
            self.artifact_root,
            self.root,
        )
        os.close(descriptor)


@dataclass(frozen=True)
class ArtifactLayout:
    """The single physical-layout interface for managed project artifacts."""

    root: Path

    def execution(
        self,
        *,
        owner: str,
        target: str,
        flow: str,
        variant: str,
        identity: str,
        artifact_kind: str,
        identity_kind: str,
    ) -> ArtifactExecutionPaths:
        """Resolve one run without exposing directory policy to its caller."""

        owner_name = validate_artifact_component(owner, "owner")
        target_name = validate_artifact_component(target, "target")
        flow_name = validate_artifact_component(flow, "flow")
        variant_name = validate_artifact_component(variant, "variant")
        artifact_identity = validate_artifact_id(identity, identity_kind.replace("_", " "))
        namespace = (
            self.root
            / "runs"
            / owner_name
            / target_name
            / flow_name
            / variant_name
        )
        return ArtifactExecutionPaths.build(
            artifact_root=self.root,
            namespace_root=namespace,
            root=namespace / artifact_identity,
            artifact_kind=artifact_kind,
            identity_kind=identity_kind,
            identity=artifact_identity,
            roles=("inputs", "work", "outputs", "logs"),
        )

    def export(self, owner: str, name: str, *components: str) -> Path:
        """Resolve a named stable handoff below the managed export tree."""

        result = (
            self.root
            / "exports"
            / validate_artifact_component(owner, "export owner")
            / validate_artifact_component(name, "export name")
        )
        for component in components:
            result /= validate_artifact_component(component, "export component")
        return result

    def agentic_execution(
        self,
        *,
        owner: str,
        target: str,
        flow: str,
        identity: str,
    ) -> ArtifactExecutionPaths:
        """Resolve private control/audit storage for one managed Flow Run."""

        owner_name = validate_artifact_component(owner, "owner")
        target_name = validate_artifact_component(target, "target")
        flow_name = validate_artifact_component(flow, "flow")
        run_id = validate_artifact_id(identity, "run id")
        namespace = (
            self.root
            / "system"
            / "agentic-target-runs"
            / owner_name
            / target_name
            / flow_name
        )
        return ArtifactExecutionPaths.build(
            artifact_root=self.root,
            namespace_root=namespace,
            root=namespace / run_id,
            artifact_kind="agentic-target-run",
            identity_kind="run_id",
            identity=run_id,
            roles=("control", "audit"),
        )

    def agentic_campaign(
        self,
        *,
        owner: str,
        campaign: str,
        identity: str,
    ) -> ArtifactExecutionPaths:
        """Resolve private durable state for one feedback-driven Campaign."""

        owner_name = validate_artifact_component(owner, "owner")
        campaign_name = validate_artifact_component(campaign, "campaign")
        run_id = validate_artifact_id(identity, "run id")
        namespace = (
            self.root
            / "system"
            / "agentic-design-campaigns"
            / owner_name
            / campaign_name
        )
        return ArtifactExecutionPaths.build(
            artifact_root=self.root,
            namespace_root=namespace,
            root=namespace / run_id,
            artifact_kind="agentic-design-campaign",
            identity_kind="run_id",
            identity=run_id,
            roles=("control", "audit", "inputs", "outputs"),
        )

    def system_operation(self, operation_id: str) -> OperationIncidentPaths:
        operation = validate_artifact_id(operation_id, "operation id")
        return OperationIncidentPaths(
            artifact_root=self.root.resolve(),
            operation_id=operation,
        )


@dataclass(frozen=True)
class ProjectContext:
    """Runtime and artifact locations needed by reusable flow code.

    Callers construct this interface explicitly.  The convenience discovery
    function below is reserved for CLI assembly and reads project-owned path
    facts from ``sigilicon.toml``; workflows never infer a Pixi workspace or a
    neighboring repository layout.
    """

    project_root: Path
    artifact_root: Path
    workspace_root: Path

    @classmethod
    def from_roots(
        cls,
        project_root: Path | str,
        *,
        artifact_root: Path | str,
        workspace_root: Path | str,
    ) -> "ProjectContext":
        root = Path(project_root).resolve()
        def resolve(value: Path | str) -> Path:
            candidate = Path(value).expanduser()
            return (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()

        artifacts = resolve(artifact_root)
        workspace = resolve(workspace_root)
        if artifacts == root:
            raise ValueError("artifact root must not be the project root")
        return cls(
            project_root=root,
            artifact_root=artifacts,
            workspace_root=workspace,
        )

    @classmethod
    def from_file(cls, path: Path | str) -> "ProjectContext":
        """Load an explicit project-layout contract owned by the caller."""

        contract = Path(path).resolve()
        try:
            with contract.open("rb") as stream:
                raw = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ValueError(f"cannot read Sigilicon project context {contract}: {exc}") from exc
        return cls.from_contract(contract, raw)

    @classmethod
    def from_contract(
        cls,
        path: Path | str,
        raw: Mapping[str, object],
    ) -> "ProjectContext":
        """Construct paths from an already-read explicit project contract."""

        contract = Path(path).resolve()
        if raw.get("schema") != 1 or raw.get("contract_kind") != "sigilicon-project":
            raise ValueError(f"{contract}: invalid Sigilicon project context header")
        manifest_owner = raw.get("owner")
        if raw.get("path_scope") != "repository" or not isinstance(
            manifest_owner, str
        ):
            raise ValueError(f"{contract}: invalid Sigilicon project context ownership")
        try:
            validate_artifact_component(manifest_owner, "project owner")
        except ValueError as exc:
            raise ValueError(
                f"{contract}: invalid Sigilicon project context ownership: {exc}"
            ) from exc
        _reject_unknown_fields(
            raw,
            {
                "schema",
                "contract_kind",
                "path_scope",
                "owner",
                "catalogs",
                "flow",
                "paths",
            },
            str(contract),
        )
        paths = raw.get("paths")
        if not isinstance(paths, Mapping):
            raise ValueError(f"{contract}: paths must be a table")
        allowed_paths = {
            "project_root",
            "workspace_root",
            "artifact_root",
        }
        _reject_unknown_fields(paths, allowed_paths, f"{contract}: paths")

        def required(name: str) -> str:
            value = paths.get(name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{contract}: paths.{name} must be a non-empty path")
            return value

        declared_root = Path(required("project_root")).expanduser()
        root = (
            (contract.parent / declared_root).resolve()
            if not declared_root.is_absolute()
            else declared_root.resolve()
        )
        return cls.from_roots(
            root,
            artifact_root=required("artifact_root"),
            workspace_root=required("workspace_root"),
        )

    @classmethod
    def from_project_root(
        cls,
        project_root: Path | str,
        *,
        artifact_root: Path | str | None = None,
    ) -> "ProjectContext":
        """Load the project-owned context contract at an explicit root."""

        root = Path(project_root).resolve()
        context = cls.from_file(root / "sigilicon.toml")
        if context.project_root != root:
            raise ValueError(
                f"{root / 'sigilicon.toml'} declares a different project root: "
                f"{context.project_root}"
            )
        if artifact_root is None:
            return context
        return context.with_artifact_root(artifact_root)

    def with_artifact_root(
        self,
        artifact_root: Path | str,
    ) -> "ProjectContext":
        """Return the same project layout with run-scoped output roots."""

        return self.from_roots(
            self.project_root,
            artifact_root=artifact_root,
            workspace_root=self.workspace_root,
        )

    @property
    def artifacts(self) -> ArtifactLayout:
        return ArtifactLayout(self.artifact_root)


@dataclass(frozen=True, init=False)
class ProjectScope:
    """Neutral runtime paths bound by the repository ownership module."""

    project: ProjectContext
    owner: str
    owner_root: Path

    @classmethod
    def _from_cataloged_owner(
        cls,
        project: ProjectContext,
        owner: str,
        owner_root: Path,
    ) -> "ProjectScope":
        """Bind one owner already selected from the canonical project catalog."""

        identity = validate_artifact_component(owner, "project owner")
        root = Path(owner_root).resolve()
        if not root.is_relative_to(project.project_root):
            raise ValueError("project owner root must stay below the project root")
        scope = object.__new__(cls)
        object.__setattr__(scope, "project", project)
        object.__setattr__(scope, "owner", identity)
        object.__setattr__(scope, "owner_root", root)
        return scope


def discover_project_contract(anchor: Path | str | None = None) -> Path:
    """Locate ``sigilicon.toml`` for a public CLI without parsing it."""

    starts = [Path.cwd()]
    if anchor is not None:
        candidate = Path(anchor).resolve()
        starts.append(candidate if candidate.is_dir() else candidate.parent)
    visited: set[Path] = set()
    for start in starts:
        for candidate in (start, *start.parents):
            if candidate in visited:
                continue
            visited.add(candidate)
            contract = candidate / "sigilicon.toml"
            if contract.is_file():
                return contract.resolve()
    raise RuntimeError(
        "cannot locate sigilicon.toml; run inside a configured project or pass a ProjectContext"
    )


def discover_project_context(anchor: Path | str | None = None) -> ProjectContext:
    """Discover and load project runtime paths for a public CLI invocation."""

    return ProjectContext.from_file(discover_project_contract(anchor))
