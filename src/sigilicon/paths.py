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
_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}\Z")


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


def validate_fingerprint(value: str, label: str) -> str:
    if not isinstance(value, str) or not _FINGERPRINT_RE.fullmatch(value):
        raise ValueError(f"invalid artifact {label}: {value!r}")
    return value


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


@dataclass(frozen=True)
class OperationIncidentPaths:
    artifact_root: Path
    root: Path
    operation_id: str

    @property
    def incident(self) -> Path:
        return self.root / "incident.json"

    def create(self) -> None:
        descriptor = _create_artifact_identity_directory(
            self.artifact_root,
            self.root,
        )
        os.close(descriptor)


@dataclass(frozen=True)
class AdeArtifactPaths:
    artifact_root: Path
    root: Path
    library: str
    testbench: str

    @property
    def current(self) -> Path:
        return self.root / "current.json"

    def setup_attempt(
        self,
        setup_fingerprint: str,
        attempt_id: str,
    ) -> ArtifactExecutionPaths:
        fingerprint = validate_fingerprint(setup_fingerprint, "setup fingerprint")
        attempt = validate_artifact_id(attempt_id, "attempt id")
        return ArtifactExecutionPaths.build(
            artifact_root=self.artifact_root,
            namespace_root=self.root,
            root=self.root / "setups" / fingerprint / "attempts" / attempt,
            artifact_kind="ade_setup",
            identity_kind="attempt_id",
            identity=attempt,
            roles=("inputs", "evidence", "logs", "work"),
        )

    def run(self, run_id: str) -> ArtifactExecutionPaths:
        run = validate_artifact_id(run_id, "run id")
        return ArtifactExecutionPaths.build(
            artifact_root=self.artifact_root,
            namespace_root=self.root,
            root=self.root / "runs" / run,
            artifact_kind="ade_run",
            identity_kind="run_id",
            identity=run,
            roles=("inputs", "results", "logs", "work"),
        )


@dataclass(frozen=True)
class ArtifactPaths:
    root: Path

    def _name(self, value: str, label: str) -> str:
        return validate_artifact_component(value, label)

    def design_sync_attempt(
        self,
        library: str,
        cell: str,
        attempt_id: str,
    ) -> ArtifactExecutionPaths:
        lib = self._name(library, "library")
        design_cell = self._name(cell, "cell")
        attempt = validate_artifact_id(attempt_id, "attempt id")
        return ArtifactExecutionPaths.build(
            artifact_root=self.root,
            namespace_root=self.root / "designs" / lib / design_cell / "sync",
            root=self.root / "designs" / lib / design_cell / "sync" / "attempts" / attempt,
            artifact_kind="design_sync",
            identity_kind="attempt_id",
            identity=attempt,
            roles=("inputs", "evidence", "logs", "work"),
        )

    def oa_text_view_attempt(
        self,
        library: str,
        cell: str,
        view: str,
        attempt_id: str,
    ) -> ArtifactExecutionPaths:
        lib = self._name(library, "library")
        design_cell = self._name(cell, "cell")
        design_view = self._name(view, "view")
        attempt = validate_artifact_id(attempt_id, "attempt id")
        namespace = (
            self.root
            / "designs"
            / lib
            / design_cell
            / "views"
            / design_view
            / "sync"
        )
        return ArtifactExecutionPaths.build(
            artifact_root=self.root,
            namespace_root=namespace,
            root=namespace / "attempts" / attempt,
            artifact_kind="oa_text_view",
            identity_kind="attempt_id",
            identity=attempt,
            roles=("inputs", "evidence", "logs", "work"),
        )

    def layout_generation_attempt(
        self,
        library: str,
        cell: str,
        view: str,
        attempt_id: str,
    ) -> ArtifactExecutionPaths:
        lib = self._name(library, "library")
        design_cell = self._name(cell, "cell")
        design_view = self._name(view, "view")
        attempt = validate_artifact_id(attempt_id, "attempt id")
        namespace = (
            self.root
            / "designs"
            / lib
            / design_cell
            / "layout"
            / design_view
            / "generate"
        )
        return ArtifactExecutionPaths.build(
            artifact_root=self.root,
            namespace_root=namespace,
            root=namespace / "attempts" / attempt,
            artifact_kind="layout_generation",
            identity_kind="attempt_id",
            identity=attempt,
            roles=("inputs", "evidence", "logs", "work"),
        )

    def layout_verification_run(
        self,
        library: str,
        cell: str,
        view: str,
        check: str,
        run_id: str,
    ) -> ArtifactExecutionPaths:
        lib = self._name(library, "library")
        design_cell = self._name(cell, "cell")
        design_view = self._name(view, "view")
        check_name = self._name(check, "check")
        run = validate_artifact_id(run_id, "run id")
        namespace = (
            self.root
            / "designs"
            / lib
            / design_cell
            / "layout"
            / design_view
            / "verification"
            / check_name
        )
        return ArtifactExecutionPaths.build(
            artifact_root=self.root,
            namespace_root=namespace,
            root=namespace / "runs" / run,
            artifact_kind="physical_verification",
            identity_kind="run_id",
            identity=run,
            roles=("inputs", "results", "logs", "work"),
        )

    def netlist_export_run(
        self,
        library: str,
        cell: str,
        view: str,
        simulator: str,
        run_id: str,
    ) -> ArtifactExecutionPaths:
        lib = self._name(library, "library")
        design_cell = self._name(cell, "cell")
        design_view = self._name(view, "view")
        backend = self._name(simulator, "simulator")
        run = validate_artifact_id(run_id, "run id")
        return ArtifactExecutionPaths.build(
            artifact_root=self.root,
            namespace_root=(
                self.root
                / "designs"
                / lib
                / design_cell
                / "exports"
                / "netlist"
                / design_view
                / backend
            ),
            root=(
                self.root
                / "designs"
                / lib
                / design_cell
                / "exports"
                / "netlist"
                / design_view
                / backend
                / "runs"
                / run
            ),
            artifact_kind="netlist_export",
            identity_kind="run_id",
            identity=run,
            roles=("inputs", "results", "logs", "work"),
        )

    def standalone_run(
        self,
        library: str,
        testbench: str,
        run_id: str,
    ) -> ArtifactExecutionPaths:
        lib = self._name(library, "library")
        tb = self._name(testbench, "testbench")
        run = validate_artifact_id(run_id, "run id")
        return ArtifactExecutionPaths.build(
            artifact_root=self.root,
            namespace_root=self.root / "verification" / lib / tb / "standalone",
            root=self.root / "verification" / lib / tb / "standalone" / "runs" / run,
            artifact_kind="standalone_simulation",
            identity_kind="run_id",
            identity=run,
            roles=("inputs", "results", "logs", "work"),
        )

    def ade(self, library: str, testbench: str) -> AdeArtifactPaths:
        lib = self._name(library, "library")
        tb = self._name(testbench, "testbench")
        return AdeArtifactPaths(
            artifact_root=self.root,
            root=self.root / "verification" / lib / tb / "ade",
            library=lib,
            testbench=tb,
        )

    def import_attempt(
        self,
        library: str,
        source: str,
        attempt_id: str,
    ) -> ArtifactExecutionPaths:
        lib = self._name(library, "library")
        source_name = self._name(source, "source")
        attempt = validate_artifact_id(attempt_id, "attempt id")
        return ArtifactExecutionPaths.build(
            artifact_root=self.root,
            namespace_root=self.root / "imports" / lib / source_name,
            root=self.root / "imports" / lib / source_name / "attempts" / attempt,
            artifact_kind="netlist_import",
            identity_kind="attempt_id",
            identity=attempt,
            roles=("source", "cells", "evidence", "logs", "work"),
        )

    def analysis_run(
        self,
        library: str,
        cell: str,
        analysis: str,
        model: str,
        run_id: str,
    ) -> ArtifactExecutionPaths:
        lib = self._name(library, "library")
        design_cell = self._name(cell, "cell")
        analysis_name = self._name(analysis, "analysis")
        model_name = self._name(model, "model")
        run = validate_artifact_id(run_id, "run id")
        namespace = (
            self.root
            / "designs"
            / lib
            / design_cell
            / analysis_name
            / model_name
        )
        return ArtifactExecutionPaths.build(
            artifact_root=self.root,
            namespace_root=namespace,
            root=namespace / "runs" / run,
            artifact_kind="analysis",
            identity_kind="run_id",
            identity=run,
            roles=("inputs", "results", "logs", "work"),
        )

    def operation_incident(self, operation_id: str) -> OperationIncidentPaths:
        operation = validate_artifact_id(operation_id, "operation id")
        return OperationIncidentPaths(
            artifact_root=self.root.resolve(),
            root=self.root / "system" / "operations" / operation,
            operation_id=operation,
        )


@dataclass(frozen=True)
class ProjectContext:
    """All project-owned locations needed by reusable flow code.

    Callers construct this interface explicitly.  The convenience discovery
    function below is reserved for CLI assembly and reads project-owned path
    facts from ``sigilicon.toml``; workflows never infer a Pixi workspace or a
    neighboring repository layout.
    """

    project_root: Path
    artifact_root: Path
    result_root: Path
    workspace_root: Path
    ip_root: Path
    managed_ip_roots: tuple[Path, ...]
    ip_config_dir: str
    config_root: Path
    artifact_namespace: str
    catalog_paths: tuple[tuple[str, Path], ...]
    owned_module_prefixes: tuple[str, ...]
    native_diagnostic_adapters: tuple[tuple[str, Path], ...]

    @classmethod
    def from_roots(
        cls,
        project_root: Path | str,
        *,
        artifact_root: Path | str,
        result_root: Path | str,
        workspace_root: Path | str,
        ip_root: Path | str,
        managed_ip_roots: tuple[Path | str, ...],
        ip_config_dir: str,
        config_root: Path | str,
        artifact_namespace: str = "sigilicon",
        catalog_paths: Mapping[str, Path | str] | None = None,
        owned_module_prefixes: tuple[str, ...] = (),
        native_diagnostic_adapters: Mapping[str, Path | str] | None = None,
    ) -> "ProjectContext":
        root = Path(project_root).resolve()
        def resolve(value: Path | str) -> Path:
            candidate = Path(value).expanduser()
            return (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()

        artifacts = resolve(artifact_root)
        results = resolve(result_root)
        workspace = resolve(workspace_root)
        ips = resolve(ip_root)
        managed_ips = tuple(resolve(value) for value in managed_ip_roots)
        if not managed_ips:
            raise ValueError("managed IP roots must not be empty")
        if len(set(managed_ips)) != len(managed_ips):
            raise ValueError("managed IP roots must be unique")
        if any(path == ips or not path.is_relative_to(ips) for path in managed_ips):
            raise ValueError(
                "managed IP roots must identify owners below the IP root"
            )
        configs = resolve(config_root)
        if artifacts == root:
            raise ValueError("artifact root must not be the project root")
        if results == root:
            raise ValueError("result root must not be the project root")
        namespace = validate_artifact_component(
            artifact_namespace, "namespace"
        )
        resolved_catalogs = tuple(
            sorted(
                (
                    validate_artifact_component(name, "catalog name"),
                    resolve(value),
                )
                for name, value in (catalog_paths or {}).items()
            )
        )
        prefixes: list[str] = []
        for prefix in owned_module_prefixes:
            if (
                not isinstance(prefix, str)
                or not prefix.endswith(".")
                or any(
                    not part.isidentifier()
                    for part in prefix.removesuffix(".").split(".")
                )
            ):
                raise ValueError(
                    "owned module prefixes must be dotted Python package prefixes"
                )
            prefixes.append(prefix)
        if len(set(prefixes)) != len(prefixes):
            raise ValueError("owned module prefixes must be unique")
        diagnostic_adapters: list[tuple[str, Path]] = []
        for owner, value in (native_diagnostic_adapters or {}).items():
            owner_name = validate_artifact_component(owner, "native diagnostic owner")
            source = resolve(value)
            if not source.is_file():
                raise ValueError(
                    "native diagnostic adapter must be an existing project-owned file"
                )
            if not source.is_relative_to(root):
                raise ValueError("native diagnostic adapter must stay below the project root")
            diagnostic_adapters.append((owner_name, source))
        if len({owner for owner, _ in diagnostic_adapters}) != len(diagnostic_adapters):
            raise ValueError("native diagnostic adapter owners must be unique")
        return cls(
            project_root=root,
            artifact_root=artifacts,
            result_root=results,
            workspace_root=workspace,
            ip_root=ips,
            managed_ip_roots=managed_ips,
            ip_config_dir=validate_artifact_component(
                ip_config_dir, "IP config directory"
            ),
            config_root=configs,
            artifact_namespace=namespace,
            catalog_paths=resolved_catalogs,
            owned_module_prefixes=tuple(prefixes),
            native_diagnostic_adapters=tuple(sorted(diagnostic_adapters)),
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
        if raw.get("schema") != 1 or raw.get("contract_kind") != "sigilicon-project":
            raise ValueError(f"{contract}: invalid Sigilicon project context header")
        paths = raw.get("paths")
        if not isinstance(paths, dict):
            raise ValueError(f"{contract}: paths must be a table")

        def required(name: str) -> str:
            value = paths.get(name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{contract}: paths.{name} must be a non-empty path")
            return value

        managed_ip_roots = paths.get("managed_ip_roots")
        if not isinstance(managed_ip_roots, list) or any(
            not isinstance(value, str) or not value for value in managed_ip_roots
        ):
            raise ValueError(
                f"{contract}: paths.managed_ip_roots must be a non-empty path array"
            )

        project = raw.get("project", {})
        if not isinstance(project, dict):
            raise ValueError(f"{contract}: project must be a table")
        artifact_namespace = project.get("artifact_namespace", "sigilicon")
        if not isinstance(artifact_namespace, str):
            raise ValueError(
                f"{contract}: project.artifact_namespace must be text"
            )
        catalogs = raw.get("catalogs", {})
        if not isinstance(catalogs, dict) or any(
            not isinstance(name, str)
            or not isinstance(value, str)
            or not value
            for name, value in catalogs.items()
        ):
            raise ValueError(f"{contract}: catalogs must map names to paths")
        python = raw.get("python", {})
        if not isinstance(python, dict):
            raise ValueError(f"{contract}: python must be a table")
        raw_prefixes = python.get("owned_module_prefixes", [])
        if not isinstance(raw_prefixes, list) or any(
            not isinstance(prefix, str) for prefix in raw_prefixes
        ):
            raise ValueError(
                f"{contract}: python.owned_module_prefixes must be a string array"
            )
        native_diagnostic_adapters = python.get("native_diagnostic_adapters", {})
        if not isinstance(native_diagnostic_adapters, dict) or any(
            not isinstance(owner, str)
            or not isinstance(value, str)
            or not value
            for owner, value in native_diagnostic_adapters.items()
        ):
            raise ValueError(
                f"{contract}: python.native_diagnostic_adapters must map owners to paths"
            )

        declared_root = Path(required("project_root")).expanduser()
        root = (
            (contract.parent / declared_root).resolve()
            if not declared_root.is_absolute()
            else declared_root.resolve()
        )
        return cls.from_roots(
            root,
            artifact_root=required("artifact_root"),
            result_root=required("result_root"),
            workspace_root=required("workspace_root"),
            ip_root=required("ip_root"),
            managed_ip_roots=tuple(managed_ip_roots),
            ip_config_dir=required("ip_config_dir"),
            config_root=required("config_root"),
            artifact_namespace=artifact_namespace,
            catalog_paths=catalogs,
            owned_module_prefixes=tuple(raw_prefixes),
            native_diagnostic_adapters=native_diagnostic_adapters,
        )

    @classmethod
    def from_project_root(
        cls,
        project_root: Path | str,
        *,
        artifact_root: Path | str | None = None,
        result_root: Path | str | None = None,
    ) -> "ProjectContext":
        """Load the project-owned context contract at an explicit root."""

        root = Path(project_root).resolve()
        context = cls.from_file(root / "sigilicon.toml")
        if context.project_root != root:
            raise ValueError(
                f"{root / 'sigilicon.toml'} declares a different project root: "
                f"{context.project_root}"
            )
        if artifact_root is None and result_root is None:
            return context
        return context.with_artifact_root(
            artifact_root or context.artifact_root,
            result_root=result_root,
        )

    def with_artifact_root(
        self,
        artifact_root: Path | str,
        *,
        result_root: Path | str | None = None,
    ) -> "ProjectContext":
        """Return the same project layout with run-scoped output roots."""

        return self.from_roots(
            self.project_root,
            artifact_root=artifact_root,
            result_root=result_root or self.result_root,
            workspace_root=self.workspace_root,
            ip_root=self.ip_root,
            managed_ip_roots=self.managed_ip_roots,
            ip_config_dir=self.ip_config_dir,
            config_root=self.config_root,
            artifact_namespace=self.artifact_namespace,
            catalog_paths=dict(self.catalog_paths),
            owned_module_prefixes=self.owned_module_prefixes,
            native_diagnostic_adapters=dict(self.native_diagnostic_adapters),
        )

    def ip(self, name: str) -> Path:
        return self.ip_root / validate_artifact_component(name, "IP name")

    def ip_config(self, name: str, filename: str) -> Path:
        return self.ip_config_root(name) / validate_artifact_component(
            filename, "IP config filename"
        )

    def ip_config_root(self, name: str) -> Path:
        return self.ip(name) / self.ip_config_dir

    def catalog(self, name: str) -> Path:
        """Return one caller-owned catalog path declared by the project."""

        key = validate_artifact_component(name, "catalog name")
        try:
            return dict(self.catalog_paths)[key]
        except KeyError as exc:
            raise ValueError(f"project context has no {key!r} catalog") from exc

    def is_managed_ip_path(self, path: Path | str) -> bool:
        """Return the caller-owned eligibility decision for an IP path."""

        resolved = Path(path).resolve()
        return any(resolved.is_relative_to(root) for root in self.managed_ip_roots)

    def native_diagnostic_adapter_for(self, owner: str) -> Path | None:
        """Return the adapter selected by the caller for one IP directory."""

        key = validate_artifact_component(owner, "native diagnostic owner")
        return dict(self.native_diagnostic_adapters).get(key)

    @property
    def artifacts(self) -> ArtifactPaths:
        return ArtifactPaths(self.artifact_root)


def discover_project_context(anchor: Path | str | None = None) -> ProjectContext:
    """Discover ``sigilicon.toml`` for a public CLI invocation."""

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
                return ProjectContext.from_file(contract)
    raise RuntimeError(
        "cannot locate sigilicon.toml; run inside a configured project or pass a ProjectContext"
    )
