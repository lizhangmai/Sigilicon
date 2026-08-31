"""Plan and execute one direct, source-bound design Action.

Design execution is intentionally a small seam:

* :func:`plan_design_action` resolves the owner-authored execution recipe
  node, validates its source boundary, and snapshots the exact runner/spec
  closure.
* :class:`DesignTargetAdapter` consumes that typed plan.  It never discovers
  a catalog or reconstructs a target from persisted configuration.

The class name is kept for the registered Adapter seam, but it no longer
depends on a target registry or a mode registry.
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
from typing import Any, Mapping

from sigilicon.domain.repository import Project, RepositoryOwner
from sigilicon.external_tools import (
    cadence_subprocess_env,
    owned_input_file,
    owned_sealed_input,
    run_process_group_capture,
)
from sigilicon.flow import (
    ActionPlan,
    ActionContext,
    AdapterResult,
    CollectedActionResult,
    FlowExecutionError,
    ProducedArtifact,
    SourceMember,
    FlowNode,
)
from sigilicon.flow.circuit_design import (
    DESIGN_ACTION_PLAN,
    DESIGN_ELECTRICAL_DIAGNOSTIC_ADAPTER,
    DESIGN_ELECTRICAL_DIAGNOSTIC_ACTION,
    DESIGN_SOURCE_CHECK_ADAPTER,
    DESIGN_SOURCE_CHECK_ACTION,
)
from sigilicon.flow.registry import FlowRegistry
from sigilicon.flow.evidence import FactSet, FactSource
from sigilicon.flow.model import EVIDENCE_LEVELS, EVIDENCE_ROLES
from sigilicon.flow.serialization import json_value
from sigilicon.flow.source_assets import (
    snapshot_source_member,
    source_member_matches,
)
from sigilicon.workflows.run_artifacts import (
    FlowRunArtifacts,
    managed_run_artifact_environment,
)
from sigilicon.workflows.source_control import artifact_source_state


_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
_MODULE_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z"
)
_SHARED_MODULES = frozenset({"sigilicon.cli.design_lifecycle"})
_KINDS = frozenset({"script", "module"})
_SPEC_ARGUMENTS = frozenset({"--spec", "--design"})
_ROUTING_ARGUMENTS = frozenset({"--spec", "--design", "--mode"})
_PLAN_FIELDS = frozenset(
    {
        "target",
        "mode",
        "kind",
        "entrypoint",
        "spec_argument",
        "spec",
        "default_args",
        "evidence_role",
        "evidence_level",
        "evidence_scope",
    }
)
_REQUIRED_PLAN_FIELDS = frozenset(
    {
        "target",
        "mode",
        "kind",
        "entrypoint",
        "evidence_role",
        "evidence_level",
        "evidence_scope",
    }
)
_BOUND_RUNNER_BOOTSTRAP = (
    "from sigilicon.workflows.design_runner import main;main()"
)


def _fact_set(
    context: ActionContext,
    values: Mapping[str, object],
) -> FactSet:
    """Project one validated design observation through the Action schema."""

    schema = context.action.fact_schema
    if schema is None:
        raise FlowExecutionError(f"Action {context.node_id!r} has no fact schema")
    return FactSet(
        schema,
        values,
        FactSource(context.action.kind, context.node_id),
    )


def _planned_product_conclusion(context: ActionContext) -> bool:
    """Project only plan authority; a runner result cannot mint qualification."""

    context.require_evidence()
    return False


@dataclass(frozen=True)
class DesignInvocation:
    """Domain payload executed from the common source-bound ActionPlan envelope."""

    owner: str
    project_root: Path
    target: str
    mode: str
    kind: str
    entrypoint: str
    entrypoint_path: Path
    spec_argument: str | None
    spec: Path | None
    spec_relative: PurePosixPath | None
    default_args: tuple[str, ...]
    evidence_role: str
    evidence_level: str
    evidence_scope: str

    def __post_init__(self) -> None:
        _name(self.target, "design Action target")
        _name(self.mode, "design Action mode")
        if not isinstance(self.owner, str) or not self.owner:
            raise ValueError("design Action owner must be non-empty text")
        if not isinstance(self.kind, str) or self.kind not in _KINDS:
            raise ValueError(
                f"design Action kind must be one of {sorted(_KINDS)}"
            )

        project_root = Path(self.project_root).resolve()
        entrypoint_path = Path(self.entrypoint_path).resolve()
        if not isinstance(self.entrypoint, str) or not self.entrypoint:
            raise ValueError("design Action entrypoint must be non-empty text")
        if self.kind == "script":
            _canonical_relative(self.entrypoint, "design Action entrypoint")
            if entrypoint_path.suffix != ".py":
                raise ValueError("script entrypoint must be a Python script")
        elif _MODULE_RE.fullmatch(self.entrypoint) is None:
            raise ValueError("module entrypoint must name a Python module")

        if self.spec_argument is None:
            if self.spec is not None or self.spec_relative is not None:
                raise ValueError(
                    "design Action spec and spec_argument must be configured together"
                )
        else:
            if (
                not isinstance(self.spec_argument, str)
                or self.spec_argument not in _SPEC_ARGUMENTS
            ):
                raise ValueError(
                    f"design Action spec_argument must be one of "
                    f"{sorted(_SPEC_ARGUMENTS)}"
                )
            if self.spec is None or self.spec_relative is None:
                raise ValueError(
                    "design Action spec and spec_argument must be configured together"
                )
            if not isinstance(self.spec_relative, PurePosixPath):
                object.__setattr__(
                    self,
                    "spec_relative",
                    PurePosixPath(self.spec_relative),
                )
        if self.spec is not None:
            spec = Path(self.spec).resolve()
            if spec == entrypoint_path:
                raise ValueError("design Action runner and spec must be distinct files")
            object.__setattr__(self, "spec", spec)

        default_args = tuple(self.default_args)
        _validate_runner_args(default_args, "design Action default_args")
        object.__setattr__(self, "default_args", default_args)
        _evidence(self.evidence_role, self.evidence_level, self.evidence_scope)

        object.__setattr__(self, "project_root", project_root)
        object.__setattr__(self, "entrypoint_path", entrypoint_path)

    def as_dict(self) -> dict[str, object]:
        """Return the portable direct node configuration projection."""

        return {
            "target": self.target,
            "mode": self.mode,
            "kind": self.kind,
            "entrypoint": self.entrypoint,
            "spec_argument": self.spec_argument,
            "spec": (
                None
                if self.spec_relative is None
                else self.spec_relative.as_posix()
            ),
            "default_args": list(self.default_args),
            "evidence_role": self.evidence_role,
            "evidence_level": self.evidence_level,
            "evidence_scope": self.evidence_scope,
        }

    def bound_command(
        self,
        *,
        runner_path: str | Path,
        spec_path: str | Path | None,
        extra_args: tuple[str, ...] = (),
    ) -> tuple[str, ...]:
        """Execute this plan through already-held immutable runner/spec files."""

        if not isinstance(runner_path, (str, Path)) or not str(runner_path):
            raise ValueError("bound design runner path must be non-empty")
        extra_args = tuple(extra_args)
        _validate_runner_args(extra_args, "design Action extra_args")
        if self.spec_argument is None:
            if spec_path is not None:
                raise ValueError("spec path provided for a design Action without a spec")
            bound_spec = ("-", "-")
            routing: tuple[str, ...] = ()
        else:
            if spec_path is None or not str(spec_path):
                raise ValueError("bound design Action requires its exact spec descriptor")
            assert self.spec is not None
            bound_spec = (str(spec_path), str(self.spec))
            routing = (self.spec_argument, str(self.spec))
        package = "-" if self.kind == "script" else self.entrypoint.rpartition(".")[0]
        return (
            sys.executable,
            "-c",
            _BOUND_RUNNER_BOOTSTRAP,
            str(runner_path),
            str(self.entrypoint_path),
            package,
            *bound_spec,
            *routing,
            "--mode",
            self.mode,
            *self.default_args,
            *extra_args,
        )


def plan_design_action(
    project: Project,
    owner: RepositoryOwner,
    config: Mapping[str, Any],
) -> ActionPlan:
    """Resolve one execution-recipe design node into a typed source-bound plan.

    ``entrypoint`` and ``spec`` are canonical project-relative paths for
    project-owned sources.  A module entrypoint may additionally be the one
    explicitly shared Sigilicon lifecycle module.  No secondary registry or
    repository scan participates in this operation.
    """

    if not isinstance(project, Project):
        raise ValueError("design Action planning requires an explicit Project")
    if owner not in project.owners:
        raise ValueError(
            f"repository does not contain owner {owner.name!r}"
        )
    if not isinstance(config, Mapping):
        raise ValueError("design Action config must be a mapping")
    unknown = set(config) - _PLAN_FIELDS
    if unknown:
        raise ValueError(
            f"design Action contains unknown configuration: {sorted(unknown)}"
        )
    missing = _REQUIRED_PLAN_FIELDS - set(config)
    if missing:
        raise ValueError(
            f"design Action is missing configuration: {sorted(missing)}"
        )

    target = _name(config["target"], "design Action target")
    mode = _name(config["mode"], "design Action mode")
    kind = config["kind"]
    if not isinstance(kind, str) or kind not in _KINDS:
        raise ValueError(f"design Action kind must be one of {sorted(_KINDS)}")
    entrypoint_value = config["entrypoint"]
    if kind == "script":
        entrypoint_path, entrypoint_relative = _owned_file(
            project,
            owner,
            entrypoint_value,
            "design Action entrypoint",
        )
        if entrypoint_path.suffix != ".py":
            raise ValueError("design Action script entrypoint must be a Python script")
        entrypoint = entrypoint_relative.as_posix()
        entrypoint_root = project.project_root
        entrypoint_scope = "project"
    else:
        if (
            not isinstance(entrypoint_value, str)
            or _MODULE_RE.fullmatch(entrypoint_value) is None
        ):
            raise ValueError("design Action module entrypoint must name a Python module")
        entrypoint = entrypoint_value
        entrypoint_path, entrypoint_root, entrypoint_scope = _module_source(
            project,
            owner,
            entrypoint,
        )

    spec_argument = config.get("spec_argument")
    spec_value = config.get("spec")
    if (spec_argument is None) != (spec_value is None):
        raise ValueError(
            "design Action spec_argument and spec must be configured together"
        )
    spec: Path | None = None
    spec_relative: PurePosixPath | None = None
    if spec_argument is not None:
        if (
            not isinstance(spec_argument, str)
            or spec_argument not in _SPEC_ARGUMENTS
        ):
            raise ValueError(
                f"design Action spec_argument must be one of {sorted(_SPEC_ARGUMENTS)}"
            )
        spec, spec_relative = _owned_file(
            project,
            owner,
            spec_value,
            "design Action spec",
        )

    raw_default_args = config.get("default_args", [])
    if not isinstance(raw_default_args, (list, tuple)):
        raise ValueError("design Action default_args must be a string array")
    default_args = tuple(raw_default_args)
    _validate_runner_args(default_args, "design Action default_args")

    evidence_role = _text(config["evidence_role"], "design Action evidence_role")
    evidence_level = _text(config["evidence_level"], "design Action evidence_level")
    evidence_scope = _text(config["evidence_scope"], "design Action evidence_scope")
    _evidence(evidence_role, evidence_level, evidence_scope)

    source_members = [
        snapshot_source_member(
            entrypoint_path,
            source_root=entrypoint_root,
            scope=entrypoint_scope,
            source_label="design Action entrypoint",
        )
    ]
    if spec is not None:
        source_members.append(
            snapshot_source_member(
                spec,
                source_root=project.project_root,
                scope="project",
                source_label="design Action spec",
            )
        )
    try:
        stable = all(source_member_matches(member) for member in source_members)
    except (OSError, RuntimeError, UnicodeError):
        stable = False
    if not stable:
        raise ValueError("design Action source changed during planning")
    invocation = DesignInvocation(
        owner=owner.name,
        project_root=project.project_root,
        target=target,
        mode=mode,
        kind=kind,
        entrypoint=entrypoint,
        entrypoint_path=entrypoint_path,
        spec_argument=spec_argument,
        spec=spec,
        spec_relative=spec_relative,
        default_args=default_args,
        evidence_role=evidence_role,
        evidence_level=evidence_level,
        evidence_scope=evidence_scope,
    )
    return ActionPlan(
        DESIGN_ACTION_PLAN,
        invocation,
        invocation.as_dict(),
        tuple(source_members),
    )


class DesignTargetAdapter:
    """Execute one typed direct design Action without planning again."""

    def run(self, context: ActionContext) -> AdapterResult:
        selected = context.require_action_plan(
            DESIGN_ACTION_PLAN,
            DesignInvocation,
        )
        assert context.action_plan is not None
        self._validate_context(context, selected)
        evidence = context.require_evidence()
        product_conclusion = _planned_product_conclusion(context)

        route_sources = tuple(context.action_plan.sources)
        expected_source_count = 1 if selected.spec is None else 2
        if len(route_sources) != expected_source_count:
            raise FlowExecutionError("typed design Action Plan source closure drift")
        if not self._sources_match(route_sources):
            raise FlowExecutionError("owner design source changed after Flow planning")

        extra_args: tuple[str, ...] = ()
        environment = os.environ.copy()
        environment.pop("SIGILICON_MANAGED_RUN_ARTIFACTS", None)
        managed_electrical = False
        if context.action.kind == DESIGN_ELECTRICAL_DIAGNOSTIC_ACTION:
            capability = context.capabilities["tool.cadence-spectre"]
            if capability.executable is None:
                raise FlowExecutionError(
                    "Spectre design diagnostic requires an executable capability"
                )
            extra_args = ("--spectre", str(capability.executable))
            environment = cadence_subprocess_env()
            managed_electrical = True
        elif context.action.kind != DESIGN_SOURCE_CHECK_ACTION:
            raise FlowExecutionError(
                f"unsupported project design Action: {context.action.kind}"
            )

        timeout = context.adapter_config.get("timeout_seconds", 600)
        if (
            set(context.adapter_config) != {"timeout_seconds"}
            or isinstance(timeout, bool)
            or not isinstance(timeout, int)
            or timeout <= 0
        ):
            raise FlowExecutionError(
                "design Adapter configuration requires one positive timeout_seconds"
            )

        source = artifact_source_state(selected.project_root)
        source["design_action"] = [
            {
                "path": member.path,
                "sha256": hashlib.sha256(
                    member.record_text.encode("utf-8")
                ).hexdigest(),
                "executable": member.executable,
            }
            for member in route_sources
        ]
        if managed_electrical:
            environment.update(
                managed_run_artifact_environment(context, "evidence", source)
            )
        runner_member = self._member_at(
            route_sources,
            selected.entrypoint_path,
            "entrypoint",
        )
        spec_member = (
            None
            if selected.spec is None
            else self._member_at(route_sources, selected.spec, "spec")
        )
        with ExitStack() as stack:
            runner_source = stack.enter_context(
                owned_input_file(runner_member.location)
            )
            spec_source = (
                None
                if spec_member is None
                else stack.enter_context(owned_input_file(spec_member.location))
            )
            if not self._owned_source_matches(runner_source.fd, runner_member) or (
                spec_member is not None
                and (
                    spec_source is None
                    or not self._owned_source_matches(spec_source.fd, spec_member)
                )
            ):
                raise FlowExecutionError(
                    "owner design source changed while binding runner inputs"
                )
            sealed_runner = stack.enter_context(
                owned_sealed_input(
                    runner_member.record_text.encode("utf-8"),
                    name="design-runner.py",
                )
            )
            sealed_spec = (
                None
                if spec_member is None
                else stack.enter_context(
                    owned_sealed_input(
                        spec_member.record_text.encode("utf-8"),
                        name="design-spec.toml",
                    )
                )
            )
            pass_fds = (sealed_runner.fd,)
            if sealed_spec is not None:
                pass_fds = (*pass_fds, sealed_spec.fd)
            completed = run_process_group_capture(
                selected.bound_command(
                    runner_path=sealed_runner.child_path,
                    spec_path=(
                        None if sealed_spec is None else sealed_spec.child_path
                    ),
                    extra_args=extra_args,
                ),
                cwd=selected.project_root,
                env=environment,
                timeout=timeout,
                pass_fds=pass_fds,
            )
        artifacts = FlowRunArtifacts(context, "evidence", source)
        stdout = artifacts.write_text(
            "logs",
            ("design-runner.stdout.log",),
            completed.stdout,
        )
        stderr = artifacts.write_text(
            "logs",
            ("design-runner.stderr.log",),
            completed.stderr or "",
        )
        if not self._sources_match(route_sources):
            raise FlowExecutionError("owner design source changed during Flow execution")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise FlowExecutionError("design runner did not emit one JSON result") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("passed"), bool):
            raise FlowExecutionError(
                "design runner result must be an object with boolean passed"
            )
        if completed.returncode != 0 and payload["passed"]:
            tail = "\n".join((completed.stderr or completed.stdout).splitlines()[-80:])
            raise FlowExecutionError(
                "design runner reported passed=true but exited "
                f"{completed.returncode}:\n{tail}"
            )
        runner_result = self._runner_result(payload)
        result_payload = {
            "schema": 1,
            "contract_kind": "design-action-evidence",
            "owner": selected.owner,
            "target": selected.target,
            "mode": selected.mode,
            "runner_result": runner_result,
            "process_returncode": completed.returncode,
            "source": source,
            "evidence_role": evidence.role,
            "evidence_level": evidence.level,
            "evidence_scope": evidence.scope,
            "product_qualification_conclusion": product_conclusion,
        }
        evidence_path = context.output_path("evidence", "design-evidence.json")
        evidence_path.write_text(
            json.dumps(result_payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return AdapterResult.succeeded(
            CollectedActionResult(
                artifacts=(
                    ProducedArtifact(
                        "evidence",
                        context.action.output("evidence").kind,
                        evidence_path,
                    ),
                ),
                facts=_fact_set(context, {
                    "passed": payload["passed"],
                    "process-returncode": completed.returncode,
                    "evidence-role": evidence.role,
                    "evidence-level": evidence.level,
                    "evidence-scope": evidence.scope,
                    "product-qualification-conclusion": product_conclusion,
                }),
                evidence=(stdout, stderr),
            )
        )

    @staticmethod
    def _validate_context(
        context: ActionContext,
        selected: DesignInvocation,
    ) -> None:
        assert context.action_plan is not None
        expected = selected.as_dict()
        record = json_value(context.action_plan.record)
        if isinstance(record, dict):
            for field in ("spec_argument", "spec", "default_args"):
                record.setdefault(field, expected[field])
        if record != expected:
            raise FlowExecutionError("typed design Action Plan record drift")
        actual = json_value(context.action_config.values)
        if not isinstance(actual, dict):
            raise FlowExecutionError("design Action configuration is not portable")
        expected = selected.as_dict()
        for field in ("spec_argument", "spec", "default_args"):
            actual.setdefault(field, expected[field])
        if actual != expected:
            raise FlowExecutionError("design Action configuration drift")
        scope = context.project_scope
        if scope is not None and (
            scope.owner != selected.owner
            or scope.project.project_root != selected.project_root
        ):
            raise FlowExecutionError("design Action Plan project owner scope drift")
        evidence = context.require_evidence()
        if (
            evidence.role != selected.evidence_role
            or evidence.level != selected.evidence_level
            or evidence.scope != selected.evidence_scope
        ):
            raise FlowExecutionError("design Action evidence envelope drift")

    @staticmethod
    def _sources_match(sources: tuple[SourceMember, ...]) -> bool:
        try:
            return all(source_member_matches(source) for source in sources)
        except (OSError, RuntimeError, UnicodeError):
            return False

    @staticmethod
    def _owned_source_matches(descriptor: int, source: SourceMember) -> bool:
        metadata = os.fstat(descriptor)
        executable = bool(
            metadata.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        )
        chunks: list[bytes] = []
        offset = 0
        while chunk := os.pread(descriptor, 1024 * 1024, offset):
            chunks.append(chunk)
            offset += len(chunk)
        try:
            record = b"".join(chunks).decode("utf-8")
        except UnicodeError:
            return False
        return record == source.record_text and executable == source.executable

    @staticmethod
    def _runner_result(payload: dict[str, Any]) -> dict[str, Any]:
        """Project untrusted runner JSON onto the portable evidence contract."""

        result: dict[str, Any] = {"passed": payload["passed"]}
        for field in ("reason", "status"):
            value = payload.get(field)
            if (
                isinstance(value, str)
                and len(value) <= 4000
                and not Path(value).is_absolute()
            ):
                result[field] = value
        return result

    @staticmethod
    def _member_at(
        members: tuple[SourceMember, ...],
        location: Path,
        label: str,
    ) -> SourceMember:
        matches = tuple(member for member in members if member.location == location)
        if len(matches) != 1:
            raise FlowExecutionError(
                f"typed design Action Plan has no unique {label} source"
            )
        return matches[0]


def _name(value: object, label: str) -> str:
    if not isinstance(value, str) or _NAME_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must match {_NAME_RE.pattern!r}")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be non-empty text")
    return value


def _evidence(role: str, level: str, scope: str) -> None:
    if not isinstance(role, str) or role not in EVIDENCE_ROLES:
        raise ValueError(f"unsupported design Action evidence_role: {role!r}")
    if not isinstance(level, str) or level not in EVIDENCE_LEVELS:
        raise ValueError(f"unsupported design Action evidence_level: {level!r}")
    if (
        not isinstance(scope, str)
        or not scope
        or "\n" in scope
        or "\r" in scope
        or Path(scope).is_absolute()
        or re.match(r"[A-Za-z]:[\\/]", scope)
    ):
        raise ValueError("design Action evidence_scope must be a relative identity")


def _canonical_relative(value: object, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty project-relative path")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or "\\" in value
        or relative.as_posix() != value
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError(f"{label} must be a canonical project-relative path")
    return relative


def _owned_file(
    project: Project,
    owner: RepositoryOwner,
    value: object,
    label: str,
) -> tuple[Path, PurePosixPath]:
    relative = _canonical_relative(value, label)
    resolved, canonical = project.resolve_owner_file(owner, relative.as_posix(), label)
    configured = project.project_root.joinpath(*relative.parts)
    if configured != resolved:
        raise ValueError(f"{label} must not traverse a symlink")
    return resolved, canonical


def _module_source(
    project: Project,
    owner: RepositoryOwner,
    module: str,
) -> tuple[Path, Path, str]:
    if module in _SHARED_MODULES:
        source_root = Path(__file__).resolve().parents[2]
        source = source_root.joinpath(*module.split(".")).with_suffix(".py")
        if source != source.resolve() or not source.is_file():
            raise ValueError(f"shared design runner source is missing: {module}")
        return source, source_root, "sigilicon-package"

    module_path = Path(*module.split("."))
    candidates = (
        module_path.with_suffix(".py"),
        module_path / "__main__.py",
    )
    existing = tuple(
        path
        for path in candidates
        if (project.project_root / path).is_file()
    )
    if len(existing) != 1:
        raise ValueError(
            "design Action module entrypoint must name one unambiguous "
            "project-owned module"
        )
    source, _relative = _owned_file(
        project,
        owner,
        existing[0].as_posix(),
        "design Action entrypoint",
    )
    return source, project.project_root, "project"


def _validate_runner_args(value: tuple[str, ...], field: str) -> None:
    for argument in value:
        if not isinstance(argument, str) or not argument:
            raise ValueError(f"{field} must contain non-empty strings")
        option = argument.split("=", 1)[0]
        if option in _ROUTING_ARGUMENTS:
            raise ValueError(f"{field} cannot override routing argument {option}")


def install_design_flow(
    registry: FlowRegistry,
    project: Project,
    owner: RepositoryOwner,
) -> None:
    """Install direct design planners and their shared stateless Adapter."""

    registry.register_adapter_factory(
        DESIGN_SOURCE_CHECK_ADAPTER,
        DesignTargetAdapter,
    )
    registry.register_adapter_factory(
        DESIGN_ELECTRICAL_DIAGNOSTIC_ADAPTER,
        DesignTargetAdapter,
    )

    def planner(node: FlowNode) -> ActionPlan:
        return plan_design_action(project, owner, node.config)

    registry.register_action_planner(DESIGN_SOURCE_CHECK_ACTION, planner)
    registry.register_action_planner(DESIGN_ELECTRICAL_DIAGNOSTIC_ACTION, planner)


__all__ = [
    "DesignInvocation",
    "DesignTargetAdapter",
    "install_design_flow",
    "plan_design_action",
]
