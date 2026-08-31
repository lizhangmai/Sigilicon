"""Project-owned extensions assembled into the reusable Flow registry."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import re
import stat
import sys
from types import ModuleType
from typing import TYPE_CHECKING, Any

from sigilicon.artifacts import read_nofollow_text
from sigilicon.domain.repository import (
    OwnerCatalogSnapshot,
    Project,
    RepositoryOwner,
)
from sigilicon.flow import (
    ExecutionEnvironment,
    DesignCatalogExpansion,
    FlowCatalog,
    FlowEngine,
    FlowPlan,
    FlowProgress,
    FlowResult,
    LayoutCatalogExpansion,
    PreflightResult,
    parse_flow_catalog,
    resolve_catalog_selection,
)
from sigilicon.flow.model import SourceMember, identifier
from sigilicon.flow.native import (
    NATIVE_OA_PLAN_ADAPTER,
    NATIVE_OA_SIMULATION_ADAPTER,
    XCELIUM_VERIFICATION_ADAPTER,
    XCELIUM_AMS_VERIFICATION_ADAPTER,
)
from sigilicon.flow.circuit_design import (
    DESIGN_ELECTRICAL_DIAGNOSTIC_ADAPTER,
    DESIGN_SOURCE_CHECK_ADAPTER,
)
from sigilicon.flow.layout import (
    LAYOUT_GENERATION_ADAPTER,
    LAYOUT_VERIFICATION_ADAPTER,
)
from sigilicon.flow.registry import FlowRegistry, ToolAdapter
from sigilicon.flow.source_assets import source_member_matches
from sigilicon.workflows.builtin import build_flow_registry
from sigilicon.workflows.catalog_flow import (
    compile_design_catalog_flow,
    compile_layout_catalog_flow,
)
from sigilicon.workflows.design_targets import (
    DesignTargetCatalog,
    load_design_target_catalog,
)
from sigilicon.workflows.layout_generation import (
    LayoutPlanningResult,
    plan_layout_spec,
)
from sigilicon.workflows.layout_targets import LayoutTargetCatalog

if TYPE_CHECKING:
    from sigilicon.virtuoso.client import VirtuosoClient


@dataclass(frozen=True)
class FlowRunSelection:
    flow: str
    target: str
    profile: str | None = None

    def __post_init__(self) -> None:
        identifier(self.flow, "Flow identity")
        identifier(self.target, "Flow target")
        if self.profile is not None:
            identifier(self.profile, "Execution Profile identity")


@dataclass(frozen=True)
class DesignRunSelection:
    target: str
    mode: str

    def __post_init__(self) -> None:
        identifier(self.target, "design target")
        identifier(self.mode, "design mode")


@dataclass(frozen=True)
class LayoutRunSelection:
    target: str
    operation: str

    def __post_init__(self) -> None:
        identifier(self.target, "layout target")
        if self.operation not in {
            "generate",
            "verify-drc",
            "verify-lvs",
            "verify-all",
        }:
            raise ValueError(f"unsupported layout operation: {self.operation!r}")


@dataclass(frozen=True)
class OaSimulationSelection:
    testbench: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.testbench, str)
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", self.testbench) is None
        ):
            raise ValueError(f"invalid OA simulation testbench: {self.testbench!r}")


RunSelection = (
    FlowRunSelection
    | DesignRunSelection
    | LayoutRunSelection
    | OaSimulationSelection
)


@dataclass(frozen=True)
class RunRequest:
    """One typed project operation compiled through the canonical Flow seam."""

    selection: RunSelection

    def __post_init__(self) -> None:
        if not isinstance(
            self.selection,
            (
                FlowRunSelection,
                DesignRunSelection,
                LayoutRunSelection,
                OaSimulationSelection,
            ),
        ):
            raise ValueError("RunRequest selection must be a typed selection")

    @classmethod
    def flow(
        cls,
        flow: str,
        target: str,
        profile: str | None = None,
    ) -> RunRequest:
        return cls(FlowRunSelection(flow, target, profile))

    @classmethod
    def design(cls, target: str, mode: str) -> RunRequest:
        return cls(DesignRunSelection(target, mode))

    @classmethod
    def layout(cls, target: str, operation: str) -> RunRequest:
        return cls(LayoutRunSelection(target, operation))

    @classmethod
    def oa_simulation(cls, testbench: str) -> RunRequest:
        return cls(OaSimulationSelection(testbench))


def _default_client_factory() -> VirtuosoClient:
    from sigilicon.virtuoso.client import get_client

    return get_client()


def _native_oa_plan_adapter(project: Project, owner: str) -> ToolAdapter:
    from sigilicon.workflows.native_flow import NativeOaPlanAdapter

    return NativeOaPlanAdapter(project, owner)


def _native_oa_simulation_adapter(project: Project, owner: str) -> ToolAdapter:
    from sigilicon.workflows.native_flow import NativeOaSimulationAdapter

    return NativeOaSimulationAdapter(project, owner)


def _xcelium_verification_adapter(project: Project, owner: str) -> ToolAdapter:
    from sigilicon.workflows.native_flow import XceliumVerificationAdapter

    return XceliumVerificationAdapter(project, owner)


def _xcelium_ams_verification_adapter(
    project: Project,
    owner: str,
) -> ToolAdapter:
    from sigilicon.workflows.native_flow import XceliumAmsVerificationAdapter

    return XceliumAmsVerificationAdapter(project, owner)


def _design_target_adapter(
    project: Project,
    owner: str,
    catalog_inventory: tuple[OwnerCatalogSnapshot, ...],
    design_catalog: DesignTargetCatalog | None,
) -> ToolAdapter:
    from sigilicon.workflows.design_flow import ProjectDesignTargetAdapter

    return ProjectDesignTargetAdapter(
        project,
        owner,
        design_catalog
        if design_catalog is not None
        else load_design_target_catalog(
            project,
            catalog_inventory=catalog_inventory,
        ),
    )


def _layout_target_adapter(
    project: Project,
    owner: str,
    catalog: LayoutTargetCatalog,
    *,
    target: str,
    operation: str,
    planning: LayoutPlanningResult,
    client_factory: Callable[[], Any],
    sources: tuple[SourceMember, ...],
) -> ToolAdapter:
    from sigilicon.workflows.layout_flow import ProjectLayoutTargetAdapter

    return ProjectLayoutTargetAdapter(
        project,
        owner,
        catalog,
        target=target,
        operation=operation,
        planning=planning,
        client_factory=client_factory,
        sources=sources,
    )


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
    catalog_inventory: tuple[OwnerCatalogSnapshot, ...],
    *,
    design_catalog: DesignTargetCatalog | None = None,
    layout_adapter_factory: Callable[[], ToolAdapter] | None = None,
    intent_sources: tuple[SourceMember, ...] = (),
) -> FlowRegistry:
    """Assemble built-ins and one explicitly selected owner extension.

    The project manifest selects the source, while the cataloged component
    proves that source belongs to the selected owner and its ``flow`` fileset.
    """

    repository = project
    if owner not in repository.owners:
        raise ValueError(
            f"Flow owner {owner.name!r} does not belong to the selected Project"
        )
    registry = build_flow_registry()
    for record in intent_sources:
        registry.bind_implementation_source(record)
    registry.register_adapter_factory(
        NATIVE_OA_PLAN_ADAPTER,
        lambda: _native_oa_plan_adapter(project, owner.name),
    )
    registry.register_adapter_factory(
        NATIVE_OA_SIMULATION_ADAPTER,
        lambda: _native_oa_simulation_adapter(project, owner.name),
    )
    registry.register_adapter_factory(
        XCELIUM_VERIFICATION_ADAPTER,
        lambda: _xcelium_verification_adapter(project, owner.name),
    )
    registry.register_adapter_factory(
        XCELIUM_AMS_VERIFICATION_ADAPTER,
        lambda: _xcelium_ams_verification_adapter(project, owner.name),
    )
    registry.register_adapter_factory(
        DESIGN_SOURCE_CHECK_ADAPTER,
        lambda: _design_target_adapter(
            project,
            owner.name,
            catalog_inventory,
            design_catalog,
        ),
    )
    registry.register_adapter_factory(
        DESIGN_ELECTRICAL_DIAGNOSTIC_ADAPTER,
        lambda: _design_target_adapter(
            project,
            owner.name,
            catalog_inventory,
            design_catalog,
        ),
    )
    if layout_adapter_factory is not None:
        registry.register_adapter_factory(
            LAYOUT_GENERATION_ADAPTER,
            layout_adapter_factory,
        )
        registry.register_adapter_factory(
            LAYOUT_VERIFICATION_ADAPTER,
            layout_adapter_factory,
        )
    source = repository.flow_registry_extension(owner)
    if source is None:
        return registry
    try:
        implementation_sources = tuple(
            _implementation_source(
                candidate,
                project_root=repository.project_root,
            )
            for candidate in owner.flow_implementation_files()
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
    intent_by_path = {record.path: record for record in intent_sources}
    for record in implementation_sources:
        selected = intent_by_path.get(record.path)
        if selected is not None:
            if selected != record:
                raise ValueError(
                    f"design intent source conflicts with Flow implementation: "
                    f"{record.path}"
                )
            continue
        registry.bind_implementation_source(record)
    return registry


@dataclass(frozen=True)
class ProjectFlowPlan:
    """One owner plan bound to its exact registry assembly."""

    _engine: FlowEngine = field(repr=False, compare=False)
    _plan: FlowPlan = field(repr=False)
    _project: Project = field(repr=False, compare=False)

    @property
    def plan_identity(self) -> str:
        return self._engine.plan_id(self._plan)

    @property
    def record(self) -> dict[str, object]:
        return self._engine.plan_record(self._plan)

    @property
    def owner(self) -> str:
        return self._plan.spec.owner

    @property
    def flow(self) -> str:
        return self._plan.spec.flow_id

    @property
    def target(self) -> str:
        return self._plan.target.target_id

    @property
    def profile(self) -> str:
        return self._plan.profile.profile_id

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


@dataclass(frozen=True)
class ProjectFlow:
    """Project-level Interface shared by CLI, Python and agent callers.

    The Module fixes one canonical owner and hides its root, Flow catalog,
    Execution Profile paths, registry assembly and project artifact root.
    ASIC, analog and mixed-signal differences remain behind typed Actions and
    owner Adapters.
    """

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
            raise ValueError("ProjectFlow client factory must be callable")
        object.__setattr__(self, "owner_name", owner.name)

    @property
    def owner(self) -> RepositoryOwner:
        return self.project.owner(self.owner_name)

    def catalog(self) -> FlowCatalog:
        """Load the owner's canonical typed Flow catalog."""

        return self._catalog(
            self.project.owner_flow_catalog_inventory(self.owner)
        )

    def _catalog(
        self,
        inventory: tuple[OwnerCatalogSnapshot, ...],
    ) -> FlowCatalog:
        snapshots = tuple(
            snapshot
            for snapshot in inventory
            if snapshot.contract_kind == "flow-catalog"
        )
        if len(snapshots) != 1:
            raise ValueError(
                f"cataloged owner {self.owner.name!r} must select exactly one "
                "Flow Catalog"
            )
        snapshot = snapshots[0]
        return parse_flow_catalog(
            snapshot.document,
            snapshot.path,
            owner_root=self.owner.root,
        )

    def plan(
        self,
        request: RunRequest | None = None,
        *,
        flow: str | None = None,
        target: str | None = None,
        profile: str | None = None,
    ) -> ProjectFlowPlan:
        """Compile a typed request; keyword arguments are a compatibility facade."""

        if request is None:
            if flow is None or target is None:
                raise ValueError("Flow planning requires a RunRequest")
            request = RunRequest.flow(flow, target, profile)
        elif flow is not None or target is not None or profile is not None:
            raise ValueError("RunRequest cannot be combined with legacy Flow fields")
        if not isinstance(request, RunRequest):
            raise ValueError("ProjectFlow.plan requires a RunRequest")

        selection = request.selection
        if isinstance(selection, FlowRunSelection):
            return self._plan_flow(selection)
        if isinstance(selection, DesignRunSelection):
            return self._plan_design(selection)
        if isinstance(selection, LayoutRunSelection):
            return self._plan_layout(selection)
        if isinstance(selection, OaSimulationSelection):
            return self._plan_oa_simulation(selection)
        raise AssertionError("unhandled typed RunRequest")

    def _plan_flow(
        self,
        request: FlowRunSelection,
        *,
        catalog_inventory: tuple[OwnerCatalogSnapshot, ...] | None = None,
    ) -> ProjectFlowPlan:
        catalog_inventory = (
            self.project.owner_flow_catalog_inventory(self.owner)
            if catalog_inventory is None
            else catalog_inventory
        )
        selection = resolve_catalog_selection(
            self._catalog(catalog_inventory),
            flow_id=request.flow,
            profile_id=request.profile,
        )
        if isinstance(selection.spec.catalog_expansion, DesignCatalogExpansion):
            catalog = load_design_target_catalog(
                self.project,
                catalog_inventory=catalog_inventory,
            ).for_owner(self.owner.name)
            matches = [
                DesignRunSelection(target.name, mode.name)
                for target in catalog.targets
                for mode in target.modes
                if mode.flow == request.flow and mode.target == request.target
            ]
            if len(matches) != 1:
                raise ValueError(
                    f"expanded design Flow target {request.target!r} resolved "
                    f"{matches!r}"
                )
            if request.profile not in {None, selection.profile.profile_id}:
                raise ValueError("expanded design route profile drift")
            return self._plan_design(matches[0])
        if isinstance(selection.spec.catalog_expansion, LayoutCatalogExpansion):
            from sigilicon.workflows.layout_targets import load_layout_target_catalog

            catalog = load_layout_target_catalog(
                self.project,
                catalog_inventory=catalog_inventory,
            ).for_owner(self.owner.name)
            matches = [
                LayoutRunSelection(target.name, route.operation)
                for target in catalog.targets
                for route in target.routes
                if route.flow == request.flow and route.target == request.target
            ]
            if len(matches) != 1:
                raise ValueError(
                    f"expanded layout Flow target {request.target!r} resolved "
                    f"{matches!r}"
                )
            if request.profile not in {None, selection.profile.profile_id}:
                raise ValueError("expanded layout route profile drift")
            return self._plan_layout(matches[0])
        engine = self._engine(catalog_inventory)
        return self._bind(
            engine,
            engine.plan(selection.spec, request.target, selection.profile),
        )

    def _plan_design(self, request: DesignRunSelection) -> ProjectFlowPlan:
        inventory = self.project.owner_flow_catalog_inventory(self.owner)
        selected_catalog = load_design_target_catalog(
            self.project,
            catalog_inventory=inventory,
        ).for_owner(self.owner.name)
        selected_target = selected_catalog.get(request.target)
        selected_mode = selected_target.get_mode(request.mode)
        engine = self._engine(
            selected_catalog.inventory,
            design_catalog=selected_catalog,
            intent_sources=selected_catalog.source_members_for(selected_target),
        )
        selection = resolve_catalog_selection(
            self._catalog(selected_catalog.inventory),
            flow_id=selected_mode.flow,
            profile_id=None,
        )
        spec = (
            compile_design_catalog_flow(selection.spec, selected_catalog)
            if isinstance(
                selection.spec.catalog_expansion,
                DesignCatalogExpansion,
            )
            else selection.spec
        )
        return self._bind(
            engine,
            engine.plan(spec, selected_mode.target, selection.profile),
        )

    def _plan_layout(self, request: LayoutRunSelection) -> ProjectFlowPlan:
        from sigilicon.workflows.layout_targets import load_layout_target_catalog

        inventory = self.project.owner_flow_catalog_inventory(self.owner)
        selected_catalog = load_layout_target_catalog(
            self.project,
            catalog_inventory=inventory,
        ).for_owner(self.owner.name)
        selected_target = selected_catalog.get(request.target)
        route = selected_target.get_route(request.operation)
        planning = plan_layout_spec(selected_target.spec, project=self.project)
        intent_sources = selected_catalog.source_members_for(
            selected_target,
            planning,
        )
        engine = self._engine(
            selected_catalog.inventory,
            layout_adapter_factory=lambda: _layout_target_adapter(
                self.project,
                self.owner.name,
                selected_catalog,
                target=selected_target.name,
                operation=request.operation,
                planning=planning,
                client_factory=self.client_factory,
                sources=intent_sources,
            ),
            intent_sources=intent_sources,
        )
        selection = resolve_catalog_selection(
            self._catalog(selected_catalog.inventory),
            flow_id=route.flow,
            profile_id=None,
        )
        spec = (
            compile_layout_catalog_flow(selection.spec, selected_catalog)
            if isinstance(
                selection.spec.catalog_expansion,
                LayoutCatalogExpansion,
            )
            else selection.spec
        )
        return self._bind(
            engine,
            engine.plan(spec, route.target, selection.profile),
        )

    def _plan_oa_simulation(
        self,
        request: OaSimulationSelection,
    ) -> ProjectFlowPlan:
        inventory = self.project.owner_flow_catalog_inventory(self.owner)
        catalog = self._catalog(inventory)
        matches: list[FlowRunSelection] = []
        for entry in catalog.entries:
            selection = resolve_catalog_selection(catalog, flow_id=entry.flow_id)
            node_ids = {
                node.node_id
                for node in selection.spec.nodes
                if node.action_kind == "native-oa.simulate"
                and node.config.get("testbench") == request.testbench
            }
            matches.extend(
                FlowRunSelection(entry.flow_id, target.target_id)
                for target in selection.spec.targets
                if len(target.goals) == 1 and target.goals[0] in node_ids
            )
        if len(matches) != 1:
            raise ValueError(
                "OA testbench must resolve to exactly one cataloged typed "
                f"Flow target: {request.testbench!r} resolved {matches!r}"
            )
        return self._plan_flow(matches[0], catalog_inventory=inventory)

    def plan_design(
        self,
        catalog: DesignTargetCatalog,
        *,
        target: str,
        mode: str,
    ) -> ProjectFlowPlan:
        """Compile one owner design intent into its exact typed Flow plan."""

        if catalog.project is not self.project:
            raise ValueError("design catalog does not belong to this exact Project")
        return self.plan(RunRequest.design(target, mode))

    def plan_layout(
        self,
        catalog: LayoutTargetCatalog,
        *,
        target: str,
        operation: str,
    ) -> ProjectFlowPlan:
        """Compile one owner layout intent into its exact typed Flow plan."""

        if catalog.project is not self.project:
            raise ValueError("layout catalog does not belong to this exact Project")
        return self.plan(RunRequest.layout(target, operation))

    def preflight(
        self,
        planned: ProjectFlowPlan,
        environment: ExecutionEnvironment,
    ) -> PreflightResult:
        self._require_owned_plan(planned)
        return planned._engine.preflight(planned._plan, environment)

    def preflight_record(
        self,
        planned: ProjectFlowPlan,
        result: PreflightResult,
    ) -> dict[str, object]:
        self._require_owned_plan(planned)
        return planned._engine.preflight_record(planned._plan, result)

    def run(
        self,
        planned: ProjectFlowPlan,
        environment: ExecutionEnvironment,
        *,
        run_id: str | None = None,
        progress: Callable[[FlowProgress], None] | None = None,
    ) -> FlowResult:
        self._require_owned_plan(planned)
        return planned._engine.run(
            planned._plan,
            artifact_root=self.project.artifact_root,
            environment=environment,
            run_id=run_id,
            progress=progress,
        )

    def read_result(
        self,
        *,
        flow: str,
        target: str,
        run_id: str,
    ) -> dict[str, Any]:
        """Read one persisted result selected through this owner's catalog."""

        planned = self.plan(RunRequest.flow(flow, target))
        return FlowEngine(FlowRegistry()).read_run_result(
            artifact_root=self.project.artifact_root,
            owner=self.owner.name,
            flow_id=planned.flow,
            target=planned.target,
            run_id=run_id,
        )

    def clean_run(
        self,
        *,
        flow: str,
        target: str,
        run_id: str,
    ) -> None:
        """Remove one manifest-owned result selected through this owner."""

        planned = self.plan(RunRequest.flow(flow, target))
        FlowEngine(FlowRegistry()).clean_run(
            artifact_root=self.project.artifact_root,
            owner=self.owner.name,
            flow_id=planned.flow,
            target=planned.target,
            run_id=run_id,
        )

    def _engine(
        self,
        catalog_inventory: tuple[OwnerCatalogSnapshot, ...],
        *,
        design_catalog: DesignTargetCatalog | None = None,
        layout_adapter_factory: Callable[[], ToolAdapter] | None = None,
        intent_sources: tuple[SourceMember, ...] = (),
    ) -> FlowEngine:
        return FlowEngine(
            _project_workflow_registry(
                self.project,
                self.owner,
                catalog_inventory,
                design_catalog=design_catalog,
                layout_adapter_factory=layout_adapter_factory,
                intent_sources=intent_sources,
            ),
            project_scope=self.project.scope(self.owner),
        )

    def _bind(self, engine: FlowEngine, plan: FlowPlan) -> ProjectFlowPlan:
        return ProjectFlowPlan(engine, plan, self.project)

    def _require_owned_plan(self, planned: ProjectFlowPlan) -> None:
        if (
            planned._plan.spec.owner != self.owner.name
            or planned._project is not self.project
        ):
            raise ValueError(
                "Flow plan does not belong to this exact project owner binding"
            )


def resolve_project_flow_plan(
    project: Project,
    plan_identity: str,
) -> ProjectFlowPlan:
    """Resolve one exact plan identity through its selected project owner."""

    if not isinstance(plan_identity, str) or not plan_identity:
        raise ValueError("Flow Plan identity must be non-empty text")
    fields = plan_identity.split(":")
    if len(fields) != 4:
        raise ValueError(
            "Flow Plan identity must be owner:flow:target:profile"
        )
    owner, flow, target, profile = fields
    resolved = ProjectFlow(project, owner).plan(
        RunRequest.flow(flow, target, profile),
    )
    if resolved.plan_identity != plan_identity:
        raise ValueError("Flow Plan identity does not match its project plan")
    return resolved


__all__ = [
    "DesignRunSelection",
    "FlowRunSelection",
    "LayoutRunSelection",
    "OaSimulationSelection",
    "ProjectFlow",
    "ProjectFlowPlan",
    "RunRequest",
    "resolve_project_flow_plan",
]
