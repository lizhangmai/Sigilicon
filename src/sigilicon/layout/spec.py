"""Canonical configuration for generated layout pilots."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import sysconfig
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping

from sigilicon.domain.component import ComponentContract, load_component_graph
from sigilicon.contracts import (
    DocumentStore,
    freeze_toml_document,
    is_frozen_toml_document,
    read_toml,
    require_config_header,
)
from sigilicon.domain.design import IDENTIFIER_RE
from sigilicon.domain.context import RepositoryIdentity
from sigilicon.domain.netlist import (
    NetlistSnapshot,
    load_netlist_snapshot,
    select_subckt_snapshot,
    subckt_ports,
)
from sigilicon.domain.oa_library import (
    OALibrarySource,
    find_oa_assembly,
    load_oa_library_source,
)
from sigilicon.domain.physical_verification import PhysicalVerificationPolicy
from sigilicon.domain.platform import (
    LayoutPlatform,
    Platform,
    PlatformSnapshot,
    resolve_platform_snapshot,
)

if TYPE_CHECKING:
    from sigilicon.project import Project


_DIRECTIONS = {"input", "output", "inputOutput"}
_MAPPING_PROXY_TYPE = type(MappingProxyType({}))
_PACKAGE_SOURCE_ROOT = Path(__file__).resolve().parents[2]
_STDLIB_SOURCE_ROOT = Path(sysconfig.get_path("stdlib")).resolve()


@dataclass(frozen=True)
class LayoutSpec:
    path: Path
    repository: RepositoryIdentity
    library: str
    cell: str
    view: str
    generator: str
    generator_source: Path
    generator_dependencies: tuple[Path, ...]
    generator_modules: tuple[str, ...]
    generator_module_sources: tuple[Path, ...]
    stage: str
    source_netlist: Path
    source_snapshot: NetlistSnapshot
    source_snapshots: tuple[NetlistSnapshot, ...]
    dependency_netlists: tuple[Path, ...]
    ports: tuple[str, ...]
    directions: Mapping[str, str]
    oa_assembly_manifest: Path | None
    primitive_masters: tuple[str, ...]
    physical_verification: PhysicalVerificationPolicy | None
    pdk: Platform
    layout_pdk: LayoutPlatform
    source_documents: Mapping[Path, Mapping[str, Any]] = field(
        default_factory=lambda: MappingProxyType({})
    )

    @property
    def project_root(self) -> Path:
        return self.repository.project_root

    @property
    def workspace_root(self) -> Path:
        return self.repository.workspace_root

def _table(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a table")
    return value


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or IDENTIFIER_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be an identifier")
    return value


def _path(value: Any, field: str, *, base: Path) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty path")
    result = Path(value).expanduser()
    if not result.is_absolute():
        result = base / result
    return result.resolve()


def _required_file(value: Any, field: str, *, base: Path) -> Path:
    result = _path(value, field, base=base)
    if not result.is_file():
        raise ValueError(f"{field} does not exist: {result}")
    return result


def _module_source(project_root: Path, module: str) -> Path | None:
    """Resolve declared project or trusted Sigilicon code without importing it."""

    parts = module.split(".")
    roots = (
        [_PACKAGE_SOURCE_ROOT]
        if parts[0] == "sigilicon"
        else [project_root, _STDLIB_SOURCE_ROOT]
    )
    for root in roots:
        module_file = root.joinpath(*parts).with_suffix(".py")
        package_file = root.joinpath(*parts, "__init__.py")
        if module_file.is_file():
            return module_file.resolve()
        if package_file.is_file():
            return package_file.resolve()
    return None


def _owner_oa_assembly(
    repository: Project,
    spec_path: Path,
    *,
    oa_source: OALibrarySource | None = None,
) -> OALibrarySource | None:
    """Resolve the canonical owner assembly when the spec belongs to one."""

    if oa_source is None:
        if repository.owner_for(spec_path) is None:
            return None
        manifest = find_oa_assembly(repository, spec_path)
        if manifest is None:
            return None
        source = load_oa_library_source(manifest, project=repository)
    else:
        oa_source.repository.validate(repository)
        source = oa_source
    declared_specs = {
        layout_spec
        for cell in source.cells
        for layout_spec in cell.layout_specs
    }
    if spec_path not in declared_specs:
        raise ValueError(
            f"layout spec is below an OA assembly owner but is not declared: {spec_path}"
        )
    return source


def _component_source_files(
    graph: Mapping[str, ComponentContract], *, project_root: Path
) -> Mapping[Path, tuple[tuple[ComponentContract, str], ...]]:
    """Index files declared by source-library components in *graph*.

    A layout generator is allowed to import project code only through the
    current owner's own source tree or an explicitly composed source-library
    component.  Keeping the index here makes that boundary use the component
    contract rather than directory naming conventions.
    """

    declared: dict[Path, list[tuple[ComponentContract, str]]] = {}
    for component in graph.values():
        if component.kind != "source-library":
            continue
        for source_id, relative in component.sources.items():
            source = (project_root / Path(relative)).resolve()
            declared.setdefault(source, []).append((component, source_id))
    return {
        source: tuple(declarations)
        for source, declarations in declared.items()
    }


def _validate_generator_ownership(
    repository: Project,
    *,
    owner: Any,
    component_graph: Mapping[str, ComponentContract],
    generator_source: Path,
    generator_dependencies: tuple[Path, ...],
    generator_modules: tuple[str, ...],
    generator_module_sources: tuple[Path, ...],
    source_netlist: Path,
    dependency_netlists: tuple[Path, ...],
    selected_platform_layout: Path,
) -> None:
    """Enforce the project source boundary for one cataloged layout owner.

    ``generator_source`` is executable entrypoint code and therefore must be
    physically owned by the owner which owns the layout spec.  Dependencies
    and module sources may cross that boundary only through a component graph
    edge to a ``source-library`` component.  The selected platform's exact
    layout contract is the one intentional non-component project dependency;
    other platform or repository files are not implicitly trusted.
    """

    owner_name = owner.name
    owner_root = owner.root

    try:
        generator_source.relative_to(owner_root)
    except ValueError as exc:
        raise ValueError(
            "layout.generator_source must belong to cataloged owner "
            f"{owner_name!r}: {generator_source}"
        ) from exc

    source_library_files = _component_source_files(
        component_graph, project_root=repository.project_root
    )

    def validate_project_source(source: Path, field: str) -> None:
        source_owner = repository.owner_for(source)
        declarations = source_library_files.get(source, ())
        if source_owner is not None and source_owner.name == owner_name:
            # Files in the current owner's root are already covered by the
            # owner contract.  The source-library declaration rule applies
            # only when a generator crosses that root boundary.
            return

        matching = (
            declarations
            if source_owner is None
            else tuple(
                (component, source_id)
                for component, source_id in declarations
                if component.name == source_owner.name
            )
        )
        if not matching:
            if declarations:
                declared = ", ".join(
                    f"{component.name!r} ({source_id})"
                    for component, source_id in declarations
                )
                if source_owner is None:
                    reason = (
                        "the project source has no cataloged owner and is not "
                        f"declared by the current owner graph (declared: {declared})"
                    )
                else:
                    reason = (
                        "the source-library declaration does not match the "
                        f"cataloged owner {source_owner.name!r} (declared: {declared})"
                    )
            else:
                reason = (
                    "the path is absent from the current owner component graph's "
                    "source-library inventory"
                )
            raise ValueError(
                f"{field} crosses owner boundary at {source}; {reason}"
            )

    for dependency in generator_dependencies:
        if dependency == selected_platform_layout:
            continue
        validate_project_source(dependency, "layout.generator_dependencies[]")

    for module, source in zip(generator_modules, generator_module_sources, strict=True):
        # Sources outside the project root are installed package modules and
        # remain valid.  Only project-owned module sources need this check.
        if source.is_relative_to(repository.project_root):
            validate_project_source(source, f"layout.generator_modules[{module!r}]")

    if not source_netlist.is_relative_to(owner_root):
        raise ValueError(
            "layout.source_netlist must belong to cataloged owner "
            f"{owner_name!r}: {source_netlist}"
        )
    for dependency in dependency_netlists:
        validate_project_source(dependency, "layout.dependency_netlists[]")


def load_layout_spec(
    path: Path,
    *,
    project: Project,
    oa_source: OALibrarySource | None = None,
    platform: PlatformSnapshot | None = None,
    netlist_inventory: Mapping[Path, NetlistSnapshot] | None = None,
) -> LayoutSpec:
    spec_path = path.absolute()
    repository = project
    root = repository.project_root
    if spec_path.resolve() != spec_path:
        raise ValueError(f"layout TOML must not traverse a symlink: {spec_path}")
    raw = read_toml(spec_path)
    if repository.owner_for(spec_path) is not None:
        require_config_header(
            raw,
            spec_path,
            contract_kind="cell-layout",
            path_scope="cell",
        )
    layout = _table(raw.get("layout"), "layout")
    ports_table = _table(raw.get("ports"), "ports")

    library = _identifier(layout.get("library"), "layout.library")
    cell = _identifier(layout.get("cell"), "layout.cell")
    view = _identifier(layout.get("view", "layout"), "layout.view")
    generator = _identifier(layout.get("generator"), "layout.generator")
    generator_source_value = layout.get("generator_source")
    if generator_source_value is not None:
        generator_source = _required_file(
            generator_source_value,
            "layout.generator_source",
            base=spec_path.parent,
        )
    else:
        generator_source = next(
            (
                parent / "layout_generator.py"
                for parent in (spec_path.parent, *spec_path.parents)
                if parent.is_relative_to(root)
                and (parent / "layout_generator.py").is_file()
            ),
            None,
        )
        if generator_source is None:
            raise ValueError(
                "layout.generator_source is required when no design-owned "
                "layout_generator.py is present in the spec ancestry"
            )
        generator_source = generator_source.resolve()
    try:
        generator_source.relative_to(root)
    except ValueError as exc:
        raise ValueError("layout.generator_source must stay below the project root") from exc
    if generator_source.suffix != ".py":
        raise ValueError("layout.generator_source must be a Python source file")
    raw_generator_dependencies = layout.get("generator_dependencies", [])
    if not isinstance(raw_generator_dependencies, list) or not all(
        isinstance(value, str) and value for value in raw_generator_dependencies
    ):
        raise ValueError("layout.generator_dependencies must be a path array")
    generator_dependencies = tuple(
        _required_file(
            value,
            "layout.generator_dependencies[]",
            base=spec_path.parent,
        )
        for value in raw_generator_dependencies
    )
    if len(set(generator_dependencies)) != len(generator_dependencies):
        raise ValueError("layout.generator_dependencies contains duplicate paths")
    if generator_source in generator_dependencies:
        raise ValueError("layout.generator_dependencies repeats layout.generator_source")
    for dependency in generator_dependencies:
        try:
            dependency.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                "layout.generator_dependencies must stay below the project root"
            ) from exc
    raw_generator_modules = layout.get("generator_modules", [])
    if not isinstance(raw_generator_modules, list) or any(
        not isinstance(value, str) or not value for value in raw_generator_modules
    ):
        raise ValueError("layout.generator_modules must be a module-name array")
    generator_modules = tuple(raw_generator_modules)
    if len(set(generator_modules)) != len(generator_modules):
        raise ValueError("layout.generator_modules contains duplicate names")
    module_sources: list[Path] = []
    for module in generator_modules:
        source = _module_source(root, module)
        if source is None:
            raise ValueError(
                "layout.generator_modules must name project, Sigilicon, or "
                f"standard-library code: {module}"
            )
        if not source.is_file() or source.suffix != ".py":
            raise ValueError(
                f"layout.generator_modules must resolve to Python source: {module}"
            )
        module_sources.append(source)
    generator_module_sources = tuple(module_sources)
    stage = layout.get("stage", "placement_probe")
    if stage not in {"placement_probe", "routed"}:
        raise ValueError("layout.stage must be placement_probe or routed")
    pdk_key = _identifier(layout.get("pdk"), "layout.pdk")
    source_netlist = _required_file(
        layout.get("source_netlist"),
        "layout.source_netlist",
        base=spec_path.parent,
    )
    try:
        source_netlist.relative_to(root)
    except ValueError as exc:
        raise ValueError("layout.source_netlist must stay below the project root") from exc
    full_snapshot = (
        None
        if netlist_inventory is None
        else netlist_inventory.get(source_netlist)
    )
    if full_snapshot is None:
        full_snapshot = load_netlist_snapshot(source_netlist)
    if full_snapshot.source_path != source_netlist:
        raise ValueError("layout netlist snapshot identity drift")
    source_snapshot = select_subckt_snapshot(full_snapshot, cell)
    dependency_values = layout.get("dependency_netlists", [])
    if not isinstance(dependency_values, list) or not all(
        isinstance(value, str) and value for value in dependency_values
    ):
        raise ValueError("layout.dependency_netlists must be a path array")
    dependency_netlists = tuple(
        _required_file(
            value,
            "layout.dependency_netlists[]",
            base=spec_path.parent,
        )
        for value in dependency_values
    )
    if len(set(dependency_netlists)) != len(dependency_netlists):
        raise ValueError("layout.dependency_netlists contains duplicate paths")
    if source_netlist in dependency_netlists:
        raise ValueError("layout.dependency_netlists repeats layout.source_netlist")
    for dependency in dependency_netlists:
        try:
            dependency.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                "layout.dependency_netlists must stay below the project root"
            ) from exc
    # LVS hierarchy resolution needs every sibling definition in the declared
    # canonical file.  The resolver still emits only the closure reachable
    # from ``layout.cell``, so unreachable subckts are not promoted into source.
    dependency_snapshots: list[NetlistSnapshot] = []
    for dependency in dependency_netlists:
        dependency_snapshot = (
            None
            if netlist_inventory is None
            else netlist_inventory.get(dependency)
        )
        if dependency_snapshot is None:
            dependency_snapshot = load_netlist_snapshot(dependency)
        if dependency_snapshot.source_path != dependency:
            raise ValueError("layout dependency netlist snapshot identity drift")
        dependency_snapshots.append(dependency_snapshot)
    source_snapshots = (full_snapshot, *dependency_snapshots)

    raw_ports = ports_table.get("order")
    if not isinstance(raw_ports, list) or not raw_ports:
        raise ValueError("ports.order must be a non-empty identifier array")
    declared_ports = tuple(_identifier(item, "ports.order[]") for item in raw_ports)
    if len(set(declared_ports)) != len(declared_ports):
        raise ValueError("ports.order contains duplicate names")
    canonical_ports = subckt_ports(source_snapshot, cell)
    if declared_ports != canonical_ports:
        raise ValueError(
            f"layout ports do not match canonical subckt {cell}: "
            f"declared={declared_ports}, canonical={canonical_ports}"
        )
    directions_table = _table(ports_table.get("directions"), "ports.directions")
    if set(directions_table) != set(declared_ports):
        raise ValueError("ports.directions keys must exactly match ports.order")
    directions: dict[str, str] = {}
    for name in declared_ports:
        direction = directions_table[name]
        if direction not in _DIRECTIONS:
            raise ValueError(f"unsupported direction for {name}: {direction!r}")
        directions[name] = direction

    pdk = resolve_platform_snapshot(repository, pdk_key, snapshot=platform)
    if pdk.layout is None:
        raise ValueError(
            f"platform {pdk_key!r} does not declare layout and verification contracts"
        )
    layout_pdk = pdk.layout
    assembly = _owner_oa_assembly(
        repository,
        spec_path,
        oa_source=oa_source,
    )
    if assembly is not None and assembly.pdk != pdk_key:
        raise ValueError(
            f"layout spec platform {pdk_key!r} disagrees with OA assembly "
            f"{assembly.pdk!r}"
        )
    owner = repository.owner_for(spec_path)
    if owner is not None:
        component_graph = load_component_graph(
            owner.component.path,
            project=repository,
            root_contract=owner.component,
            contract_inventory=repository.component_inventory,
        )
        _validate_generator_ownership(
            repository,
            owner=owner,
            component_graph=component_graph,
            generator_source=generator_source,
            generator_dependencies=generator_dependencies,
            generator_modules=generator_modules,
            generator_module_sources=generator_module_sources,
            source_netlist=source_netlist,
            dependency_netlists=dependency_netlists,
            selected_platform_layout=layout_pdk.layout_path,
        )
    return LayoutSpec(
        path=spec_path,
        repository=RepositoryIdentity.for_path(repository, spec_path),
        library=library,
        cell=cell,
        view=view,
        generator=generator,
        generator_source=generator_source,
        generator_dependencies=generator_dependencies,
        generator_modules=generator_modules,
        generator_module_sources=generator_module_sources,
        stage=stage,
        source_netlist=source_netlist,
        source_snapshot=source_snapshot,
        source_snapshots=source_snapshots,
        dependency_netlists=dependency_netlists,
        ports=declared_ports,
        directions=directions,
        oa_assembly_manifest=(assembly.manifest_path if assembly is not None else None),
        primitive_masters=(assembly.primitive_masters if assembly is not None else ()),
        physical_verification=(
            assembly.physical_verification if assembly is not None else None
        ),
        pdk=pdk,
        layout_pdk=layout_pdk,
        source_documents=MappingProxyType(
            {spec_path: freeze_toml_document(raw)}
        ),
    )


def resolve_layout_spec(
    path: Path,
    *,
    project: Project,
    snapshot: LayoutSpec | None = None,
    platform: PlatformSnapshot | None = None,
) -> LayoutSpec:
    """Load a layout spec or validate one operation-owned snapshot."""

    if snapshot is None:
        return load_layout_spec(path, project=project, platform=platform)
    spec_path = path.resolve()
    root = project.project_root
    if (
        snapshot.path != spec_path
        or snapshot.repository != RepositoryIdentity.for_path(project, spec_path)
        or not spec_path.is_relative_to(root)
        or not spec_path.is_file()
    ):
        raise ValueError("layout snapshot identity drift")
    owner = project.owner_for(spec_path)
    resolved_pdk = resolve_platform_snapshot(
        project,
        snapshot.pdk.key,
        snapshot=snapshot.pdk if platform is None else platform,
    )
    if not resolved_pdk.source_documents:
        raise ValueError("layout snapshot platform source document drift")
    if resolved_pdk.layout is None or snapshot.layout_pdk is not resolved_pdk.layout:
        raise ValueError("layout snapshot platform identity drift")
    if (
        not isinstance(snapshot.source_documents, Mapping)
        or set(snapshot.source_documents) != {spec_path}
        or any(
            not isinstance(source, Path)
            or source != source.resolve()
            or not source.is_relative_to(root)
            or not source.is_file()
            or not isinstance(document, Mapping)
            for source, document in snapshot.source_documents.items()
        )
    ):
        raise ValueError("layout snapshot source document identity drift")
    raw = snapshot.source_documents[spec_path]
    if (
        not isinstance(snapshot.source_documents, _MAPPING_PROXY_TYPE)
        or not is_frozen_toml_document(raw)
    ):
        raise ValueError("layout snapshot source document drift: mutable snapshot")
    DocumentStore(root, snapshot.source_documents).verify_current("layout snapshot")
    if owner is not None:
        require_config_header(
            raw,
            spec_path,
            contract_kind="cell-layout",
            path_scope="cell",
            owner=owner.name,
        )
    layout = raw.get("layout")
    ports = raw.get("ports")
    if not isinstance(layout, Mapping) or not isinstance(ports, Mapping):
        raise ValueError("layout snapshot source document drift")

    def declared_paths(field_name: str) -> tuple[Path, ...] | None:
        values = layout.get(field_name, ())
        if not isinstance(values, (list, tuple)) or any(
            not isinstance(value, str) or not value for value in values
        ):
            return None
        return tuple((spec_path.parent / value).resolve() for value in values)

    generator_source_value = layout.get("generator_source")
    if generator_source_value is None:
        generator_source = next(
            (
                parent / "layout_generator.py"
                for parent in (spec_path.parent, *spec_path.parents)
                if parent.is_relative_to(root)
                and (parent / "layout_generator.py").is_file()
            ),
            None,
        )
        if generator_source is not None:
            generator_source = generator_source.resolve()
    elif isinstance(generator_source_value, str) and generator_source_value:
        generator_source = (spec_path.parent / generator_source_value).resolve()
    else:
        generator_source = None
    source_netlist_value = layout.get("source_netlist")
    source_netlist = (
        None
        if not isinstance(source_netlist_value, str) or not source_netlist_value
        else (spec_path.parent / source_netlist_value).resolve()
    )
    generator_dependencies = declared_paths("generator_dependencies")
    dependency_netlists = declared_paths("dependency_netlists")
    module_names = layout.get("generator_modules", ())
    resolved_module_sources: list[Path] = []
    if isinstance(module_names, (list, tuple)):
        for module in module_names:
            if not isinstance(module, str) or not module:
                break
            module_source = _module_source(root, module)
            if module_source is None:
                break
            if not module_source.is_file() or module_source.suffix != ".py":
                break
            resolved_module_sources.append(module_source)
    project_sources = (
        snapshot.generator_source,
        *snapshot.generator_dependencies,
        snapshot.source_netlist,
        *snapshot.dependency_netlists,
    )
    if (
        layout.get("library") != snapshot.library
        or layout.get("cell") != snapshot.cell
        or layout.get("view", "layout") != snapshot.view
        or layout.get("generator") != snapshot.generator
        or layout.get("stage", "placement_probe") != snapshot.stage
        or layout.get("pdk") != snapshot.pdk.key
        or generator_source != snapshot.generator_source
        or generator_dependencies != snapshot.generator_dependencies
        or source_netlist != snapshot.source_netlist
        or dependency_netlists != snapshot.dependency_netlists
        or not isinstance(module_names, (list, tuple))
        or tuple(module_names) != snapshot.generator_modules
        or tuple(resolved_module_sources) != snapshot.generator_module_sources
        or any(
            source != source.resolve()
            or not source.is_file()
            or not source.is_relative_to(root)
            for source in project_sources
        )
        or tuple(
            load_netlist_snapshot(source)
            for source in (snapshot.source_netlist, *snapshot.dependency_netlists)
        )
        != snapshot.source_snapshots
        or snapshot.source_snapshot.source_path != snapshot.source_netlist
        or select_subckt_snapshot(
            snapshot.source_snapshots[0],
            snapshot.cell,
        )
        != snapshot.source_snapshot
        or tuple(item.source_path for item in snapshot.source_snapshots)
        != (snapshot.source_netlist, *snapshot.dependency_netlists)
        or tuple(ports.get("order") or ()) != snapshot.ports
        or ports.get("directions") != snapshot.directions
    ):
        raise ValueError("layout snapshot source document drift")
    if owner is not None:
        component_graph = load_component_graph(
            owner.component.path,
            project=project,
            root_contract=owner.component,
            contract_inventory=project.component_inventory,
        )
        _validate_generator_ownership(
            project,
            owner=owner,
            component_graph=component_graph,
            generator_source=snapshot.generator_source,
            generator_dependencies=snapshot.generator_dependencies,
            generator_modules=snapshot.generator_modules,
            generator_module_sources=snapshot.generator_module_sources,
            source_netlist=snapshot.source_netlist,
            dependency_netlists=snapshot.dependency_netlists,
            selected_platform_layout=resolved_pdk.layout.layout_path,
        )
    return snapshot
