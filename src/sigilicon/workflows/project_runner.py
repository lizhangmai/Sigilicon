"""Compile owner targets into one managed project execution lifecycle."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
from pathlib import PurePosixPath
import stat
import sys
import tomllib
from types import ModuleType
from typing import TYPE_CHECKING, Any

from sigilicon.artifacts import read_nofollow_text
from sigilicon.domain.repository import Project, RepositoryOwner
from sigilicon.domain.targets import (
    OwnerTargetCatalog,
    ProjectTarget,
    TargetOperation,
    load_owner_target_catalog,
)
from sigilicon.flow import (
    ExecutionEnvironment,
    FlowEngine,
    FlowPlan,
    FlowProgress,
    FlowResult,
    FlowSpec,
    FlowTarget,
    PreflightResult,
    compile_flow_spec,
    parse_execution_recipe,
)
from sigilicon.flow.model import SourceMember
from sigilicon.flow.registry import FlowRegistry
from sigilicon.flow.source_assets import snapshot_source_member, source_member_matches
from sigilicon.workflows.builtin import build_flow_registry
from sigilicon.workflows.design_flow import install_design_flow
from sigilicon.workflows.layout_flow import install_layout_flow
from sigilicon.workflows.native_flow import install_native_flow

if TYPE_CHECKING:
    from sigilicon.virtuoso.client import VirtuosoClient


def _default_client_factory() -> VirtuosoClient:
    from sigilicon.virtuoso.client import get_client

    return get_client()


def _load_extension(source: Path, record_text: str) -> ModuleType:
    identity = hashlib.sha256(str(source).encode("utf-8")).hexdigest()
    module_name = f"_sigilicon_owner_extension_{identity}"
    module = ModuleType(module_name)
    module.__file__ = str(source)
    module.__package__ = ""
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        exec(compile(record_text, str(source), "exec"), module.__dict__)
    except Exception as exc:
        raise ValueError(f"cannot load owner registry extension {source}: {exc}") from exc
    finally:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
    return module


def _implementation_source(source: Path, *, project_root: Path) -> SourceMember:
    return SourceMember(
        path=source.relative_to(project_root).as_posix(),
        source_root=project_root,
        record_text=read_nofollow_text(source),
        executable=bool(
            source.stat(follow_symlinks=False).st_mode
            & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        ),
        location=source,
    )


def _project_workflow_registry(
    project: Project,
    owner: RepositoryOwner,
    *,
    client_factory: Callable[[], Any] = _default_client_factory,
) -> FlowRegistry:
    """Assemble reusable Actions and the selected owner's Adapter extension."""

    if owner not in project.owners:
        raise ValueError(
            f"execution owner {owner.name!r} does not belong to the selected Project"
        )
    registry = build_flow_registry()
    install_native_flow(
        registry,
        project,
        owner,
        client_factory=client_factory,
    )
    install_design_flow(registry, project, owner)
    install_layout_flow(
        registry,
        project,
        owner,
        client_factory=client_factory,
    )
    source = project.flow_registry_extension(owner)
    if source is None:
        return registry
    try:
        implementation_sources = tuple(
            _implementation_source(candidate, project_root=project.project_root)
            for candidate in owner.flow_implementation_files()
        )
    except (OSError, RuntimeError, UnicodeError) as exc:
        raise ValueError(f"cannot read owner implementation source: {exc}") from exc
    source_record = next(
        (record for record in implementation_sources if record.location == source),
        None,
    )
    if source_record is None:
        raise ValueError(
            f"owner registry extension {source} is not a bound Python implementation"
        )
    module = _load_extension(source, source_record.record_text)
    register = getattr(module, "register_action_modules", None)
    if not callable(register):
        raise ValueError(
            f"owner registry extension {source} must define "
            "register_action_modules(registry, project, owner)"
        )
    try:
        result = register(registry, project, owner)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        raise ValueError(f"cannot register owner extension {source}: {exc}") from exc
    if result is not None:
        raise ValueError(
            f"owner registry extension {source} must mutate the supplied registry "
            "and return None"
        )
    try:
        stable_sources = all(
            source_member_matches(record) for record in implementation_sources
        )
    except (OSError, RuntimeError, UnicodeError):
        stable_sources = False
    if not stable_sources:
        raise ValueError("owner implementation changed during registry assembly")
    for record in implementation_sources:
        registry.bind_implementation_source(record)
    return registry


@dataclass(frozen=True, init=False)
class ProjectExecution:
    """One owner target operation and its complete managed lifecycle."""

    _engine: FlowEngine = field(repr=False, compare=False)
    _plan: FlowPlan = field(repr=False)
    _project: Project = field(repr=False, compare=False)

    @classmethod
    def _bind(
        cls,
        engine: FlowEngine,
        plan: FlowPlan,
        project: Project,
    ) -> ProjectExecution:
        owner = project.owner(plan.spec.owner)
        expected_scope = project.scope(owner)
        if plan.spec.owner_root != owner.root or engine.project_scope != expected_scope:
            raise ValueError(
                "project execution engine, plan, and owner binding disagree"
            )
        execution = object.__new__(cls)
        object.__setattr__(execution, "_engine", engine)
        object.__setattr__(execution, "_plan", plan)
        object.__setattr__(execution, "_project", project)
        return execution

    @property
    def plan_identity(self) -> str:
        return self._engine.plan_identity(self._plan)

    @property
    def record(self) -> dict[str, object]:
        return self._engine.plan_record(self._plan)

    @property
    def owner(self) -> str:
        return self._plan.spec.owner

    @property
    def target(self) -> str:
        return self._plan.spec.flow_id

    @property
    def operation(self) -> str:
        return self._plan.target.target_id

    @property
    def recipe(self) -> str:
        return self._plan.spec.recipe_id

    @property
    def node_count(self) -> int:
        return len(self._plan.nodes)

    @property
    def execution_capabilities(self) -> tuple[str, ...]:
        return tuple(node.execution_capability for node in self._plan.nodes)

    @property
    def graph(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        return tuple(
            (node.node.node_id, node.dependencies) for node in self._plan.nodes
        )

    def preflight(self, environment: ExecutionEnvironment) -> PreflightResult:
        return self._engine.preflight(self._plan, environment)

    def preflight_record(self, result: PreflightResult) -> dict[str, object]:
        return self._engine.preflight_record(self._plan, result)

    def run(
        self,
        environment: ExecutionEnvironment,
        *,
        run_id: str | None = None,
        progress: Callable[[FlowProgress], None] | None = None,
    ) -> FlowResult:
        return self._engine.run(
            self._plan,
            artifact_root=self._project.artifact_root,
            environment=environment,
            run_id=run_id,
            progress=progress,
        )

    def restore_result(self, run_id: str) -> FlowResult:
        return self._engine.restore_result(
            self._plan,
            artifact_root=self._project.artifact_root,
            run_id=run_id,
        )

@dataclass(frozen=True)
class _TargetSelection:
    target: ProjectTarget
    operation: TargetOperation
    spec: FlowSpec


@dataclass(frozen=True)
class ProjectRunner:
    """Plan every project domain through one owner target interface."""

    project: Project
    owner_name: str
    client_factory: Callable[[], Any] = field(
        default=_default_client_factory,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        owner = self.project.owner(self.owner_name)
        if not callable(self.client_factory):
            raise ValueError("ProjectRunner client factory must be callable")
        object.__setattr__(self, "owner_name", owner.name)

    @property
    def owner(self) -> RepositoryOwner:
        return self.project.owner(self.owner_name)

    def targets(self) -> tuple[dict[str, object], ...]:
        """Return the owner's canonical target inventory."""

        source = self.project.owner_target_catalog(self.owner)
        targets = load_owner_target_catalog(
            self.project,
            self.owner,
            catalog_snapshot=source,
        )
        self._require_current(
            (
                snapshot_source_member(
                    source.path,
                    source_root=self.project.project_root,
                    scope="project",
                    record_text=source.record_text,
                    source_label="owner targets",
                ),
            )
        )
        return tuple(
            {
                "name": target.name,
                "description": target.description,
                "operations": tuple(target.operations),
            }
            for target in targets.targets.values()
        )

    def describe(
        self,
        target: str,
        operation: str | None = None,
    ) -> dict[str, object]:
        """Describe one target or one fully compiled target operation."""

        if operation is None:
            source = self.project.owner_target_catalog(self.owner)
            targets = load_owner_target_catalog(
                self.project,
                self.owner,
                catalog_snapshot=source,
            )
            selected = targets.get(target)
            return {
                "name": selected.name,
                "description": selected.description,
                "operations": tuple(selected.operations),
            }
        selection = self._select(target, operation)
        return {
            "schema": 1,
            "contract_kind": "target-operation-summary",
            "owner": self.owner.name,
            "target": selection.target.name,
            "operation": selection.operation.name,
            "recipe": selection.spec.recipe_id,
            "nodes": tuple(node.node_id for node in selection.spec.nodes),
            "policies": tuple(policy.policy_id for policy in selection.spec.policies),
        }

    def plan(self, target: str, operation: str) -> ProjectExecution:
        """Compile exactly one owner target operation."""

        selection = self._select(target, operation)
        engine = self._engine()
        plan = engine.plan(
            selection.spec,
            selection.operation.name,
        )
        return ProjectExecution._bind(engine, plan, self.project)

    def _select(self, target: str, operation: str) -> _TargetSelection:
        source = self.project.owner_target_catalog(self.owner)
        targets: OwnerTargetCatalog = load_owner_target_catalog(
            self.project,
            self.owner,
            catalog_snapshot=source,
        )
        selected_target = targets.get(target)
        selected_operation = selected_target.operation(operation)
        recipe_path = self.owner.root.joinpath(*selected_operation.recipe.parts)
        try:
            recipe_record = read_nofollow_text(recipe_path)
            raw = tomllib.loads(recipe_record)
        except (OSError, RuntimeError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise ValueError(
                f"cannot read execution recipe {recipe_path}: {exc}"
            ) from exc
        recipe = parse_execution_recipe(
            raw,
            recipe_path,
            owner_root=self.owner.root,
        )
        if recipe.owner != self.owner.name:
            raise ValueError("execution recipe owner disagrees with target owner")
        sources = (
            snapshot_source_member(
                source.path,
                source_root=self.project.project_root,
                scope="project",
                record_text=source.record_text,
                source_label="owner targets",
            ),
            snapshot_source_member(
                recipe_path,
                source_root=self.project.project_root,
                scope="project",
                record_text=recipe_record,
                source_label="execution recipe",
            ),
        )
        input_sources = list(sources)
        for name, declaration in recipe.inputs.items():
            if declaration.kind != "owner-path":
                continue
            value = selected_target.inputs.get(name)
            if not isinstance(value, str):
                continue
            relative = PurePosixPath(value)
            if (
                relative.is_absolute()
                or "\\" in value
                or relative.as_posix() != value
                or any(part in {"", ".", ".."} for part in relative.parts)
            ):
                continue
            configured = self.owner.root.joinpath(*relative.parts)
            resolved = configured.resolve(strict=False)
            if configured != resolved or not resolved.is_relative_to(self.owner.root):
                continue
            if resolved.is_file():
                member = snapshot_source_member(
                    resolved,
                    source_root=self.project.project_root,
                    scope="project",
                    source_label=f"execution input {name}",
                )
                if not any(
                    (
                        existing.scope,
                        existing.source_root,
                        existing.path,
                    )
                    == (member.scope, member.source_root, member.path)
                    for existing in input_sources
                ):
                    input_sources.append(member)
        sources = tuple(input_sources)
        self._require_current(sources)
        spec = compile_flow_spec(
            recipe,
            flow_id=selected_target.name,
            targets=(
                FlowTarget(
                    selected_operation.name,
                    selected_operation.goals,
                ),
            ),
            inputs=selected_target.inputs,
            source_members=sources,
        )
        self._require_current(sources)
        return _TargetSelection(
            selected_target,
            selected_operation,
            spec,
        )

    @staticmethod
    def _require_current(sources: tuple[SourceMember, ...]) -> None:
        try:
            current = all(source_member_matches(source) for source in sources)
        except (OSError, RuntimeError, UnicodeError):
            current = False
        if not current:
            raise ValueError("target selection source changed during planning")

    def _engine(self) -> FlowEngine:
        return FlowEngine(
            _project_workflow_registry(
                self.project,
                self.owner,
                client_factory=self.client_factory,
            ),
            project_scope=self.project.scope(self.owner),
        )


__all__ = [
    "ProjectExecution",
    "ProjectRunner",
]
