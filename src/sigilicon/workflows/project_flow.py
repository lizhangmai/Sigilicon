"""Project-owned extensions assembled into the reusable Flow registry."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import stat
import sys
from types import ModuleType

from sigilicon.artifacts import read_nofollow_text
from sigilicon.domain.repository import RepositoryContext, RepositoryOwner
from sigilicon.flow import (
    ExecutionEnvironment,
    FlowCatalog,
    FlowEngine,
    FlowPlan,
    FlowProgress,
    FlowResult,
    PreflightResult,
    load_catalog_selection,
    load_flow_catalog,
)
from sigilicon.flow.model import SourceMember
from sigilicon.flow.registry import FlowRegistry
from sigilicon.flow.source_assets import source_member_matches
from sigilicon.workflows.builtin import builtin_workflow_registry


def _load_extension(source: Path, record_text: str) -> ModuleType:
    identity = hashlib.sha256(str(source).encode("utf-8")).hexdigest()
    module_name = f"_sigilicon_project_flow_{identity}"
    module = ModuleType(module_name)
    module.__file__ = str(source)
    module.__package__ = ""
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        exec(compile(record_text, str(source), "exec"), module.__dict__)
    except Exception as exc:
        raise ValueError(f"cannot load Flow registry extension {source}: {exc}") from exc
    finally:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
    return module


def _implementation_source(source: Path, *, project_root: Path) -> SourceMember:
    return SourceMember(
        path=source.relative_to(project_root).as_posix(),
        record_text=read_nofollow_text(source),
        executable=bool(
            source.stat(follow_symlinks=False).st_mode
            & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        ),
        location=source,
    )


def project_workflow_registry(
    project: RepositoryContext | Path | str,
    owner_root: Path | str | None,
) -> FlowRegistry:
    """Assemble built-ins and one explicitly selected owner extension.

    The project manifest selects the source, while the cataloged component
    proves that source belongs to the selected owner and its ``flow`` fileset.
    """

    if owner_root is None:
        return builtin_workflow_registry()
    repository = (
        project
        if isinstance(project, RepositoryContext)
        else RepositoryContext.from_project_root(project)
    )
    selected_root = Path(owner_root).resolve()
    owner = repository.require_owner(selected_root)
    if owner.root != selected_root:
        raise ValueError(
            f"Flow owner root must equal its cataloged root: {owner.root}"
        )
    registry = builtin_workflow_registry(owner.root)
    source = repository.flow_registry_extension(owner)
    if source is None:
        return registry
    try:
        implementation_sources = tuple(
            _implementation_source(
                candidate,
                project_root=repository.project_root,
            )
            for candidate in owner.files("flow")
            if candidate.suffix == ".py"
        )
    except (OSError, RuntimeError, UnicodeError) as exc:
        raise ValueError(
            f"cannot read owner Flow implementation source: {exc}"
        ) from exc
    source_record = next(
        (record for record in implementation_sources if record.location == source),
        None,
    )
    if source_record is None:
        raise ValueError(
            f"Flow registry extension {source} is not a bound Python implementation"
        )
    module = _load_extension(source, source_record.record_text)
    register = getattr(module, "register_flow_adapters", None)
    if not callable(register):
        raise ValueError(
            f"Flow registry extension {source} must define "
            "register_flow_adapters(registry, owner_root)"
        )
    try:
        result = register(registry, owner.root)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        raise ValueError(
            f"cannot register Flow extension {source}: {exc}"
        ) from exc
    if result is not None:
        raise ValueError(
            f"Flow registry extension {source} must mutate the supplied registry "
            "and return None"
        )
    try:
        stable_sources = all(
            source_member_matches(record) for record in implementation_sources
        )
    except (OSError, RuntimeError, UnicodeError):
        stable_sources = False
    if not stable_sources:
        raise ValueError("owner Flow implementation changed during registry assembly")
    for record in implementation_sources:
        registry.bind_implementation_source(record)
    return registry


def project_workflow_registry_for_owner_root(owner_root: Path | str) -> FlowRegistry:
    """Assemble the project containing an explicit owner root.

    A loose standalone owner directory remains supported when it is itself a
    minimal project root with no cataloged components. Discovery never consults
    cwd, so a neighboring project cannot silently select the wrong assembly.
    """

    selected_root = Path(owner_root).resolve()
    for candidate in (selected_root, *selected_root.parents):
        if not (candidate / "sigilicon.toml").is_file():
            continue
        repository = RepositoryContext.from_project_root(candidate)
        if candidate == selected_root and repository.owner_for(selected_root) is None:
            return builtin_workflow_registry(selected_root)
        return project_workflow_registry(repository, selected_root)
    return builtin_workflow_registry(selected_root)


@dataclass(frozen=True)
class ProjectFlowPlan:
    """One owner plan bound to its exact registry assembly."""

    engine: FlowEngine = field(repr=False, compare=False)
    plan: FlowPlan
    project_root: Path | None = field(default=None, repr=False)
    owner_root: Path | None = field(default=None, repr=False)

    @property
    def plan_identity(self) -> str:
        return self.engine.plan_id(self.plan)

    @property
    def record(self) -> dict[str, object]:
        return self.engine.plan_record(self.plan)


@dataclass(frozen=True)
class ProjectFlow:
    """Project-level Interface shared by CLI, Python and agent callers.

    The Module fixes one canonical owner and hides its root, Flow catalog,
    Execution Profile paths, registry assembly and project artifact root.
    ASIC, analog and mixed-signal differences remain behind typed Actions and
    owner Adapters.
    """

    repository: RepositoryContext
    owner_name: str
    registry_factory: Callable[
        [RepositoryContext | Path | str, Path | str | None], FlowRegistry
    ] = field(default=project_workflow_registry, repr=False, compare=False)

    def __post_init__(self) -> None:
        owner = self.repository.owner(self.owner_name)
        object.__setattr__(self, "owner_name", owner.name)

    @classmethod
    def from_project_root(
        cls,
        project_root: Path | str,
        *,
        owner: str,
    ) -> "ProjectFlow":
        return cls(RepositoryContext.from_project_root(project_root), owner)

    @property
    def owner(self) -> RepositoryOwner:
        return self.repository.owner(self.owner_name)

    @property
    def catalog_path(self) -> Path:
        return self.repository.owner_flow_catalog(self.owner)

    def catalog(self) -> FlowCatalog:
        """Load the owner's canonical typed Flow catalog."""

        return load_flow_catalog(self.catalog_path, owner_root=self.owner.root)

    def plan(
        self,
        *,
        flow: str,
        target: str,
        profile: str | None = None,
    ) -> ProjectFlowPlan:
        selection = load_catalog_selection(
            self.catalog_path,
            owner_root=self.owner.root,
            flow_id=flow,
            profile_id=profile,
        )
        engine = FlowEngine(
            self.registry_factory(self.repository, self.owner.root)
        )
        return ProjectFlowPlan(
            engine,
            engine.plan(selection.spec, target, selection.profile),
            self.repository.project_root,
            self.owner.root,
        )

    def preflight(
        self,
        planned: ProjectFlowPlan,
        environment: ExecutionEnvironment,
    ) -> PreflightResult:
        self._require_owned_plan(planned)
        return planned.engine.preflight(planned.plan, environment)

    def run(
        self,
        planned: ProjectFlowPlan,
        environment: ExecutionEnvironment,
        *,
        run_id: str | None = None,
        progress: Callable[[FlowProgress], None] | None = None,
    ) -> FlowResult:
        self._require_owned_plan(planned)
        return planned.engine.run(
            planned.plan,
            artifact_root=self.repository.project.artifact_root,
            environment=environment,
            run_id=run_id,
            progress=progress,
        )

    def _require_owned_plan(self, planned: ProjectFlowPlan) -> None:
        if (
            planned.plan.spec.owner != self.owner.name
            or planned.project_root != self.repository.project_root
            or planned.owner_root != self.owner.root
        ):
            raise ValueError(
                "Flow plan does not belong to this exact project owner binding"
            )


__all__ = [
    "ProjectFlow",
    "ProjectFlowPlan",
    "project_workflow_registry",
    "project_workflow_registry_for_owner_root",
]
