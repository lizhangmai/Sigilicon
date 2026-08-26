"""Canonical configuration for generated layout pilots."""

from __future__ import annotations

from dataclasses import dataclass
import importlib.util
from pathlib import Path
import tomllib
from typing import Any, Mapping

from sigilicon.domain.config_contracts import require_config_header
from sigilicon.domain.design import IDENTIFIER_RE
from sigilicon.domain.netlist import (
    NetlistSnapshot,
    load_netlist_snapshot,
    select_subckt_snapshot,
    subckt_ports,
)
from sigilicon.domain.oa_library import OALibrarySource, load_oa_library_source
from sigilicon.domain.physical_verification import PhysicalVerificationPolicy
from sigilicon.domain.platform import (
    LayoutPdkConfig,
    PdkConfig,
    load_platform,
)
from sigilicon.domain.repository import RepositoryContext


_DIRECTIONS = {"input", "output", "inputOutput"}


@dataclass(frozen=True)
class LayoutSpec:
    path: Path
    project_root: Path
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
    pdk: PdkConfig
    layout_pdk: LayoutPdkConfig

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


def _owner_oa_assembly(
    repository: RepositoryContext,
    spec_path: Path,
) -> OALibrarySource | None:
    """Resolve the canonical owner assembly when the spec belongs to one."""

    if repository.owner_for(spec_path) is None:
        return None
    manifest = repository.oa_assembly_for(spec_path)
    if manifest is None:
        return None
    source = load_oa_library_source(manifest, project_root=repository.project_root)
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


def load_layout_spec(path: Path, *, project_root: Path | None = None) -> LayoutSpec:
    spec_path = path.resolve()
    if project_root is None:
        raise ValueError("project_root is required for a layout spec")
    repository = RepositoryContext.from_project_root(project_root)
    root = repository.project_root
    try:
        spec_payload = spec_path.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read layout TOML {spec_path}: {exc}") from exc
    try:
        raw = tomllib.loads(spec_payload.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read layout TOML {spec_path}: {exc}") from exc
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
        project_module = root.joinpath(*module.split(".")).with_suffix(".py")
        project_package = root.joinpath(*module.split("."), "__init__.py")
        if project_module.is_file():
            source = project_module.resolve()
        elif project_package.is_file():
            source = project_package.resolve()
        else:
            module_spec = importlib.util.find_spec(module)
            origin = None if module_spec is None else module_spec.origin
            if origin is None or origin in {"built-in", "frozen"}:
                raise ValueError(
                    f"layout.generator_modules cannot resolve source: {module}"
                )
            source = Path(origin).resolve()
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
    full_snapshot = load_netlist_snapshot(source_netlist)
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
    source_snapshots = (full_snapshot,) + tuple(
        load_netlist_snapshot(dependency) for dependency in dependency_netlists
    )

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

    pdk = load_platform(repository, pdk_key)
    if pdk.layout is None:
        raise ValueError(
            f"platform {pdk_key!r} does not declare layout and verification contracts"
        )
    layout_pdk = pdk.layout
    assembly = _owner_oa_assembly(repository, spec_path)
    if assembly is not None and assembly.pdk != pdk_key:
        raise ValueError(
            f"layout spec platform {pdk_key!r} disagrees with OA assembly "
            f"{assembly.pdk!r}"
        )
    return LayoutSpec(
        path=spec_path,
        project_root=root,
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
    )
