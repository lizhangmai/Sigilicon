"""Trusted Cadence backends for direct RTL, native OA, and layout execution."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
from types import MappingProxyType
from typing import Any, Mapping

from sigilicon.artifacts import read_nofollow_text
from sigilicon.canonical import canonical_digest
from sigilicon.execution.backend import _Preparation
from sigilicon.execution.model import (
    Artifact,
    ContractError,
    ExecutionError,
    OperationStep,
    PreflightCheck,
    Resources,
    Source,
    PreparedStep,
    StepContext,
    StepResult,
    json_value,
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


def _strict_config(step: PreparedStep, fields: frozenset[str]) -> Mapping[str, Any]:
    request = step.request
    if set(request) == {"config", "prepared", "external"}:
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


def _direct_preparation(backend: Any, step: OperationStep) -> _Preparation:
    prepared = PreparedStep.from_operation(step)
    backend.preflight(prepared, Resources())
    return _Preparation(prepared)


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
    step: PreparedStep,
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
    external: Mapping[Path, tuple[str, str]],
) -> Mapping[str, Any]:
    return {
        "config": dict(config),
        "prepared": dict(prepared),
        "external": [
            {"path": str(path), "sha256": digest, "text": snapshot}
            for path, (digest, snapshot) in sorted(
                external.items(), key=lambda item: str(item[0])
            )
        ],
    }


def _portable_external(step: PreparedStep) -> Mapping[Path, tuple[str, str]]:
    raw = step.request.get("external")
    if not isinstance(raw, tuple):
        raise ExecutionError("Cadence request has no prepared external source set")
    selected: dict[Path, tuple[str, str]] = {}
    for entry in raw:
        if not isinstance(entry, Mapping) or set(entry) != {"path", "sha256", "text"}:
            raise ExecutionError("Cadence request contains an invalid external source")
        path_value = entry["path"]
        digest = entry["sha256"]
        snapshot = entry["text"]
        if not all(isinstance(value, str) for value in (path_value, digest, snapshot)):
            raise ExecutionError("Cadence external source fields must be text")
        path = Path(path_value).absolute()
        if path != path.resolve() or path in selected:
            raise ExecutionError("Cadence external source path is unsafe or duplicated")
        if hashlib.sha256(snapshot.encode("utf-8")).hexdigest() != digest:
            raise ExecutionError("Cadence external source digest disagrees with its snapshot")
        selected[path] = (digest, snapshot)
    return MappingProxyType(selected)


class XceliumBackend:
    """Execute one explicit, source-closed Verilog/SystemVerilog testbench."""

    name = "cadence.xcelium"
    _fields = frozenset({"success_marker", "timeout_seconds"})

    @staticmethod
    def _hdl_sources(step: PreparedStep) -> tuple[str, ...]:
        sources = tuple(
            source
            for source in step.sources
            if Path(source).suffix.lower() in {".sv", ".svh", ".v", ".vh"}
        )
        if not sources:
            raise ContractError("Xcelium filesets select no Verilog sources")
        return sources

    def prepare(self, _project: Any, step: OperationStep) -> _Preparation:
        return _direct_preparation(self, step)

    def preflight(self, step: PreparedStep, resources: Resources) -> tuple[PreflightCheck, ...]:
        config = _strict_config(step, self._fields)
        for source in self._hdl_sources(step):
            _relative(source, "HDL fileset source")
        _text(config, "success_marker")
        _positive_integer(config, "timeout_seconds")
        return (
            _executable_check(resources, _XRUN),
            *_capability_checks(resources, frozenset({"tool.cadence-xcelium"})),
        )

    def run(self, context: StepContext, step: PreparedStep) -> StepResult:
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

    def preflight(self, step: PreparedStep, resources: Resources) -> tuple[PreflightCheck, ...]:
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

    def prepare(self, project: Any, step: OperationStep) -> _Preparation:
        from sigilicon.workflows.xcelium_ams import plan_xcelium_ams_cell

        initial = PreparedStep.from_operation(step)
        self.preflight(initial, Resources())
        config = _strict_config(initial, self._fields)
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
        if required != frozenset(planning.source_records):
            raise ContractError("Xcelium AMS plan source snapshot is incomplete")
        bindings = _bind_source_paths(
            project,
            owner,
            step,
            planning.source_records,
        )
        external = _external_file_records(project, planning.source_records)
        captured = _captured_project_sources(project, owner, bindings)
        prepared = PreparedStep.from_operation(
            step,
            request=_portable_request(config, planning.as_dict(), external),
            sources=tuple(dict.fromkeys((*step.sources, *(s.path for s in captured)))),
        )
        self.preflight(prepared, Resources())
        return _Preparation(prepared, captured)

    def run(self, context: StepContext, step: PreparedStep) -> StepResult:
        context.require_step(step)
        from sigilicon.project import Project
        from sigilicon.workflows.xcelium_ams import (
            execute_xcelium_ams_cell,
            plan_xcelium_ams_cell,
        )

        config = _strict_config(step, self._fields)
        owner = _text(config, "owner")
        if context.project_root is None or context.owner_root is None:
            raise ExecutionError("Xcelium AMS requires Project runtime roots")
        project = Project.open(context.project_root)
        planning = plan_xcelium_ams_cell(
            context.owner_root / _relative(_text(config, "cell"), "verification cell"),
            project=project,
        )
        expected = step.request.get("prepared")
        if not isinstance(expected, Mapping) or canonical_digest(
            planning.as_dict()
        ) != canonical_digest(json_value(expected)):
            raise ExecutionError("Xcelium AMS preparation identity drift")
        sources = _bind_source_paths(project, owner, step, planning.source_records)
        external = _portable_external(step)
        _require_bound_sources(context, sources)
        _require_external_files(external)
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
            artifacts = context.files(
                "xcelium-ams",
                {
                    "owner": owner,
                    "cell": str(config["cell"]),
                },
                tool_work_root=scratch.path,
            )
            bound_sources = {
                path: context.source_path(name)
                for path, (name, _digest) in sources.items()
            }
            bound_sources.update(
                {
                    path: artifacts.write_text(
                        "inputs",
                        ("external", f"{index:03d}-{path.name}"),
                        snapshot,
                    )
                    for index, (path, (_digest, snapshot)) in enumerate(
                        sorted(external.items(), key=lambda item: str(item[0]))
                    )
                }
            )
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


class NativeOaBackend:
    """Run one source-owned native Maestro testbench through a bound OA session."""

    name = "cadence.native-oa"
    _fields = frozenset({"owner", "testbench", "timeout_seconds"})

    def preflight(self, step: PreparedStep, resources: Resources) -> tuple[PreflightCheck, ...]:
        config = _strict_config(step, self._fields)
        _text(config, "owner")
        _text(config, "testbench")
        _positive_integer(config, "timeout_seconds")
        if "configs/oa.toml" not in step.sources:
            raise ContractError("native OA step must close over configs/oa.toml")
        return _capability_checks(resources, _OA_CAPABILITIES)

    def prepare(self, project: Any, step: OperationStep) -> _Preparation:
        from sigilicon.workflows.oa_library import (
            oa_plan_source_paths,
            plan_oa_library_rebuild,
        )

        initial = PreparedStep.from_operation(step)
        self.preflight(initial, Resources())
        config = _strict_config(initial, self._fields)
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
        captured = _captured_project_sources(project, owner, sources)
        prepared = PreparedStep.from_operation(
            step,
            request=_portable_request(
                config,
                {
                    "assembly_identity": canonical_digest(planning.as_dict()),
                    "library": planning.library,
                    "testbench": matches[0].cell,
                },
                external,
            ),
            sources=tuple(dict.fromkeys((*step.sources, *(s.path for s in captured)))),
        )
        self.preflight(prepared, Resources())
        return _Preparation(prepared, captured)

    def run(self, context: StepContext, step: PreparedStep) -> StepResult:
        context.require_step(step)
        from sigilicon.virtuoso.client import get_client
        from sigilicon.project import Project
        from sigilicon.workflows.oa_library import (
            oa_plan_source_paths,
            plan_oa_library_rebuild,
        )
        from sigilicon.workflows.oa_simulation import execute_oa_maestro_testbench

        config = _strict_config(step, self._fields)
        owner = _text(config, "owner")
        testbench = _text(config, "testbench")
        if context.project_root is None or context.owner_root is None:
            raise ExecutionError("native OA requires Project runtime roots")
        project = Project.open(context.project_root)
        manifest = project.oa_assembly_for(context.owner_root)
        if manifest is None:
            raise ExecutionError(f"owner {owner!r} has no OA assembly")
        plan = plan_oa_library_rebuild(manifest, project=project)
        matches = tuple(item for item in plan.testbenches if item.cell == testbench)
        if len(matches) != 1:
            raise ExecutionError(f"prepared native OA testbench disappeared: {testbench}")
        selected = matches[0]
        expected = step.request.get("prepared")
        actual = {
            "assembly_identity": canonical_digest(plan.as_dict()),
            "library": plan.library,
            "testbench": selected.cell,
        }
        if not isinstance(expected, Mapping) or canonical_digest(json_value(expected)) != canonical_digest(actual):
            raise ExecutionError("native OA preparation identity drift")
        required = _validate_oa_plan_sources(
            project, owner, plan, oa_plan_source_paths(plan)
        )
        sources = _bind_source_paths(project, owner, step, required)
        external = _portable_external(step)
        _require_bound_sources(context, sources)
        _require_external_files(external)
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


class _OaBackend:
    """Execute one fixed native-OA operation against a plan-bound assembly."""

    _base_fields = frozenset({"owner", "timeout_seconds"})

    def __init__(self, operation: str) -> None:
        if operation not in {"check", "rebuild", "attest"}:
            raise ValueError(f"unsupported OA operation: {operation}")
        self.operation = operation
        self.name = f"cadence.oa-{operation}"

    def _config(self, step: PreparedStep) -> Mapping[str, Any]:
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

    def preflight(self, step: PreparedStep, resources: Resources) -> tuple[PreflightCheck, ...]:
        self._config(step)
        return _capability_checks(resources, _OA_CAPABILITIES)

    def prepare(self, project: Any, step: OperationStep) -> _Preparation:
        from sigilicon.workflows.oa_library import (
            oa_plan_source_paths,
            plan_oa_library_rebuild,
        )

        initial = PreparedStep.from_operation(step)
        self.preflight(initial, Resources())
        config = self._config(initial)
        owner = _text(config, "owner")
        manifest = project.oa_assembly_for(project.owner(owner).root)
        if manifest is None:
            raise ContractError(f"owner {owner!r} has no OA assembly")
        planning = plan_oa_library_rebuild(manifest, project=project)
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
        )
        captured = _captured_project_sources(project, owner, sources)
        prepared = PreparedStep.from_operation(
            step,
            request=_portable_request(
                config,
                {
                    "assembly_identity": canonical_digest(planning.as_dict()),
                    "library": planning.library,
                    "operation": self.operation,
                    "testbench": None if selected is None else selected.cell,
                },
                external,
            ),
            sources=tuple(dict.fromkeys((*step.sources, *(s.path for s in captured)))),
        )
        self.preflight(prepared, Resources())
        return _Preparation(prepared, captured)

    def run(self, context: StepContext, step: PreparedStep) -> StepResult:
        context.require_step(step)
        from sigilicon.virtuoso.client import get_client
        from sigilicon.project import Project
        from sigilicon.workflows.oa_check import check_oa_library
        from sigilicon.workflows.oa_library import (
            attest_oa_testbench,
            oa_plan_source_paths,
            plan_oa_library_rebuild,
            rebuild_oa_library,
        )

        config = self._config(step)
        owner = _text(config, "owner")
        if context.project_root is None or context.owner_root is None or context.workspace_root is None:
            raise ExecutionError("OA management requires Project runtime roots")
        project = Project.open(context.project_root)
        manifest = project.oa_assembly_for(context.owner_root)
        if manifest is None:
            raise ExecutionError(f"owner {owner!r} has no OA assembly")
        planning = plan_oa_library_rebuild(manifest, project=project)
        selected = None
        if self.operation == "attest":
            testbench = _text(config, "testbench")
            matches = tuple(item for item in planning.testbenches if item.cell == testbench)
            if len(matches) != 1:
                raise ExecutionError(f"prepared OA testbench disappeared: {testbench}")
            selected = matches[0]
        expected = step.request.get("prepared")
        actual = {
            "assembly_identity": canonical_digest(planning.as_dict()),
            "library": planning.library,
            "operation": self.operation,
            "testbench": None if selected is None else selected.cell,
        }
        if not isinstance(expected, Mapping) or canonical_digest(json_value(expected)) != canonical_digest(actual):
            raise ExecutionError("OA preparation identity drift")
        required = _validate_oa_plan_sources(
            project, owner, planning, oa_plan_source_paths(planning)
        )
        sources = _bind_source_paths(project, owner, step, required)
        external = _portable_external(step)
        _require_bound_sources(context, sources)
        _require_external_files(external)
        timeout = _positive_integer(config, "timeout_seconds")
        client = get_client()
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
                    planning.source.manifest_path,
                    project=project,
                    library=None,
                    client=client,
                    timeout=timeout,
                    plan=planning,
                    operation=operation,
                )
        elif self.operation == "rebuild":
            payload = rebuild_oa_library(
                planning,
                client,
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


class LayoutBackend:
    """Generate one source-authored layout through a bound OA mutation lease."""

    name = "cadence.layout"
    _fields = frozenset({"owner", "spec", "timeout_seconds"})

    def preflight(self, step: PreparedStep, resources: Resources) -> tuple[PreflightCheck, ...]:
        config = _strict_config(step, self._fields)
        _text(config, "owner")
        spec = _relative(_text(config, "spec"), "layout spec")
        if spec not in step.sources:
            raise ContractError("layout spec must be inside the operation source closure")
        _positive_integer(config, "timeout_seconds")
        return _capability_checks(resources, _OA_CAPABILITIES)

    def prepare(self, project: Any, step: OperationStep) -> _Preparation:
        from sigilicon.workflows.layout_generation import plan_layout_spec

        initial = PreparedStep.from_operation(step)
        self.preflight(initial, Resources())
        config = _strict_config(initial, self._fields)
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
        captured = _captured_project_sources(project, owner, sources)
        prepared = PreparedStep.from_operation(
            step,
            request=_portable_request(
                config,
                json.loads(planning.plan.canonical_json()),
                external,
            ),
            sources=tuple(dict.fromkeys((*step.sources, *(s.path for s in captured)))),
        )
        self.preflight(prepared, Resources())
        return _Preparation(prepared, captured)

    def run(self, context: StepContext, step: PreparedStep) -> StepResult:
        context.require_step(step)
        from sigilicon.project import Project
        from sigilicon.workflows.layout_generation import plan_layout_spec

        config = _strict_config(step, self._fields)
        owner = _text(config, "owner")
        if context.project_root is None or context.owner_root is None:
            raise ExecutionError("layout generation requires Project runtime roots")
        project = Project.open(context.project_root)
        planning = plan_layout_spec(
            context.owner_root / _relative(_text(config, "spec"), "layout spec"),
            project=project,
        )
        expected = step.request.get("prepared")
        actual = json.loads(planning.plan.canonical_json())
        if not isinstance(expected, Mapping) or canonical_digest(json_value(expected)) != canonical_digest(actual):
            raise ExecutionError("layout preparation identity drift")
        sources = _bind_source_paths(project, owner, step, planning.source_records)
        external = _portable_external(step)
        _require_bound_sources(context, sources)
        _require_external_files(external)
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

    def preflight(self, step: PreparedStep, resources: Resources) -> tuple[PreflightCheck, ...]:
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

    def prepare(
        self,
        project: Any,
        step: OperationStep,
    ) -> _Preparation:
        from sigilicon.workflows.layout_generation import plan_layout_spec

        initial = PreparedStep.from_operation(step)
        self.preflight(initial, Resources())
        config = _strict_config(initial, self._fields)
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
        check = _text(config, "check")
        captured = _captured_project_sources(project, owner, sources)
        prepared = PreparedStep.from_operation(
            step,
            request=_portable_request(
                config,
                {
                    "layout": json.loads(planning.plan.canonical_json()),
                    "check": check,
                },
                external,
            ),
            sources=tuple(dict.fromkeys((*step.sources, *(s.path for s in captured)))),
        )
        self.preflight(prepared, Resources())
        return _Preparation(prepared, captured)

    def run(self, context: StepContext, step: PreparedStep) -> StepResult:
        context.require_step(step)
        from sigilicon.project import Project
        from sigilicon.workflows.layout_generation import plan_layout_spec

        config = _strict_config(step, self._fields)
        owner = _text(config, "owner")
        check = _text(config, "check")
        if context.project_root is None or context.owner_root is None:
            raise ExecutionError("layout verification requires Project runtime roots")
        project = Project.open(context.project_root)
        planning = plan_layout_spec(
            context.owner_root / _relative(_text(config, "spec"), "layout spec"),
            project=project,
        )
        expected = step.request.get("prepared")
        actual = {
            "layout": json.loads(planning.plan.canonical_json()),
            "check": check,
        }
        if not isinstance(expected, Mapping) or canonical_digest(json_value(expected)) != canonical_digest(actual):
            raise ExecutionError("layout verification preparation identity drift")
        sources = _bind_source_paths(project, owner, step, planning.source_records)
        external = _portable_external(step)
        _require_bound_sources(context, sources)
        _require_external_files(external)
        return self._execute(
            context,
            planning,
            {path: snapshot for path, (_digest, snapshot) in external.items()},
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
