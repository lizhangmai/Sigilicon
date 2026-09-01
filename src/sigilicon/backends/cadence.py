"""Trusted Cadence backends for direct RTL, native OA, and layout execution."""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import shutil
from typing import Any, Mapping

from sigilicon.execution.model import (
    Artifact,
    ContractError,
    ExecutionError,
    PreflightCheck,
    Resources,
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


def _copy_isolated_project(context: StepContext, owner: str) -> tuple[Path, str]:
    """Build a source-only project view while keeping the real OA workspace."""

    if (
        context.project_root is None
        or context.owner_root is None
        or context.workspace_root is None
    ):
        raise ExecutionError("Cadence owner backend requires project identity")
    owner_path = context.owner_root.relative_to(context.project_root).as_posix()
    root = context.work_root / "project"
    root.mkdir()
    for source in context.step.sources:
        scope = context.source_scopes.get(source)
        if scope == "owner":
            destination = root / owner_path / source
        elif scope == "project":
            destination = root / source
        else:
            raise ExecutionError(f"Cadence source has no declared scope: {source}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(context.source_path(source), destination)
    manifest = root / "sigilicon.toml"
    manifest.write_text(
        "schema = 1\n"
        "contract_kind = \"sigilicon-project\"\n"
        "path_scope = \"repository\"\n"
        "owner = \"repository\"\n\n"
        "[catalogs]\n"
        "ip = \"ip/catalog.toml\"\n"
        "platform = \"configs/platform/catalog.toml\"\n\n"
        "[paths]\n"
        "project_root = \".\"\n"
        f"workspace_root = {json.dumps(str(context.workspace_root))}\n"
        f"artifact_root = {json.dumps(str(context.output_root))}\n",
        encoding="utf-8",
    )
    catalog = root / "ip/catalog.toml"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    component = f"{owner_path}/configs/ip.toml"
    catalog.write_text(
        "schema = 1\n"
        "contract_kind = \"ip-catalog\"\n"
        "path_scope = \"repository\"\n"
        "owner = \"repository\"\n\n"
        "[targets]\n\n"
        f"[components.{owner}]\n"
        f"contract = {json.dumps(component)}\n"
        f"root = {json.dumps(owner_path)}\n",
        encoding="utf-8",
    )
    return root, owner_path


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

    def run(self, context: StepContext) -> StepResult:
        from sigilicon.domain.repository import Project
        from sigilicon.virtuoso.client import get_client
        from sigilicon.workflows.oa_library import oa_plan_source_paths
        from sigilicon.workflows.oa_simulation import execute_oa_maestro_testbench
        from sigilicon.workflows.project_oa import ProjectOaWorkflow
        from sigilicon.workflows.run_artifacts import RunArtifacts

        config = _strict_config(context.step, self._fields)
        owner = _text(config, "owner")
        project_root, owner_path = _copy_isolated_project(context, owner)
        project = Project.from_project_root(project_root)
        if project.owner(owner).root.resolve() != project_root / owner_path:
            raise ExecutionError("native OA owner identity drift")
        plan = ProjectOaWorkflow(project, owner).plan()
        available = {step.cell: step for step in plan.testbenches}
        testbench = _text(config, "testbench")
        try:
            selected = available[testbench]
        except KeyError as exc:
            raise ExecutionError(f"unknown native OA testbench: {testbench}") from exc
        declared = {
            (scope, name)
            for name, scope in context.source_scopes.items()
        }
        required: set[tuple[str, str]] = set()
        for path in oa_plan_source_paths(plan):
            if not path.is_relative_to(project_root):
                continue
            relative = path.relative_to(project_root).as_posix()
            prefix = f"{owner_path}/"
            required.add(
                ("owner", relative.removeprefix(prefix))
                if relative.startswith(prefix)
                else ("project", relative)
            )
        missing = required - declared
        if missing:
            raise ExecutionError(
                f"native OA source closure is incomplete: {sorted(missing)}"
            )
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

    def run(self, context: StepContext) -> StepResult:
        from sigilicon.domain.repository import Project
        from sigilicon.virtuoso.client import get_client
        from sigilicon.workflows.layout_generation import generate_layout, plan_layout_spec
        from sigilicon.workflows.run_artifacts import RunArtifacts

        config = _strict_config(context.step, self._fields)
        owner = _text(config, "owner")
        project_root, owner_path = _copy_isolated_project(context, owner)
        project = Project.from_project_root(project_root)
        if project.owner(owner).root.resolve() != project_root / owner_path:
            raise ExecutionError("layout owner identity drift")
        spec = project_root / owner_path / _relative(_text(config, "spec"), "layout spec")
        planning = plan_layout_spec(spec, project=project)
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

    def run(self, context: StepContext) -> StepResult:
        from sigilicon.domain.repository import Project
        from sigilicon.virtuoso.client import get_client
        from sigilicon.workflows.layout_generation import plan_layout_spec
        from sigilicon.workflows.layout_verification import run_layout_verification
        from sigilicon.workflows.run_artifacts import RunArtifacts

        config = _strict_config(context.step, self._fields)
        owner = _text(config, "owner")
        project_root, owner_path = _copy_isolated_project(context, owner)
        project = Project.from_project_root(project_root)
        if project.owner(owner).root.resolve() != project_root / owner_path:
            raise ExecutionError("layout verification owner identity drift")
        spec = (
            project_root
            / owner_path
            / _relative(_text(config, "spec"), "layout spec")
        )
        planning = plan_layout_spec(spec, project=project)
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
    NativeOaBackend,
    LayoutBackend,
    LayoutVerificationBackend,
]:
    return (
        XceliumBackend(),
        NativeOaBackend(),
        LayoutBackend(),
        LayoutVerificationBackend(),
    )


__all__ = [
    "LayoutBackend",
    "LayoutVerificationBackend",
    "NativeOaBackend",
    "XceliumBackend",
    "cadence_backends",
]
