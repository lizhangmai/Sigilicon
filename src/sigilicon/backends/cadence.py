"""Trusted Cadence backends for direct RTL, native OA, and layout execution."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from sigilicon.artifacts import read_nofollow_text
from sigilicon.canonical import canonical_digest
from sigilicon.execution.model import (
    Artifact,
    ContractError,
    ExecutionError,
    PreflightCheck,
    Resources,
    Source,
    Step,
    StepContext,
    StepResult,
)
from sigilicon.external_tools import (
    owned_directory,
    owned_scratch_directory,
    process_group_cleanup_uncertainty,
    run_process_group_capture,
    xrun_env,
)


_XRUN = "SIGILICON_CADENCE_XRUN"
_XSTREAM = "SIGILICON_CADENCE_XSTREAM"
_CALIBRE = "SIGILICON_CALIBRE"
_OA_CAPABILITIES = frozenset({"tool.virtuoso-bridge", "license.cadence-oa"})
_LAYOUT_VERIFICATION_CAPABILITIES = _OA_CAPABILITIES | frozenset(
    {"tool.cadence-xstream", "tool.calibre"}
)


def _strict_config(step: Step, fields: frozenset[str]) -> Mapping[str, Any]:
    unknown = set(step.config) - fields
    missing = fields - set(step.config)
    if unknown or missing:
        raise ContractError(
            f"{step.uses} config fields disagree with its contract; "
            f"missing={sorted(missing)}, unknown={sorted(unknown)}"
        )
    return step.config


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
            "supplied by the invoking runtime"
            if capability in resources.capabilities
            else "missing capability",
        )
        for capability in sorted(required)
    )


def _configured_executable(resources: Resources, name: str) -> Path | None:
    value = resources.environment.get(name)
    if not value:
        return None
    discovered = shutil.which(value, path=resources.environment.get("PATH"))
    path = Path(discovered if discovered is not None else os.path.abspath(value))
    return path if path.is_file() and os.access(path, os.X_OK) else None


def _executable_check(resources: Resources, name: str) -> PreflightCheck:
    path = _configured_executable(resources, name)
    ready = path is not None
    return PreflightCheck(
        "runtime-resource",
        name,
        "ready" if ready else "blocked",
        "executable supplied by the invoking runtime" if ready else "missing executable",
    )


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
    selected: dict[Path, tuple[str, str]] = {}
    for source in paths:
        path = Path(source).absolute()
        if path != path.resolve():
            raise ContractError(f"backend source must not traverse a symlink: {path}")
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
) -> Mapping[Path, tuple[str, str]]:
    project_root = project.project_root.resolve()
    selected: dict[Path, tuple[str, str]] = {}
    entries = (
        *((source, False) for source in source_records),
        *((source, True) for source in extra_paths),
    )
    for source, include_project in entries:
        path = Path(source).absolute()
        if path != path.resolve():
            raise ContractError(f"external resource must not traverse a symlink: {path}")
        if path.is_relative_to(project_root) and not include_project:
            continue
        snapshot = (
            source_records[source]
            if isinstance(source_records, Mapping) and source in source_records
            else read_nofollow_text(path)
        )
        selected[path] = (
            hashlib.sha256(snapshot.encode("utf-8")).hexdigest(),
            snapshot,
        )
    return MappingProxyType(selected)


def _require_external_files(sources: Mapping[Path, tuple[str, str]]) -> None:
    for path, (digest, _snapshot) in sources.items():
        try:
            current = hashlib.sha256(
                read_nofollow_text(path).encode("utf-8")
            ).hexdigest()
        except OSError as exc:
            raise ExecutionError(f"bound external resource is unavailable: {path}") from exc
        if current != digest:
            raise ExecutionError(f"bound external resource identity drift: {path}")


class XceliumBackend:
    """Execute one explicit, source-closed Verilog/SystemVerilog testbench."""

    name = "cadence.xcelium"
    _fields = frozenset(
        {"hdl_sources", "success_marker", "timeout_seconds"}
    )

    def preflight(self, step: Step, resources: Resources) -> tuple[PreflightCheck, ...]:
        config = _strict_config(step, self._fields)
        sources = _strings(config, "hdl_sources")
        for source in sources:
            _relative(source, "hdl source")
            if Path(source).suffix.lower() not in {".sv", ".svh", ".v", ".vh"}:
                raise ContractError(f"unsupported Xcelium HDL source: {source}")
        _text(config, "success_marker")
        _positive_integer(config, "timeout_seconds")
        return (
            _executable_check(resources, _XRUN),
            *_capability_checks(resources, frozenset({"tool.cadence-xcelium"})),
        )

    def run(self, context: StepContext) -> StepResult:
        config = _strict_config(context.step, self._fields)
        source_names = _strings(config, "hdl_sources")
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
            owned_directory(context.work_root) as work,
            owned_scratch_directory(
                prefix=f"sigilicon-xcelium-{context.run_id}-"
            ) as library,
        ):
            command = (
                str(executable),
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
            completed = run_process_group_capture(
                command,
                cwd=Path(work.child_path),
                env=xrun_env(executable, context.resources.environment),
                timeout=timeout,
                before_spawn=(
                    lambda: (work.require_visible(), library.require_visible())
                ),
                pass_fds=(work.fd, library.fd),
            )
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


class XceliumAmsBackend:
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
        return (
            _executable_check(resources, _XRUN),
            *_capability_checks(resources, frozenset({"tool.cadence-xcelium"})),
        )

    def bind(self, project: Any, step: Step) -> "_BoundXceliumAmsBackend":
        from sigilicon.workflows.xcelium_ams import plan_xcelium_ams_cell

        self.preflight(step, Resources())
        config = _strict_config(step, self._fields)
        owner = _text(config, "owner")
        selected_owner = project.owner(owner)
        contract = selected_owner.root / _relative(
            _text(config, "cell"), "verification cell"
        )
        planning = plan_xcelium_ams_cell(contract, project=project)
        required = frozenset(
            {
                *planning.source_records,
                *planning.sources,
                planning.circuit_netlist,
                *planning.model_set.files,
            }
        )
        bindings = _bind_source_paths(project, owner, step, required)
        return _BoundXceliumAmsBackend(planning, bindings)

    def run(self, context: StepContext) -> StepResult:
        raise ExecutionError("Xcelium AMS Step was not bound by Project.plan")


@dataclass(frozen=True)
class _BoundXceliumAmsBackend(XceliumAmsBackend):
    _planning: Any
    _sources: Mapping[Path, tuple[str, str]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "_sources", MappingProxyType(dict(self._sources)))

    @property
    def binding_sources(self) -> Mapping[Path, str]:
        return MappingProxyType(
            {path: digest for path, (_name, digest) in self._sources.items()}
        )

    @property
    def binding_record(self) -> Mapping[str, Any]:
        return {
            "schema": 1,
            "backend": self.name,
            "request": self._planning.as_dict(),
            "project_sources": [
                {"path": name, "sha256": digest}
                for name, digest in sorted(self._sources.values())
            ],
        }

    def bind(self, project: Any, step: Step) -> "_BoundXceliumAmsBackend":
        raise ContractError("Xcelium AMS Step is already bound")

    def run(self, context: StepContext) -> StepResult:
        from sigilicon.workflows.run_artifacts import RunArtifacts
        from sigilicon.workflows.xcelium_ams import execute_xcelium_ams_cell

        config = _strict_config(context.step, self._fields)
        owner = _text(config, "owner")
        planning = self._planning
        _require_bound_sources(context, self._sources)
        root_environment = planning.platform.installation_root_environment
        if root_environment is not None and context.resources.environment.get(
            root_environment
        ) != os.environ.get(root_environment):
            raise ExecutionError(
                "Xcelium AMS platform root differs from the resource snapshot"
            )
        xrun = _configured_executable(context.resources, _XRUN)
        if xrun is None:
            raise ExecutionError("configured Xcelium executable is unavailable")
        with owned_scratch_directory(
            prefix=f"sigilicon-xcelium-ams-{context.run_id}-",
            retain_on_error=lambda exc: process_group_cleanup_uncertainty(exc)
            is not None,
        ) as scratch:
            artifacts = RunArtifacts.from_step_context(
                context,
                "xcelium-ams",
                {
                    "owner": owner,
                    "cell": str(config["cell"]),
                },
                tool_work_root=scratch.path,
            )
            result = execute_xcelium_ams_cell(
                planning,
                artifacts=artifacts,
                xrun=xrun,
                source_paths={
                    path: context.source_path(name)
                    for path, (name, _digest) in self._sources.items()
                },
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


class NativeOaBackend:
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
        return _capability_checks(resources, _OA_CAPABILITIES)

    def bind(self, project: Any, step: Step) -> "_BoundNativeOaBackend":
        from sigilicon.workflows.oa_library import (
            oa_plan_source_paths,
            plan_oa_library_rebuild,
        )

        self.preflight(step, Resources())
        config = _strict_config(step, self._fields)
        owner = _text(config, "owner")
        selected_owner = project.owner(owner)
        manifest = project.oa_assembly_for(selected_owner.root)
        if manifest is None:
            raise ContractError(f"owner {owner!r} has no OA assembly")
        planning = plan_oa_library_rebuild(manifest, project=project)
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
        )
        return _BoundNativeOaBackend(planning, matches[0], sources, external)

    def run(self, context: StepContext) -> StepResult:
        raise ExecutionError("native OA Step was not bound by Project.plan")


@dataclass(frozen=True)
class _BoundNativeOaBackend(NativeOaBackend):
    _planning: Any
    _selected: Any
    _sources: Mapping[Path, tuple[str, str]]
    _external: Mapping[Path, tuple[str, str]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "_sources", MappingProxyType(dict(self._sources)))
        object.__setattr__(self, "_external", MappingProxyType(dict(self._external)))

    @property
    def binding_sources(self) -> Mapping[Path, str]:
        return MappingProxyType(
            {path: digest for path, (_name, digest) in self._sources.items()}
        )

    @property
    def binding_record(self) -> Mapping[str, Any]:
        return {
            "schema": 1,
            "backend": self.name,
            "request": {
                "assembly_identity": canonical_digest(self._planning.as_dict()),
                "library": self._planning.library,
                "testbench": self._selected.cell,
            },
            "project_sources": [
                {"path": name, "sha256": digest}
                for name, digest in sorted(self._sources.values())
            ],
            "external_sources": [
                {"path": str(path), "sha256": record[0]}
                for path, record in sorted(
                    self._external.items(), key=lambda item: str(item[0])
                )
            ],
        }

    def bind(self, project: Any, step: Step) -> "_BoundNativeOaBackend":
        raise ContractError("native OA Step is already bound")

    def run(self, context: StepContext) -> StepResult:
        from sigilicon.virtuoso.client import get_client
        from sigilicon.workflows.oa_simulation import execute_oa_maestro_testbench
        from sigilicon.workflows.run_artifacts import RunArtifacts

        config = _strict_config(context.step, self._fields)
        owner = _text(config, "owner")
        testbench = _text(config, "testbench")
        plan = self._planning
        selected = self._selected
        _require_bound_sources(context, self._sources)
        _require_external_files(self._external)
        uncertainty: list[str] = []
        try:
            with owned_scratch_directory(
                prefix=f"sigilicon-maestro-{context.run_id}-",
                retain_on_error=lambda exc: bool(uncertainty)
                or process_group_cleanup_uncertainty(exc) is not None,
            ) as scratch:
                artifacts = RunArtifacts.from_step_context(
                    context,
                    "maestro",
                    {"owner": owner, "testbench": testbench},
                    tool_work_root=scratch.path,
                )
                result = execute_oa_maestro_testbench(
                    plan,
                    selected,
                    get_client(),
                    artifacts=artifacts,
                    operation_id=context.operation_id,
                    bind_operation=context.bind_workspace_operation,
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


class OaBackend:
    """Check, rebuild, or attest one plan-bound native OA assembly."""

    name = "cadence.oa"
    _base_fields = frozenset({"owner", "action", "timeout_seconds"})

    def _config(self, step: Step) -> Mapping[str, Any]:
        action = _text(step.config, "action")
        fields = self._base_fields | ({"testbench"} if action == "attest" else set())
        config = _strict_config(step, frozenset(fields))
        if action not in {"check", "rebuild", "attest"}:
            raise ContractError("OA action must be check, rebuild, or attest")
        _text(config, "owner")
        _positive_integer(config, "timeout_seconds")
        if action == "attest":
            _text(config, "testbench")
        if "configs/oa.toml" not in step.sources:
            raise ContractError("OA management step must close over configs/oa.toml")
        return config

    def preflight(self, step: Step, resources: Resources) -> tuple[PreflightCheck, ...]:
        self._config(step)
        return _capability_checks(resources, _OA_CAPABILITIES)

    def bind(self, project: Any, step: Step) -> "_BoundOaBackend":
        from sigilicon.workflows.oa_library import (
            oa_plan_source_paths,
            plan_oa_library_rebuild,
        )

        self.preflight(step, Resources())
        config = self._config(step)
        owner = _text(config, "owner")
        manifest = project.oa_assembly_for(project.owner(owner).root)
        if manifest is None:
            raise ContractError(f"owner {owner!r} has no OA assembly")
        planning = plan_oa_library_rebuild(manifest, project=project)
        selected = None
        if _text(config, "action") == "attest":
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
        )
        return _BoundOaBackend(
            planning,
            _text(config, "action"),
            selected,
            sources,
            external,
        )

    def run(self, context: StepContext) -> StepResult:
        raise ExecutionError("OA management Step was not bound by Project.plan")


@dataclass(frozen=True)
class _BoundOaBackend(OaBackend):
    _planning: Any
    _action: str
    _selected: Any
    _sources: Mapping[Path, tuple[str, str]]
    _external: Mapping[Path, tuple[str, str]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "_sources", MappingProxyType(dict(self._sources)))
        object.__setattr__(self, "_external", MappingProxyType(dict(self._external)))

    @property
    def binding_sources(self) -> Mapping[Path, str]:
        return MappingProxyType(
            {path: digest for path, (_name, digest) in self._sources.items()}
        )

    @property
    def binding_record(self) -> Mapping[str, Any]:
        return {
            "schema": 1,
            "backend": self.name,
            "request": {
                "assembly_identity": canonical_digest(self._planning.as_dict()),
                "library": self._planning.library,
                "action": self._action,
                "testbench": (
                    None if self._selected is None else self._selected.cell
                ),
            },
            "project_sources": [
                {"path": name, "sha256": digest}
                for name, digest in sorted(self._sources.values())
            ],
            "external_sources": [
                {"path": str(path), "sha256": record[0]}
                for path, record in sorted(
                    self._external.items(), key=lambda item: str(item[0])
                )
            ],
        }

    def bind(self, project: Any, step: Step) -> "_BoundOaBackend":
        raise ContractError("OA management Step is already bound")

    def run(self, context: StepContext) -> StepResult:
        from sigilicon.virtuoso.client import get_client
        from sigilicon.workflows.oa_check import check_oa_library
        from sigilicon.workflows.oa_library import (
            attest_oa_testbench,
            rebuild_oa_library,
        )

        _require_bound_sources(context, self._sources)
        _require_external_files(self._external)
        timeout = _positive_integer(self._config(context.step), "timeout_seconds")
        client = get_client()
        if self._action == "check":
            from sigilicon.virtuoso.workspace import (
                OperationPolicy,
                workspace_operation,
            )

            with workspace_operation(
                client,
                self._planning.source.project.workspace_root,
                "check-oa-library",
                policy=OperationPolicy.READ_ONLY,
                acquire_flow_lock=False,
                record_incident=False,
                operation_id=context.operation_id,
            ) as operation:
                context.bind_workspace_operation(operation)
                payload = check_oa_library(
                    self._planning.source.manifest_path,
                    project=self._planning.source.project,
                    library=None,
                    client=client,
                    timeout=timeout,
                    plan=self._planning,
                    operation=operation,
                )
        elif self._action == "rebuild":
            payload = rebuild_oa_library(
                self._planning,
                client,
                timeout=timeout,
                operation_id=context.operation_id,
                bind_operation=context.bind_workspace_operation,
            )
        else:
            if self._selected is None:
                raise ExecutionError("OA attest binding lost its testbench")
            payload = attest_oa_testbench(
                self._planning,
                self._selected,
                client,
                timeout=timeout,
                operation_id=context.operation_id,
                bind_operation=context.bind_workspace_operation,
            )
        output = context.write_text(
            "oa",
            f"{self._action}.json",
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        )
        passed = bool(payload.get("passed"))
        artifacts = (Artifact("oa", "evidence.cadence-oa", output),)
        facts = {"passed": passed, "action": self._action}
        return (
            StepResult.succeeded(artifacts=artifacts, facts=facts)
            if passed
            else StepResult(
                "failed",
                artifacts,
                facts,
                f"OA {self._action} did not pass",
            )
        )


class LayoutBackend:
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
        return _capability_checks(resources, _OA_CAPABILITIES)

    def bind(self, project: Any, step: Step) -> "_BoundLayoutBackend":
        from sigilicon.workflows.layout_generation import plan_layout_spec

        self.preflight(step, Resources())
        config = _strict_config(step, self._fields)
        owner = _text(config, "owner")
        spec = project.owner(owner).root / _relative(
            _text(config, "spec"), "layout spec"
        )
        planning = plan_layout_spec(spec, project=project)
        sources = _bind_source_paths(
            project,
            owner,
            step,
            planning.source_records,
        )
        external = _external_file_records(project, planning.source_records)
        return _BoundLayoutBackend(planning, sources, external)

    def run(self, context: StepContext) -> StepResult:
        raise ExecutionError("layout Step was not bound by Project.plan")

    def _execute(self, context: StepContext, planning: Any) -> StepResult:
        from sigilicon.virtuoso.client import get_client
        from sigilicon.workflows.layout_generation import generate_layout
        from sigilicon.workflows.run_artifacts import RunArtifacts

        config = _strict_config(context.step, self._fields)
        owner = _text(config, "owner")
        uncertainty: list[str] = []
        try:
            with owned_scratch_directory(
                prefix=f"sigilicon-layout-{context.run_id}-",
                retain_on_error=lambda exc: bool(uncertainty)
                or process_group_cleanup_uncertainty(exc) is not None,
            ) as scratch:
                artifacts = RunArtifacts.from_step_context(
                    context,
                    "layout",
                    {"owner": owner, "spec": str(config["spec"])},
                    tool_work_root=scratch.path,
                )
                result = generate_layout(
                    planning,
                    get_client(),
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


@dataclass(frozen=True)
class _BoundLayoutBackend(LayoutBackend):
    _planning: Any
    _sources: Mapping[Path, tuple[str, str]]
    _external: Mapping[Path, tuple[str, str]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "_sources", MappingProxyType(dict(self._sources)))
        object.__setattr__(self, "_external", MappingProxyType(dict(self._external)))

    @property
    def binding_sources(self) -> Mapping[Path, str]:
        return MappingProxyType(
            {path: digest for path, (_name, digest) in self._sources.items()}
        )

    @property
    def binding_record(self) -> Mapping[str, Any]:
        return {
            "schema": 1,
            "backend": self.name,
            "request": json.loads(self._planning.plan.canonical_json()),
            "project_sources": [
                {"path": name, "sha256": digest}
                for name, digest in sorted(self._sources.values())
            ],
            "external_sources": [
                {"path": str(path), "sha256": record[0]}
                for path, record in sorted(
                    self._external.items(), key=lambda item: str(item[0])
                )
            ],
        }

    def bind(self, project: Any, step: Step) -> "_BoundLayoutBackend":
        raise ContractError("layout Step is already bound")

    def run(self, context: StepContext) -> StepResult:
        _require_bound_sources(context, self._sources)
        _require_external_files(self._external)
        return self._execute(context, self._planning)


class LayoutVerificationBackend:
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
            _executable_check(resources, _XSTREAM),
            _executable_check(resources, _CALIBRE),
            *_capability_checks(resources, _LAYOUT_VERIFICATION_CAPABILITIES),
        )

    def bind(
        self,
        project: Any,
        step: Step,
    ) -> "_BoundLayoutVerificationBackend":
        from sigilicon.workflows.layout_generation import plan_layout_spec

        self.preflight(step, Resources())
        config = _strict_config(step, self._fields)
        owner = _text(config, "owner")
        spec = project.owner(owner).root / _relative(
            _text(config, "spec"), "layout spec"
        )
        planning = plan_layout_spec(spec, project=project)
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
        )
        return _BoundLayoutVerificationBackend(
            planning,
            _text(config, "check"),
            sources,
            external,
        )

    def run(self, context: StepContext) -> StepResult:
        raise ExecutionError("layout verification Step was not bound by Project.plan")

    def _execute(
        self,
        context: StepContext,
        planning: Any,
        external_sources: Mapping[Path, str],
    ) -> StepResult:
        from sigilicon.virtuoso.client import get_client
        from sigilicon.workflows.layout_verification import run_layout_verification
        from sigilicon.workflows.run_artifacts import RunArtifacts

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
                artifacts = RunArtifacts.from_step_context(
                    context,
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
                    get_client(),
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


@dataclass(frozen=True)
class _BoundLayoutVerificationBackend(LayoutVerificationBackend):
    _planning: Any
    _check: str
    _sources: Mapping[Path, tuple[str, str]]
    _external: Mapping[Path, tuple[str, str]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "_sources", MappingProxyType(dict(self._sources)))
        object.__setattr__(self, "_external", MappingProxyType(dict(self._external)))

    @property
    def binding_sources(self) -> Mapping[Path, str]:
        project_root = self._planning.spec.project_root.resolve()
        sources = {
            path: digest for path, (_name, digest) in self._sources.items()
        }
        sources.update(
            {
                path: record[0]
                for path, record in self._external.items()
                if path.is_relative_to(project_root)
            }
        )
        return MappingProxyType(sources)

    @property
    def binding_record(self) -> Mapping[str, Any]:
        return {
            "schema": 1,
            "backend": self.name,
            "request": {
                "layout": json.loads(self._planning.plan.canonical_json()),
                "check": self._check,
            },
            "project_sources": [
                {"path": name, "sha256": digest}
                for name, digest in sorted(self._sources.values())
            ],
            "external_sources": [
                {"path": str(path), "sha256": record[0]}
                for path, record in sorted(
                    self._external.items(), key=lambda item: str(item[0])
                )
            ],
        }

    def bind(
        self,
        project: Any,
        step: Step,
    ) -> "_BoundLayoutVerificationBackend":
        raise ContractError("layout verification Step is already bound")

    def run(self, context: StepContext) -> StepResult:
        _require_bound_sources(context, self._sources)
        _require_external_files(self._external)
        return self._execute(
            context,
            self._planning,
            {path: record[1] for path, record in self._external.items()},
        )


def cadence_backends() -> tuple[
    XceliumBackend,
    XceliumAmsBackend,
    NativeOaBackend,
    OaBackend,
    LayoutBackend,
    LayoutVerificationBackend,
]:
    return (
        XceliumBackend(),
        XceliumAmsBackend(),
        NativeOaBackend(),
        OaBackend(),
        LayoutBackend(),
        LayoutVerificationBackend(),
    )


__all__ = [
    "LayoutBackend",
    "LayoutVerificationBackend",
    "NativeOaBackend",
    "OaBackend",
    "XceliumBackend",
    "XceliumAmsBackend",
    "cadence_backends",
]
