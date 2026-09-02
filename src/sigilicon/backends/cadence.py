"""Trusted Cadence adapters for direct RTL, native OA, and layout execution."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
import re
from types import MappingProxyType
from typing import Any, Mapping

from sigilicon.artifacts import read_nofollow_text
from sigilicon.canonical import canonical_digest
from sigilicon.execution.adapter import DirectAdapter, PlanningProject
from sigilicon.execution.model import (
    Artifact,
    ContractError,
    ExecutionError,
    ResourceBinding,
    PreflightCheck,
    Resources,
    Source,
    Step,
    StepContext,
    StepResult,
    json_value,
)
from sigilicon.external_tools import (
    CADENCE_SPICEIN_TOOL,
    CADENCE_TEXT_IMPORT_TOOL,
    CADENCE_VIRTUOSO_TOOL,
    ProcessRequest,
    managed_process,
    owned_directory,
    owned_executable,
    owned_scratch_directory,
    process_group_cleanup_uncertainty,
    xrun_env,
)


_XRUN = "cadence.xrun"
_XSTREAM = "cadence.xstream"
_CALIBRE = "mentor.calibre"
_OA_CAPABILITIES = frozenset({"tool.virtuoso-bridge", "license.cadence-oa"})
_OA_TEXT_VIEW_KINDS = frozenset({"spectre_model", "veriloga", "system_verilog"})
_PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def _strict_config(step: Step, fields: frozenset[str]) -> Mapping[str, Any]:
    request = step.request
    if set(request) == {"config", "prepared"}:
        nested = request["config"]
        if not isinstance(nested, Mapping):
            raise ContractError("prepared Cadence config must be a mapping")
        config = nested
    else:
        config = request
    unknown = set(config) - fields
    missing = fields - set(config)
    if unknown or missing:
        raise ContractError(
            f"{step.uses} config fields disagree with its contract; "
            f"missing={sorted(missing)}, unknown={sorted(unknown)}"
        )
    return config


def _text(config: Mapping[str, Any], name: str) -> str:
    value = config.get(name)
    if not isinstance(value, str) or not value:
        raise ContractError(f"Cadence step requires non-empty {name!r}")
    return value


def _positive_integer(config: Mapping[str, Any], name: str) -> int:
    value = config.get(name)
    if type(value) is not int or value <= 0:
        raise ContractError(f"Cadence step requires positive integer {name!r}")
    return value


def _strings(config: Mapping[str, Any], name: str) -> tuple[str, ...]:
    value = config.get(name)
    if not isinstance(value, tuple) or not value or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ContractError(f"Cadence step requires non-empty text array {name!r}")
    if len(value) != len(set(value)):
        raise ContractError(f"Cadence step {name!r} contains duplicates")
    return value


def _relative(value: str, label: str) -> str:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or "\\" in value
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ContractError(f"{label} must be a canonical relative path")
    return value


def _capability_checks(
    resources: Resources,
    required: frozenset[str],
) -> tuple[PreflightCheck, ...]:
    return tuple(
        PreflightCheck(
            "runtime-capability",
            capability,
            "ready" if capability in resources.capabilities else "blocked",
            "declared by the project runtime"
            if capability in resources.capabilities
            else "missing capability",
        )
        for capability in sorted(required)
    )


def _configured_executable(resources: Resources, name: str) -> Path | None:
    return resources.configured_tool(name)


def _executable_check(resources: Resources, name: str) -> PreflightCheck:
    path = _configured_executable(resources, name)
    ready = path is not None
    return PreflightCheck(
        "runtime-resource",
        name,
        "ready" if ready else "blocked",
        "executable declared by the project runtime" if ready else "missing executable",
    )


def _bridge_check(resources: Resources) -> PreflightCheck:
    from sigilicon.virtuoso.bridge import bridge_endpoint

    try:
        host, port = bridge_endpoint(resources)
    except (TypeError, ValueError, ContractError) as exc:
        return PreflightCheck(
            "runtime-resource",
            "virtuoso-bridge-endpoint",
            "blocked",
            str(exc),
        )
    return PreflightCheck(
        "runtime-resource",
        "virtuoso-bridge-endpoint",
        "ready",
        f"explicit endpoint {host}:{port}",
    )


def _oa_runtime_executables(planning: Any, operation: str) -> tuple[str, ...]:
    if operation != "rebuild":
        return ()
    required: list[str] = []
    if getattr(planning, "designs", ()) or getattr(planning, "testbenches", ()):
        required.append(CADENCE_SPICEIN_TOOL)
    if any(
        item.view.kind in _OA_TEXT_VIEW_KINDS
        for item in getattr(planning, "views", ())
    ):
        required.append(CADENCE_TEXT_IMPORT_TOOL)
    return tuple(required)


def _publish_tree(context: StepContext, role: str, kind: str) -> tuple[Artifact, ...]:
    root = context.output_root / role
    if not root.is_dir() or root.is_symlink():
        return ()
    return tuple(
        Artifact(role, kind, path.absolute())
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    )


def _bind_source_paths(
    project: Any,
    owner_name: str,
    step: Step,
    paths: Mapping[Path, str] | tuple[Path, ...] | frozenset[Path],
) -> Mapping[Path, tuple[str, str]]:
    """Bind Project-owned inputs; package code and PDK files stay runtime resources."""

    project_root = project.project_root.resolve()
    owner_root = project.owner(owner_name).root.resolve()
    artifact_root = Path(
        getattr(project, "artifact_root", project_root / "artifacts")
    ).resolve()
    selected: dict[Path, tuple[str, str]] = {}
    for source in paths:
        path = Path(source).absolute()
        if path != path.resolve():
            raise ContractError(f"backend source must not traverse a symlink: {path}")
        if path.is_relative_to(artifact_root):
            continue
        if path.is_relative_to(owner_root):
            name = path.relative_to(owner_root).as_posix()
        elif path.is_relative_to(project_root):
            name = path.relative_to(project_root).as_posix()
        else:
            continue
        snapshot = read_nofollow_text(path)
        if isinstance(paths, Mapping):
            expected = paths[source]
            if snapshot != expected:
                raise ContractError(f"typed backend source snapshot drift: {path}")
        selected[path] = (
            name,
            hashlib.sha256(snapshot.encode("utf-8")).hexdigest(),
        )
    return MappingProxyType(selected)


def _validate_oa_plan_sources(
    project: Any,
    owner_name: str,
    planning: Any,
    paths: frozenset[Path],
) -> Mapping[Path, str]:
    from sigilicon.workflows.oa_library import validate_oa_plan_source_members

    project_root = project.project_root.resolve()
    owner_root = project.owner(owner_name).root.resolve()
    members: list[Source] = []
    for source in paths:
        path = Path(source).absolute()
        if path != path.resolve():
            raise ContractError(f"OA plan source must not traverse a symlink: {path}")
        if path.is_relative_to(owner_root):
            root, scope = owner_root, "owner"
        elif path.is_relative_to(project_root):
            root, scope = project_root, "project"
        else:
            root, scope = path.parent, "resource"
        members.append(Source.capture(path, root=root, scope=scope))
    try:
        validate_oa_plan_source_members(planning, members)
    except ValueError as exc:
        raise ContractError(str(exc)) from exc
    return MappingProxyType({member.location: member.text for member in members})


def _require_bound_sources(
    context: StepContext,
    sources: Mapping[Path, tuple[str, str]],
) -> None:
    for name, digest in sources.values():
        current = hashlib.sha256(
            context.source_text(name).encode("utf-8")
        ).hexdigest()
        if current != digest:
            raise ExecutionError(f"sealed backend source identity drift: {name}")


def _external_file_records(
    project: Any,
    source_records: Mapping[Path, str],
    extra_paths: tuple[Path, ...] = (),
    identities: Mapping[Path, str] = MappingProxyType({}),
) -> tuple[ResourceBinding, ...]:
    project_root = project.project_root.resolve()
    artifact_root = getattr(project, "artifact_root", None)
    artifact_root = (
        None if artifact_root is None else Path(artifact_root).resolve()
    )
    selected: dict[str, tuple[Path, str | None]] = {}
    seen: set[Path] = set()
    entries = (
        *((source, False) for source in source_records),
        *((source, True) for source in extra_paths),
    )
    for source, include_project in entries:
        path = Path(source).absolute()
        if path != path.resolve():
            raise ContractError(f"external resource must not traverse a symlink: {path}")
        if (
            not (artifact_root is not None and path.is_relative_to(artifact_root))
            and path.is_relative_to(project_root)
            and not include_project
        ):
            continue
        if path in seen:
            continue
        expected = source_records.get(source)
        if artifact_root is not None and path.is_relative_to(artifact_root):
            identity = identities.get(
                path,
                "release:" + path.relative_to(artifact_root).as_posix(),
            )
        elif path.is_relative_to(project_root):
            identity = "project:" + path.relative_to(project_root).as_posix()
        elif path in identities:
            identity = identities[path]
        else:
            raise ContractError(
                f"external resource has no logical identity: {path.name}"
            )
        previous = selected.get(identity)
        if previous is not None and previous[0] != path:
            raise ContractError(f"external resource identity is ambiguous: {identity}")
        seen.add(path)
        selected[identity] = (path, expected)
    resources = tuple(
        ResourceBinding.capture(path, identity=identity)
        for identity, (path, _expected) in sorted(selected.items())
    )
    for resource in resources:
        expected = selected[resource.identity][1]
        if expected is not None and expected.encode("utf-8") != resource.data:
            raise ContractError(
                f"external resource changed during planning: {resource.identity}"
            )
    return resources


def _platform_resource_identities(platform: Any) -> Mapping[Path, str]:
    selected: dict[Path, str] = {}
    key = getattr(platform, "key", None)
    if not isinstance(key, str) or not key:
        return MappingProxyType(selected)
    simulation = getattr(platform, "simulation", None)
    model_sets = getattr(simulation, "model_sets", {})
    if isinstance(model_sets, Mapping):
        for name, model_set in sorted(model_sets.items()):
            for index, path in enumerate(model_set.files):
                selected[Path(path).absolute()] = (
                    f"pdk:{key}:simulation/{name}/{index}-{Path(path).name}"
                )
    layout = getattr(platform, "layout", None)
    if layout is not None:
        for role in ("layermap", "drc_deck", "lvs_deck", "qrc_tech_file"):
            path = getattr(layout, role, None)
            if path is not None:
                selected[Path(path).absolute()] = f"pdk:{key}:layout/{role}"
    return MappingProxyType(selected)


def _model_resource_identities(platform: Any, model_set: Any) -> Mapping[Path, str]:
    selected = dict(_platform_resource_identities(platform))
    key = getattr(platform, "key", None)
    name = getattr(model_set, "name", None)
    if not isinstance(key, str) or not key or not isinstance(name, str) or not name:
        return MappingProxyType(selected)
    for index, path in enumerate(model_set.files):
        selected[Path(path).absolute()] = (
            f"pdk:{key}:simulation/{name}/{index}-{Path(path).name}"
        )
    return MappingProxyType(selected)


def _ams_resource_identities(planning: Any) -> Mapping[Path, str]:
    selected = dict(
        _model_resource_identities(planning.platform, planning.model_set)
    )
    releases = planning.integration_check.get("dependency_releases")
    if not isinstance(releases, list) or len(releases) != 1:
        raise ContractError("Xcelium AMS release identity is unavailable")
    release = releases[0]
    if not isinstance(release, Mapping):
        raise ContractError("Xcelium AMS release identity is invalid")
    dependency = release.get("name")
    release_id = release.get("release_id")
    if not isinstance(dependency, str) or not isinstance(release_id, str):
        raise ContractError("Xcelium AMS release identity is incomplete")
    role = planning.spec.ams.circuit_role
    prefix = f"release:{dependency}:{release_id}"
    selected[planning.circuit_netlist.absolute()] = f"{prefix}/role/{role}"
    for source in planning.source_records:
        path = Path(source).absolute()
        if path.name == "manifest.json" and path.parent.parent.name == "objects":
            selected[path] = f"{prefix}/manifest"
    return MappingProxyType(selected)


def _oa_resource_identities(
    project: Any,
    planning: Any,
    paths: Mapping[Path, str],
    resources: Resources,
) -> Mapping[Path, str]:
    root = project.project_root.resolve()
    if not any(not Path(path).absolute().is_relative_to(root) for path in paths):
        return MappingProxyType({})
    from sigilicon.domain.platform import load_platform

    pdk = getattr(getattr(planning, "source", None), "pdk", None)
    if not isinstance(pdk, str) or not pdk:
        raise ContractError("OA plan external resources have no platform identity")
    platform = load_platform(project, pdk, resources=resources)
    selected = dict(_platform_resource_identities(platform))
    asset_root = platform.asset_root
    if asset_root is not None:
        for source in paths:
            path = Path(source).absolute()
            if path in selected or not path.is_relative_to(asset_root):
                continue
            relative = path.relative_to(asset_root).as_posix()
            basename = re.sub(r"[^A-Za-z0-9._-]", "-", path.name)
            identity_hash = hashlib.sha256(relative.encode("utf-8")).hexdigest()
            selected[path] = (
                f"pdk:{platform.key}:asset/{identity_hash[:20]}-{basename}"
            )
    for source in paths:
        path = Path(source).absolute()
        if path in selected or not path.is_relative_to(_PACKAGE_ROOT):
            continue
        selected[path] = (
            "package:sigilicon/" + path.relative_to(_PACKAGE_ROOT).as_posix()
        )
    return MappingProxyType(selected)


def _captured_project_sources(
    project: Any,
    owner_name: str,
    sources: Mapping[Path, tuple[str, str]],
) -> tuple[Source, ...]:
    project_root = project.project_root.resolve()
    owner_root = project.owner(owner_name).root.resolve()
    captured: list[Source] = []
    for path in sorted(sources, key=str):
        if path.is_relative_to(owner_root):
            root, scope = owner_root, "owner"
        elif path.is_relative_to(project_root):
            root, scope = project_root, "project"
        else:
            raise ContractError(f"prepared source is outside the Project: {path}")
        captured.append(Source.capture(path, root=root, scope=scope))
    return tuple(captured)


def _portable_request(
    config: Mapping[str, Any],
    prepared: Mapping[str, Any],
) -> Mapping[str, Any]:
    return {
        "config": dict(config),
        "prepared": dict(prepared),
    }


@dataclass(frozen=True)
class _PreparedCadencePlan:
    """Adapter-private domain plan plus its sealed-path correspondence."""

    plan: object
    prepared: Mapping[str, Any]
    sources: Mapping[Path, tuple[str, str]]
    resources: tuple[tuple[Path, str, str], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.prepared, Mapping):
            raise ContractError("prepared Cadence identity must be a mapping")
        if not isinstance(self.sources, Mapping):
            raise ContractError("prepared Cadence sources must be a mapping")
        if len({identity for _path, identity, _digest in self.resources}) != len(
            self.resources
        ):
            raise ContractError("prepared Cadence resources contain duplicate identities")
        object.__setattr__(self, "prepared", MappingProxyType(dict(self.prepared)))
        object.__setattr__(self, "sources", MappingProxyType(dict(self.sources)))

    @classmethod
    def create(
        cls,
        plan: object,
        prepared: Mapping[str, Any],
        sources: Mapping[Path, tuple[str, str]],
        resources: tuple[ResourceBinding, ...],
    ) -> "_PreparedCadencePlan":
        return cls(
            plan,
            prepared,
            sources,
            tuple(
                (resource.location, resource.identity, resource.sha256)
                for resource in resources
            ),
        )

    @property
    def identity(self) -> str:
        """Return the deterministic identity recorded by the Step."""

        return canonical_digest(
            {
                "prepared": json_value(self.prepared),
                "sources": sorted(
                    (name, digest) for name, digest in self.sources.values()
                ),
                "resources": sorted(
                    (identity, digest)
                    for _path, identity, digest in self.resources
                ),
            }
        )

    def validate(self, context: StepContext) -> None:
        expected = context.step.request.get("prepared")
        if not isinstance(expected, Mapping):
            raise ExecutionError("Cadence Domain plan identity drift")
        recorded = dict(expected)
        identity = recorded.pop("domain_plan_identity", None)
        if (
            identity != self.identity
            or canonical_digest(json_value(recorded))
            != canonical_digest(json_value(self.prepared))
        ):
            raise ExecutionError("Cadence Domain plan identity drift")
        if tuple(identity for _path, identity, _digest in self.resources) != (
            context.step.resources
        ):
            raise ExecutionError("Cadence external resource identity drift")
        _require_bound_sources(context, self.sources)
        for _path, identity, _digest in self.resources:
            context.resource_path(identity)

    def source_paths(self, context: StepContext) -> Mapping[Path, Path]:
        self.validate(context)
        return MappingProxyType(
            {
                original: context.source_path(name)
                for original, (name, _digest) in self.sources.items()
            }
        )

    def resource_paths(self, context: StepContext) -> Mapping[Path, Path]:
        self.validate(context)
        return MappingProxyType(
            {
                original: context.resource_path(identity)
                for original, identity, _digest in self.resources
            }
        )

    def resource_text(self, context: StepContext) -> Mapping[Path, str]:
        self.validate(context)
        return MappingProxyType(
            {
                original: context.resource_text(identity)
                for original, identity, _digest in self.resources
            }
        )


class _CadenceDomainAdapter:
    """Attach a non-portable domain value to its fully recorded Step."""

    def _bind_domain_plan(
        self,
        operation: Step,
        *,
        config: Mapping[str, Any],
        plan: object,
        prepared: Mapping[str, Any],
        sources: Mapping[Path, tuple[str, str]],
        captured: tuple[Source, ...],
        resources: tuple[ResourceBinding, ...],
    ) -> Step:
        domain_plan = _PreparedCadencePlan.create(
            plan,
            prepared,
            sources,
            resources,
        )
        portable = {**prepared, "domain_plan_identity": domain_plan.identity}
        source_snapshots = tuple(
            dict.fromkeys((*operation._source_snapshots, *captured))
        )
        return replace(
            operation,
            request=_portable_request(config, portable),
            sources=tuple(
                dict.fromkeys((*operation.sources, *(source.path for source in captured)))
            ),
            resources=tuple(resource.identity for resource in resources),
            _source_snapshots=source_snapshots,
            _resource_bindings=resources,
            _payload=domain_plan,
        )

    def _prepared_domain_plan(self, context: StepContext) -> _PreparedCadencePlan:
        domain_plan = context.step._payload
        if not isinstance(domain_plan, _PreparedCadencePlan):
            raise ExecutionError("Cadence Step has no planned Domain value")
        domain_plan.validate(context)
        return domain_plan


class XceliumBackend(DirectAdapter):
    """Execute one explicit, source-closed Verilog/SystemVerilog testbench."""

    name = "cadence.xcelium"
    _fields = frozenset({"success_marker", "timeout_seconds"})

    @staticmethod
    def _hdl_sources(step: Step) -> tuple[str, ...]:
        sources = tuple(
            source
            for source in step.sources
            if Path(source).suffix.lower() in {".sv", ".svh", ".v", ".vh"}
        )
        if not sources:
            raise ContractError("Xcelium filesets select no Verilog sources")
        return sources

    def preflight(self, step: Step, resources: Resources) -> tuple[PreflightCheck, ...]:
        config = _strict_config(step, self._fields)
        for source in self._hdl_sources(step):
            _relative(source, "HDL fileset source")
        _text(config, "success_marker")
        _positive_integer(config, "timeout_seconds")
        return (_executable_check(resources, _XRUN),)

    def run(self, context: StepContext, step: Step) -> StepResult:
        context.require_step(step)
        config = _strict_config(context.step, self._fields)
        source_names = self._hdl_sources(context.step)
        sources = tuple(
            context.owner_source_path(_relative(source, "hdl source"))
            for source in source_names
        )
        executable = _configured_executable(context.resources, _XRUN)
        if executable is None:
            raise ExecutionError("configured Xcelium executable is unavailable")
        timeout = _positive_integer(config, "timeout_seconds")
        marker = _text(config, "success_marker")
        with (
            owned_executable(executable) as owned_launcher,
            owned_directory(context.work_root) as work,
            owned_scratch_directory(
                prefix=f"sigilicon-xcelium-{context.run_id}-"
            ) as library,
        ):
            command = (
                *owned_launcher.command,
                "-64bit",
                "-sv",
                "-timescale",
                "1ns/1ps",
                "-xmlibdirname",
                library.child_path,
                "-log",
                f"{work.child_path}/xrun.log",
                *(str(source) for source in sources),
            )
            completed = managed_process.run(ProcessRequest(
                argv=tuple(command),
                cwd=Path(work.child_path),
                environment=xrun_env(executable, context.resources.environment),
                timeout_seconds=timeout,
                before_spawn=(
                    lambda: (
                        owned_launcher.require_visible(),
                        work.require_visible(),
                        library.require_visible(),
                    )
                ),
                pass_fds=(work.fd, library.fd),
            ))
        native = context.work_root / "xrun.log"
        native_text = (
            native.read_text(encoding="utf-8", errors="replace")
            if native.is_file() and not native.is_symlink()
            else ""
        )
        marker_sources = tuple(
            name
            for name, value in (("stdout", completed.stdout), ("native-log", native_text))
            if marker in value
        )
        passed = completed.returncode == 0 and bool(marker_sources)
        artifacts = (
            Artifact(
                "xcelium",
                "log.cadence-xcelium",
                context.write_text("xcelium", "stdout.log", completed.stdout),
            ),
            Artifact(
                "xcelium",
                "log.cadence-xcelium",
                context.write_text("xcelium", "stderr.log", completed.stderr or ""),
            ),
            Artifact(
                "xcelium",
                "log.cadence-xcelium",
                context.write_text("xcelium", "xrun.log", native_text),
            ),
        )
        summary = {
            "schema": 1,
            "contract_kind": "cadence-execution",
            "tool": "xcelium",
            "returncode": completed.returncode,
            "completion_proven": bool(marker_sources),
            "success_marker": marker,
            "success_marker_sources": list(marker_sources),
            "passed": passed,
        }
        artifacts += (
            Artifact(
                "xcelium",
                "summary.cadence-xcelium",
                context.write_text(
                    "xcelium",
                    "summary.json",
                    json.dumps(summary, sort_keys=True, separators=(",", ":")) + "\n",
                ),
            ),
        )
        return (
            StepResult.succeeded(artifacts=artifacts, facts={"passed": True})
            if passed
            else StepResult(
                "failed",
                artifacts,
                {"passed": False},
                "Xcelium did not prove a successful declared testbench",
            )
        )


class XceliumAmsBackend(_CadenceDomainAdapter):
    """Execute one locked-release Verilog-AMS migration cell."""

    name = "cadence.xcelium-ams"
    _fields = frozenset({"owner", "cell", "timeout_seconds"})

    def preflight(self, step: Step, resources: Resources) -> tuple[PreflightCheck, ...]:
        config = _strict_config(step, self._fields)
        _text(config, "owner")
        cell = _relative(_text(config, "cell"), "verification cell")
        if cell not in step.sources:
            raise ContractError("Xcelium AMS cell must be inside the source closure")
        _positive_integer(config, "timeout_seconds")
        if step.evidence is None:
            raise ContractError("Xcelium AMS execution requires an evidence envelope")
        return (_executable_check(resources, _XRUN),)

    def plan(
        self,
        project: PlanningProject,
        step: Step,
        resources: Resources,
    ) -> Step:
        from sigilicon.workflows.xcelium_ams import plan_xcelium_ams_cell

        initial = step
        config = _strict_config(initial, self._fields)
        owner = _text(config, "owner")
        selected_owner = project.owner(owner)
        contract = selected_owner.root / _relative(
            _text(config, "cell"), "verification cell"
        )
        planning = plan_xcelium_ams_cell(
            contract,
            project=project,
            resources=resources,
        )
        required = frozenset(
            {
                *planning.source_records,
                *planning.sources,
                planning.circuit_netlist,
                *planning.model_set.files,
            }
        )
        if required != frozenset(planning.source_records):
            raise ContractError("Xcelium AMS plan source snapshot is incomplete")
        bindings = _bind_source_paths(
            project,
            owner,
            step,
            planning.source_records,
        )
        external = _external_file_records(
            project,
            planning.source_records,
            identities=_ams_resource_identities(planning),
        )
        captured = _captured_project_sources(project, owner, bindings)
        prepared_identity = {"identity": canonical_digest(planning.as_dict())}
        prepared = self._bind_domain_plan(
            step,
            config=config,
            plan=planning,
            prepared=prepared_identity,
            sources=bindings,
            captured=captured,
            resources=external,
        )
        self.preflight(prepared, resources)
        return prepared

    def run(self, context: StepContext, step: Step) -> StepResult:
        context.require_step(step)
        from sigilicon.workflows.xcelium_ams import execute_xcelium_ams_cell

        config = _strict_config(step, self._fields)
        owner = _text(config, "owner")
        prepared = self._prepared_domain_plan(context)
        planning = prepared.plan
        xrun = _configured_executable(context.resources, _XRUN)
        if xrun is None:
            raise ExecutionError("configured Xcelium executable is unavailable")
        with owned_scratch_directory(
            prefix=f"sigilicon-xcelium-ams-{context.run_id}-",
            retain_on_error=lambda exc: process_group_cleanup_uncertainty(exc)
            is not None,
        ) as scratch:
            artifacts = context.files(
                "xcelium-ams",
                {
                    "owner": owner,
                    "cell": str(config["cell"]),
                },
                tool_work_root=scratch.path,
            )
            bound_sources = dict(prepared.source_paths(context))
            bound_sources.update(prepared.resource_paths(context))
            result = execute_xcelium_ams_cell(
                planning,
                artifacts=artifacts,
                xrun=xrun,
                source_paths=bound_sources,
                environment_values=context.resources.environment,
                timeout=_positive_integer(config, "timeout_seconds"),
            )
        envelope = context.step.evidence
        if envelope is None:
            raise ExecutionError("Xcelium AMS lost its evidence envelope")
        context.write_text(
            "xcelium-ams",
            "flow-evidence.json",
            json.dumps(
                {
                    "schema": 1,
                    "contract_kind": "cadence-execution-evidence",
                    "plan_identity": context.plan_identity,
                    "tool": "xcelium-ams",
                    "cell": planning.spec.cell,
                    "evidence_role": envelope.role,
                    "evidence_level": envelope.level,
                    "evidence_scope": envelope.scope,
                    "passed": result.passed,
                    "product_qualification_conclusion": False,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
        )
        published = _publish_tree(context, "xcelium-ams", "evidence.xcelium-ams")
        facts = {
            "passed": result.passed,
            "evidence_role": envelope.role,
            "evidence_level": envelope.level,
            "evidence_scope": envelope.scope,
            "product_qualification_conclusion": False,
        }
        return (
            StepResult.succeeded(artifacts=published, facts=facts)
            if result.passed
            else StepResult(
                "failed",
                published,
                facts,
                "Xcelium AMS did not prove the declared migration testbench",
            )
        )


class NativeOaBackend(_CadenceDomainAdapter):
    """Run one source-owned native Maestro testbench through a bound OA session."""

    name = "cadence.native-oa"
    _fields = frozenset({"owner", "testbench", "timeout_seconds"})

    def preflight(self, step: Step, resources: Resources) -> tuple[PreflightCheck, ...]:
        config = _strict_config(step, self._fields)
        _text(config, "owner")
        _text(config, "testbench")
        _positive_integer(config, "timeout_seconds")
        if "configs/oa.toml" not in step.sources:
            raise ContractError("native OA step must close over configs/oa.toml")
        return (
            _bridge_check(resources),
            _executable_check(resources, CADENCE_VIRTUOSO_TOOL),
            *_capability_checks(resources, _OA_CAPABILITIES),
        )

    def plan(
        self,
        project: PlanningProject,
        step: Step,
        resources: Resources,
    ) -> Step:
        from sigilicon.domain.platform import load_platform_inventory
        from sigilicon.workflows.oa_library import (
            oa_plan_source_paths,
            plan_oa_library_rebuild,
        )

        initial = step
        config = _strict_config(initial, self._fields)
        owner = _text(config, "owner")
        selected_owner = project.owner(owner)
        manifest = project.oa_assembly_for(selected_owner.root)
        if manifest is None:
            raise ContractError(f"owner {owner!r} has no OA assembly")
        platforms = load_platform_inventory(project, resources=resources)
        planning = plan_oa_library_rebuild(
            manifest,
            project=project,
            platform_inventory=platforms,
        )
        testbench = _text(config, "testbench")
        matches = tuple(item for item in planning.testbenches if item.cell == testbench)
        if len(matches) != 1:
            raise ContractError(f"unknown native OA testbench: {testbench}")
        required = _validate_oa_plan_sources(
            project,
            owner,
            planning,
            oa_plan_source_paths(planning),
        )
        sources = _bind_source_paths(
            project,
            owner,
            step,
            required,
        )
        external = _external_file_records(
            project,
            required,
            identities=_oa_resource_identities(
                project,
                planning,
                required,
                resources,
            ),
        )
        captured = _captured_project_sources(project, owner, sources)
        prepared_identity = {
            "assembly_identity": canonical_digest(planning.as_dict()),
            "library": planning.library,
            "testbench": matches[0].cell,
        }
        prepared = self._bind_domain_plan(
            step,
            config=config,
            plan=planning,
            prepared=prepared_identity,
            sources=sources,
            captured=captured,
            resources=external,
        )
        self.preflight(prepared, resources)
        return prepared

    def run(self, context: StepContext, step: Step) -> StepResult:
        context.require_step(step)
        from sigilicon.virtuoso.client import get_client
        from sigilicon.workflows.oa_library import build_oa_layout_ir
        from sigilicon.workflows.oa_simulation import execute_oa_maestro_testbench

        config = _strict_config(step, self._fields)
        owner = _text(config, "owner")
        testbench = _text(config, "testbench")
        prepared = self._prepared_domain_plan(context)
        plan = build_oa_layout_ir(
            prepared.plan,
            source_paths=prepared.source_paths(context),
            managed_project_root=context.work_root / "layout-ir",
        )
        matches = tuple(item for item in plan.testbenches if item.cell == testbench)
        if len(matches) != 1:
            raise ExecutionError(f"prepared native OA testbench is invalid: {testbench}")
        selected = matches[0]
        uncertainty: list[str] = []
        try:
            with owned_scratch_directory(
                prefix=f"sigilicon-maestro-{context.run_id}-",
                retain_on_error=lambda exc: bool(uncertainty)
                or process_group_cleanup_uncertainty(exc) is not None,
            ) as scratch:
                artifacts = context.files(
                    "maestro",
                    {"owner": owner, "testbench": testbench},
                    tool_work_root=scratch.path,
                )
                result = execute_oa_maestro_testbench(
                    plan,
                    selected,
                    get_client(context.resources),
                    artifacts=artifacts,
                    operation_id=context.operation_id,
                    bind_operation=context.bind_workspace_operation,
                    resources=context.resources,
                    record_uncertainty=uncertainty.append,
                    timeout=_positive_integer(config, "timeout_seconds"),
                )
        except Exception:
            published = _publish_tree(context, "maestro", "evidence.cadence-maestro")
            if uncertainty:
                return StepResult(
                    "uncertain",
                    published,
                    {"workspace_uncertainty": tuple(uncertainty)},
                    " | ".join(uncertainty),
                )
            raise
        published = _publish_tree(context, "maestro", "evidence.cadence-maestro")
        if not published:
            raise ExecutionError("native Maestro produced no managed evidence")
        return (
            StepResult.succeeded(
                artifacts=published,
                facts={"passed": result.passed, "evidence_status": result.evidence.status},
            )
            if result.passed
            else StepResult(
                "failed",
                published,
                {"passed": False, "evidence_status": result.evidence.status},
                "native Maestro evidence did not pass",
            )
        )


class _OaBackend(_CadenceDomainAdapter):
    """Execute one fixed native-OA operation against a plan-bound assembly."""

    _base_fields = frozenset({"owner", "timeout_seconds"})

    def __init__(self, operation: str) -> None:
        if operation not in {"check", "rebuild", "attest"}:
            raise ValueError(f"unsupported OA operation: {operation}")
        self.operation = operation
        self.name = f"cadence.oa-{operation}"

    def _config(self, step: Step) -> Mapping[str, Any]:
        request = step.request.get("config", step.request)
        if not isinstance(request, Mapping):
            raise ContractError("prepared OA config must be a mapping")
        fields = self._base_fields | (
            {"testbench"} if self.operation == "attest" else set()
        )
        config = _strict_config(step, frozenset(fields))
        _text(config, "owner")
        _positive_integer(config, "timeout_seconds")
        if self.operation == "attest":
            _text(config, "testbench")
        if "configs/oa.toml" not in step.sources:
            raise ContractError("OA management step must close over configs/oa.toml")
        return config

    def preflight(self, step: Step, resources: Resources) -> tuple[PreflightCheck, ...]:
        self._config(step)
        prepared = step.request.get("prepared")
        runtime_executables: tuple[str, ...] = ()
        if prepared is not None:
            if not isinstance(prepared, Mapping):
                raise ContractError("prepared OA identity must be a mapping")
            selected = prepared.get("runtime_executables")
            if not isinstance(selected, tuple) or any(
                item not in {CADENCE_SPICEIN_TOOL, CADENCE_TEXT_IMPORT_TOOL}
                for item in selected
            ):
                raise ContractError(
                    "prepared OA runtime executables disagree with their contract"
                )
            runtime_executables = selected
        return (
            _bridge_check(resources),
            *(
                _executable_check(resources, name)
                for name in runtime_executables
            ),
            *_capability_checks(resources, _OA_CAPABILITIES),
        )

    def plan(
        self,
        project: PlanningProject,
        step: Step,
        resources: Resources,
    ) -> Step:
        from sigilicon.domain.platform import load_platform_inventory
        from sigilicon.workflows.oa_library import (
            oa_plan_source_paths,
            plan_oa_library_rebuild,
        )

        initial = step
        config = self._config(initial)
        owner = _text(config, "owner")
        manifest = project.oa_assembly_for(project.owner(owner).root)
        if manifest is None:
            raise ContractError(f"owner {owner!r} has no OA assembly")
        platforms = load_platform_inventory(project, resources=resources)
        planning = plan_oa_library_rebuild(
            manifest,
            project=project,
            platform_inventory=platforms,
        )
        selected = None
        if self.operation == "attest":
            testbench = _text(config, "testbench")
            matches = tuple(
                item for item in planning.testbenches if item.cell == testbench
            )
            if len(matches) != 1:
                raise ContractError(f"unknown native OA testbench: {testbench}")
            selected = matches[0]
        required = _validate_oa_plan_sources(
            project,
            owner,
            planning,
            oa_plan_source_paths(planning),
        )
        sources = _bind_source_paths(project, owner, step, required)
        external = _external_file_records(
            project,
            required,
            identities=_oa_resource_identities(
                project,
                planning,
                required,
                resources,
            ),
        )
        captured = _captured_project_sources(project, owner, sources)
        prepared_identity = {
            "assembly_identity": canonical_digest(planning.as_dict()),
            "library": planning.library,
            "operation": self.operation,
            "testbench": None if selected is None else selected.cell,
            "runtime_executables": _oa_runtime_executables(
                planning,
                self.operation,
            ),
        }
        prepared = self._bind_domain_plan(
            step,
            config=config,
            plan=planning,
            prepared=prepared_identity,
            sources=sources,
            captured=captured,
            resources=external,
        )
        self.preflight(prepared, resources)
        return prepared

    def run(self, context: StepContext, step: Step) -> StepResult:
        context.require_step(step)
        from sigilicon.virtuoso.client import get_client
        from sigilicon.workflows.oa_check import check_oa_library
        from sigilicon.workflows.oa_library import (
            attest_oa_testbench,
            build_oa_layout_ir,
            rebuild_oa_library,
        )

        config = self._config(step)
        owner = _text(config, "owner")
        if context.workspace_root is None:
            raise ExecutionError("OA management requires Project runtime roots")
        prepared = self._prepared_domain_plan(context)
        planning = prepared.plan
        if self.operation in {"check", "rebuild"}:
            planning = build_oa_layout_ir(
                planning,
                source_paths=prepared.source_paths(context),
                managed_project_root=context.work_root / "layout-ir",
            )
        selected = None
        if self.operation == "attest":
            testbench = _text(config, "testbench")
            matches = tuple(item for item in planning.testbenches if item.cell == testbench)
            if len(matches) != 1:
                raise ExecutionError(f"prepared OA testbench is invalid: {testbench}")
            selected = matches[0]
        timeout = _positive_integer(config, "timeout_seconds")
        client = get_client(context.resources)
        if self.operation == "check":
            from sigilicon.virtuoso.workspace import (
                OperationPolicy,
                workspace_operation,
            )

            with workspace_operation(
                client,
                context.workspace_root,
                "check-oa-library",
                policy=OperationPolicy.READ_ONLY,
                acquire_flow_lock=False,
                record_incident=False,
                operation_id=context.operation_id,
            ) as operation:
                context.bind_workspace_operation(operation)
                payload = check_oa_library(
                    planning,
                    client=client,
                    timeout=timeout,
                    operation=operation,
                )
        elif self.operation == "rebuild":
            payload = rebuild_oa_library(
                planning,
                client,
                source_paths=prepared.source_paths(context),
                resource_paths=prepared.resource_paths(context),
                resources=context.resources,
                timeout=timeout,
                operation_id=context.operation_id,
                bind_operation=context.bind_workspace_operation,
            )
        else:
            if selected is None:
                raise ExecutionError("OA attest preparation lost its testbench")
            payload = attest_oa_testbench(
                planning,
                selected,
                client,
                timeout=timeout,
                operation_id=context.operation_id,
                bind_operation=context.bind_workspace_operation,
            )
        output = context.write_text(
            "oa",
            f"{self.operation}.json",
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        )
        passed = bool(payload.get("passed"))
        artifacts = (Artifact("oa", "evidence.cadence-oa", output),)
        facts = {"passed": passed, "operation": self.operation}
        return (
            StepResult.succeeded(artifacts=artifacts, facts=facts)
            if passed
            else StepResult(
                "failed",
                artifacts,
                facts,
                f"OA {self.operation} did not pass",
            )
        )


class LayoutBackend(_CadenceDomainAdapter):
    """Generate one source-authored layout through a bound OA mutation lease."""

    name = "cadence.layout"
    _fields = frozenset({"owner", "spec", "timeout_seconds"})

    def preflight(self, step: Step, resources: Resources) -> tuple[PreflightCheck, ...]:
        config = _strict_config(step, self._fields)
        _text(config, "owner")
        spec = _relative(_text(config, "spec"), "layout spec")
        if spec not in step.sources:
            raise ContractError("layout spec must be inside the operation source closure")
        _positive_integer(config, "timeout_seconds")
        return (
            _bridge_check(resources),
            *_capability_checks(resources, _OA_CAPABILITIES),
        )

    def plan(
        self,
        project: PlanningProject,
        step: Step,
        resources: Resources,
    ) -> Step:
        from sigilicon.domain.platform import load_platform_inventory
        from sigilicon.workflows.layout_generation import plan_layout_spec

        initial = step
        config = _strict_config(initial, self._fields)
        owner = _text(config, "owner")
        spec = project.owner(owner).root / _relative(
            _text(config, "spec"), "layout spec"
        )
        platforms = load_platform_inventory(project, resources=resources)
        planning = plan_layout_spec(
            spec,
            project=project,
            platform=platforms,
        )
        sources = _bind_source_paths(
            project,
            owner,
            step,
            planning.source_records,
        )
        external = _external_file_records(
            project,
            planning.source_records,
            identities=_platform_resource_identities(planning.spec.pdk),
        )
        captured = _captured_project_sources(project, owner, sources)
        prepared_identity = {
            "library": planning.spec.library,
            "cell": planning.spec.cell,
            "view": planning.spec.view,
            "generator": planning.spec.generator,
            "stage": planning.spec.stage,
        }
        prepared = self._bind_domain_plan(
            step,
            config=config,
            plan=planning,
            prepared=prepared_identity,
            sources=sources,
            captured=captured,
            resources=external,
        )
        self.preflight(prepared, resources)
        return prepared

    def run(self, context: StepContext, step: Step) -> StepResult:
        context.require_step(step)
        from sigilicon.workflows.layout_generation import build_managed_layout_ir

        prepared = self._prepared_domain_plan(context)
        planning = build_managed_layout_ir(
            prepared.plan,
            source_paths=prepared.source_paths(context),
            managed_project_root=context.work_root / "layout-ir",
        )
        return self._execute(context, planning)

    def _execute(self, context: StepContext, planning: Any) -> StepResult:
        from sigilicon.virtuoso.client import get_client
        from sigilicon.workflows.layout_generation import generate_layout

        config = _strict_config(context.step, self._fields)
        owner = _text(config, "owner")
        uncertainty: list[str] = []
        try:
            with owned_scratch_directory(
                prefix=f"sigilicon-layout-{context.run_id}-",
                retain_on_error=lambda exc: bool(uncertainty)
                or process_group_cleanup_uncertainty(exc) is not None,
            ) as scratch:
                artifacts = context.files(
                    "layout",
                    {"owner": owner, "spec": str(config["spec"])},
                    tool_work_root=scratch.path,
                )
                result = generate_layout(
                    planning,
                    get_client(context.resources),
                    artifacts=artifacts,
                    operation_id=context.operation_id,
                    bind_operation=context.bind_workspace_operation,
                    record_uncertainty=uncertainty.append,
                    timeout=_positive_integer(config, "timeout_seconds"),
                )
        except Exception:
            published = _publish_tree(context, "layout", "evidence.cadence-layout")
            if uncertainty:
                return StepResult(
                    "uncertain",
                    published,
                    {"workspace_uncertainty": tuple(uncertainty)},
                    " | ".join(uncertainty),
                )
            raise
        published = _publish_tree(context, "layout", "evidence.cadence-layout")
        if not published:
            raise ExecutionError("layout generation produced no managed evidence")
        return StepResult.succeeded(
            artifacts=published,
            facts={"instance_count": result.instance_count},
        )


class LayoutVerificationBackend(_CadenceDomainAdapter):
    """Verify one existing routed OA layout with XStream and Calibre."""

    name = "cadence.layout-verify"
    _fields = frozenset(
        {
            "owner",
            "spec",
            "check",
            "xstream_timeout_seconds",
            "calibre_timeout_seconds",
        }
    )

    def preflight(self, step: Step, resources: Resources) -> tuple[PreflightCheck, ...]:
        config = _strict_config(step, self._fields)
        if step.evidence is None:
            raise ContractError("layout verification requires an evidence envelope")
        _text(config, "owner")
        spec = _relative(_text(config, "spec"), "layout spec")
        if spec not in step.sources:
            raise ContractError(
                "layout verification spec must be inside the source closure"
            )
        if _text(config, "check") not in {"drc", "lvs"}:
            raise ContractError("layout verification check must be drc or lvs")
        _positive_integer(config, "xstream_timeout_seconds")
        _positive_integer(config, "calibre_timeout_seconds")
        return (
            _bridge_check(resources),
            _executable_check(resources, _XSTREAM),
            _executable_check(resources, _CALIBRE),
            *_capability_checks(resources, _OA_CAPABILITIES),
        )

    def plan(
        self,
        project: PlanningProject,
        step: Step,
        resources: Resources,
    ) -> Step:
        from sigilicon.domain.platform import load_platform_inventory
        from sigilicon.workflows.layout_generation import plan_layout_spec

        initial = step
        config = _strict_config(initial, self._fields)
        owner = _text(config, "owner")
        spec = project.owner(owner).root / _relative(
            _text(config, "spec"), "layout spec"
        )
        platforms = load_platform_inventory(project, resources=resources)
        planning = plan_layout_spec(
            spec,
            project=project,
            platform=platforms,
        )
        if planning.spec.layout_pdk is None:
            raise ContractError("layout verification requires a layout PDK")
        deck = (
            planning.spec.layout_pdk.drc_deck
            if _text(config, "check") == "drc"
            else planning.spec.layout_pdk.lvs_deck
        )
        sources = _bind_source_paths(
            project,
            owner,
            step,
            planning.source_records,
        )
        external = _external_file_records(
            project,
            planning.source_records,
            (planning.spec.layout_pdk.layermap, deck),
            identities=_platform_resource_identities(planning.spec.pdk),
        )
        check = _text(config, "check")
        captured = _captured_project_sources(project, owner, sources)
        prepared_identity = {
            "library": planning.spec.library,
            "cell": planning.spec.cell,
            "view": planning.spec.view,
            "generator": planning.spec.generator,
            "stage": planning.spec.stage,
            "check": check,
        }
        prepared = self._bind_domain_plan(
            step,
            config=config,
            plan=planning,
            prepared=prepared_identity,
            sources=sources,
            captured=captured,
            resources=external,
        )
        self.preflight(prepared, resources)
        return prepared

    def run(self, context: StepContext, step: Step) -> StepResult:
        context.require_step(step)
        from sigilicon.workflows.layout_generation import build_managed_layout_ir

        config = _strict_config(step, self._fields)
        check = _text(config, "check")
        prepared = self._prepared_domain_plan(context)
        planning = build_managed_layout_ir(
            prepared.plan,
            source_paths=prepared.source_paths(context),
            managed_project_root=context.work_root / "layout-ir",
        )
        return self._execute(
            context,
            planning,
            prepared.resource_text(context),
        )

    def _execute(
        self,
        context: StepContext,
        planning: Any,
        external_sources: Mapping[Path, str],
    ) -> StepResult:
        from sigilicon.virtuoso.client import get_client
        from sigilicon.workflows.layout_verification import run_layout_verification

        config = _strict_config(context.step, self._fields)
        owner = _text(config, "owner")
        xstream = _configured_executable(context.resources, _XSTREAM)
        calibre = _configured_executable(context.resources, _CALIBRE)
        if xstream is None or calibre is None:
            raise ExecutionError(
                "configured XStream and Calibre executables are required"
            )
        uncertainty: list[str] = []
        try:
            with owned_scratch_directory(
                prefix=f"sigilicon-physical-{context.run_id}-",
                retain_on_error=lambda exc: bool(uncertainty)
                or process_group_cleanup_uncertainty(exc) is not None,
            ) as scratch:
                artifacts = context.files(
                    "verification",
                    {
                        "owner": owner,
                        "spec": str(config["spec"]),
                        "check": str(config["check"]),
                    },
                    tool_work_root=scratch.path,
                )
                result = run_layout_verification(
                    planning,
                    get_client(context.resources),
                    check=_text(config, "check"),
                    artifacts=artifacts,
                    xstream=xstream,
                    calibre=calibre,
                    environment=context.resources.environment,
                    external_sources=external_sources,
                    operation_id=context.operation_id,
                    bind_operation=context.bind_workspace_operation,
                    record_uncertainty=uncertainty.append,
                    xstream_timeout=_positive_integer(
                        config, "xstream_timeout_seconds"
                    ),
                    calibre_timeout=_positive_integer(
                        config, "calibre_timeout_seconds"
                    ),
                )
        except Exception:
            published = _publish_tree(
                context, "verification", "evidence.physical-verification"
            )
            if uncertainty:
                return StepResult(
                    "uncertain",
                    published,
                    {"workspace_uncertainty": tuple(uncertainty)},
                    " | ".join(uncertainty),
                )
            raise
        envelope = context.step.evidence
        if envelope is None:
            raise ExecutionError("layout verification lost its evidence envelope")
        context.write_text(
            "verification",
            "flow-evidence.json",
            json.dumps(
                {
                    "schema": 1,
                    "contract_kind": "physical-verification-evidence",
                    "plan_identity": context.plan_identity,
                    "platform": planning.spec.pdk.key,
                    "check": str(config["check"]),
                    "evidence_role": envelope.role,
                    "evidence_level": envelope.level,
                    "evidence_scope": envelope.scope,
                    "physical_verification": json.loads(
                        result.evidence.canonical_json()
                    ),
                    "product_qualification_conclusion": False,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
        )
        published = _publish_tree(
            context, "verification", "evidence.physical-verification"
        )
        if not published:
            raise ExecutionError("layout verification produced no managed evidence")
        facts = {
            "passed": result.passed,
            "check": str(config["check"]),
            "status": result.evidence.status.value,
            "evidence_role": envelope.role,
            "evidence_level": envelope.level,
            "evidence_scope": envelope.scope,
            "product_qualification_conclusion": False,
        }
        return (
            StepResult.succeeded(artifacts=published, facts=facts)
            if result.passed
            else StepResult(
                "failed",
                published,
                facts,
                f"Calibre {str(config['check']).upper()} did not prove clean",
            )
        )


def cadence_backends() -> tuple[
    XceliumBackend,
    XceliumAmsBackend,
    NativeOaBackend,
    _OaBackend,
    _OaBackend,
    _OaBackend,
    LayoutBackend,
    LayoutVerificationBackend,
]:
    return (
        XceliumBackend(),
        XceliumAmsBackend(),
        NativeOaBackend(),
        _OaBackend("check"),
        _OaBackend("rebuild"),
        _OaBackend("attest"),
        LayoutBackend(),
        LayoutVerificationBackend(),
    )


__all__ = [
    "LayoutBackend",
    "LayoutVerificationBackend",
    "NativeOaBackend",
    "XceliumBackend",
    "XceliumAmsBackend",
    "cadence_backends",
]
