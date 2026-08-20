"""Canonical configuration for generated layout pilots."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tomllib
from typing import Any, Mapping

from sigilicon.domain.config_contracts import require_config_header
from sigilicon.domain.design import IDENTIFIER_RE, PdkConfig, load_pdk_config
from sigilicon.domain.netlist import (
    NetlistSnapshot,
    load_netlist_snapshot,
    select_subckt_snapshot,
    subckt_ports,
)
from sigilicon.layout.profile import LayoutGenerationConfig, load_layout_generation_config
from sigilicon.paths import ProjectContext


_DIRECTIONS = {"input", "output", "inputOutput"}


@dataclass(frozen=True)
class PcellPolicy:
    finger_count_parameter: str | None = None
    source_terminal: str = "S"
    drain_terminal: str = "D"
    source_alias_prefix: str = "S_"
    drain_alias_prefix: str = "D_"
    cdf_callback_parameter: str | None = None
    cdf_callback_bypass_parameters: tuple[str, ...] = ()


@dataclass(frozen=True)
class LayoutPdkConfig:
    configuration_sha256: str
    dbu_per_micron: int
    layermap: Path
    drc_deck: Path
    lvs_deck: Path
    qrc_tech_file: Path
    primitive_masters: tuple[str, ...]
    primitive_subcircuits: Mapping[str, tuple[str, ...]]
    drc_disabled_defines: Mapping[str, int]
    drc_configuration_warnings: tuple[str, ...]
    drc_waiver_layers: tuple[str, ...]
    pcell_policy: PcellPolicy
    generation: LayoutGenerationConfig
    xstream_flatten_pcells: bool = True
    xstream_suppressed_warnings: tuple[str, ...] = ()
    xstream_bin: Path | None = None
    calibre_bin: Path | None = None


@dataclass(frozen=True)
class LayoutSpec:
    path: Path
    spec_sha256: str
    project_root: Path
    library: str
    cell: str
    view: str
    generator: str
    generator_source: Path
    generator_source_sha256: str
    generator_source_declared: bool
    generator_dependencies: tuple[Path, ...]
    generator_dependency_sha256s: tuple[str, ...]
    generator_modules: tuple[str, ...]
    generator_module_sources: tuple[Path, ...]
    generator_module_sha256s: tuple[str, ...]
    stage: str
    source_netlist: Path
    source_snapshot: NetlistSnapshot
    source_snapshots: tuple[NetlistSnapshot, ...]
    dependency_netlists: tuple[Path, ...]
    ports: tuple[str, ...]
    directions: Mapping[str, str]
    pdk: PdkConfig
    layout_pdk: LayoutPdkConfig

    @property
    def source_fingerprint(self) -> str:
        payload: dict[str, Any] = {
            "layout_spec_sha256": self.spec_sha256,
            "source_sha256": self.source_snapshot.sha256,
            "library": self.library,
            "cell": self.cell,
            "view": self.view,
            "generator": self.generator,
            "stage": self.stage,
            "ports": self.ports,
            "directions": dict(self.directions),
            "technology_library": self.pdk.technology_library,
            "dbu_per_micron": self.layout_pdk.dbu_per_micron,
            "pdk_configuration_sha256": self.layout_pdk.configuration_sha256,
            "layout_profile": self.layout_pdk.generation.geometry.path.relative_to(
                self.project_root
            ).as_posix(),
            "layout_profile_sha256": self.layout_pdk.generation.geometry.sha256,
        }
        if self.generator_source_declared:
            payload["generator_source"] = self.generator_source.relative_to(
                self.project_root
            ).as_posix()
            payload["generator_source_sha256"] = self.generator_source_sha256
        if self.generator_dependencies:
            payload["generator_dependencies"] = [
                {
                    "path": path.relative_to(self.project_root).as_posix(),
                    "sha256": sha256,
                }
                for path, sha256 in zip(
                    self.generator_dependencies,
                    self.generator_dependency_sha256s,
                    strict=True,
                )
            ]
        if self.generator_modules:
            payload["generator_modules"] = [
                {"module": module, "sha256": sha256}
                for module, sha256 in zip(
                    self.generator_modules,
                    self.generator_module_sha256s,
                    strict=True,
                )
            ]
        if self.dependency_netlists:
            payload["canonical_sources"] = [
                {
                    "path": snapshot.source_path.relative_to(
                        self.project_root
                    ).as_posix(),
                    "sha256": snapshot.sha256,
                }
                for snapshot in self.source_snapshots
            ]
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            value = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read layout TOML {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"layout TOML root must be a table: {path}")
    return value


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


def _optional_executable(value: Any, field: str, *, base: Path) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty path")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    # Preserve vendor launcher symlinks: Cadence and Calibre wrappers derive
    # their installation root and product name from the invoked pathname.
    result = Path(os.path.abspath(candidate))
    if not result.is_file():
        raise ValueError(f"{field} does not exist: {result}")
    if result.stat().st_mode & 0o111 == 0:
        raise ValueError(f"{field} is not executable: {result}")
    return result


def load_layout_spec(path: Path, *, project_root: Path | None = None) -> LayoutSpec:
    spec_path = path.resolve()
    if project_root is None:
        raise ValueError("project_root or ProjectContext is required for a layout spec")
    context = ProjectContext.from_project_root(project_root)
    root = context.project_root
    try:
        spec_payload = spec_path.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read layout TOML {spec_path}: {exc}") from exc
    try:
        raw = tomllib.loads(spec_payload.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read layout TOML {spec_path}: {exc}") from exc
    ip_root = context.ip_root
    legacy_root = context.legacy_ip_root
    if spec_path.is_relative_to(ip_root) and not spec_path.is_relative_to(legacy_root):
        owner = spec_path.relative_to(ip_root).parts[0].replace("_", "-")
        require_config_header(
            raw,
            spec_path,
            contract_kind="cell-layout",
            path_scope="cell",
            owner=owner,
        )
    layout = _table(raw.get("layout"), "layout")
    ports_table = _table(raw.get("ports"), "ports")

    library = _identifier(layout.get("library"), "layout.library")
    cell = _identifier(layout.get("cell"), "layout.cell")
    view = _identifier(layout.get("view", "layout"), "layout.view")
    generator = _identifier(layout.get("generator"), "layout.generator")
    generator_source_value = layout.get("generator_source")
    generator_source_declared = generator_source_value is not None
    if generator_source_declared:
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
        if dependency.suffix != ".py":
            raise ValueError(
                "layout.generator_dependencies entries must be Python source files"
            )
    generator_dependency_sha256s = tuple(
        hashlib.sha256(path.read_bytes()).hexdigest()
        for path in generator_dependencies
    )
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
        module_spec = importlib.util.find_spec(module)
        origin = None if module_spec is None else module_spec.origin
        if origin is None or origin in {"built-in", "frozen"}:
            raise ValueError(f"layout.generator_modules cannot resolve source: {module}")
        source = Path(origin).resolve()
        if not source.is_file() or source.suffix != ".py":
            raise ValueError(
                f"layout.generator_modules must resolve to Python source: {module}"
            )
        module_sources.append(source)
    generator_module_sources = tuple(module_sources)
    generator_module_sha256s = tuple(
        hashlib.sha256(path.read_bytes()).hexdigest()
        for path in generator_module_sources
    )
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
    # from ``layout.cell``, so historical subckts are not promoted into source.
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

    pdk = load_pdk_config(root, pdk_key)
    pdk_payload = pdk.path.read_bytes()
    pdk_raw = _read_toml(pdk.path)
    layout_pdk_raw = _table(pdk_raw.get("layout"), "pdk.layout")
    dbu = layout_pdk_raw.get("dbu_per_micron")
    if not isinstance(dbu, int) or dbu <= 0:
        raise ValueError("pdk.layout.dbu_per_micron must be a positive integer")
    primitive_values = layout_pdk_raw.get("primitive_masters")
    if not isinstance(primitive_values, list) or not primitive_values:
        raise ValueError("pdk.layout.primitive_masters must be a non-empty array")
    primitive_masters = tuple(
        _identifier(value, "pdk.layout.primitive_masters[]")
        for value in primitive_values
    )
    if len(set(primitive_masters)) != len(primitive_masters):
        raise ValueError("pdk.layout.primitive_masters contains duplicates")
    primitive_subcircuit_raw = layout_pdk_raw.get("primitive_subcircuits", {})
    if not isinstance(primitive_subcircuit_raw, dict):
        raise ValueError("pdk.layout.primitive_subcircuits must be a table")
    primitive_subcircuits: dict[str, tuple[str, ...]] = {}
    for raw_master, raw_terminals in primitive_subcircuit_raw.items():
        master = _identifier(raw_master, "pdk.layout.primitive_subcircuits key")
        if master not in primitive_masters:
            raise ValueError(
                f"primitive subcircuit {master} is not a declared primitive master"
            )
        if not isinstance(raw_terminals, list) or not raw_terminals:
            raise ValueError(
                f"pdk.layout.primitive_subcircuits.{master} must be a terminal array"
            )
        terminals = tuple(
            _identifier(value, f"pdk.layout.primitive_subcircuits.{master}[]")
            for value in raw_terminals
        )
        if len(set(terminals)) != len(terminals):
            raise ValueError(f"primitive subcircuit {master} has duplicate terminals")
        primitive_subcircuits[master] = terminals
    xstream_flatten_pcells = layout_pdk_raw.get("xstream_flatten_pcells", True)
    if not isinstance(xstream_flatten_pcells, bool):
        raise ValueError("pdk.layout.xstream_flatten_pcells must be a boolean")
    raw_suppressed_warnings = layout_pdk_raw.get("xstream_suppressed_warnings", [])
    if not isinstance(raw_suppressed_warnings, list) or not all(
        isinstance(value, str) and value.startswith("XSTRM-")
        and value.removeprefix("XSTRM-").isdigit()
        for value in raw_suppressed_warnings
    ):
        raise ValueError(
            "pdk.layout.xstream_suppressed_warnings must be XSTRM-ID strings"
        )
    xstream_suppressed_warnings = tuple(raw_suppressed_warnings)
    if len(set(xstream_suppressed_warnings)) != len(xstream_suppressed_warnings):
        raise ValueError("pdk.layout.xstream_suppressed_warnings contains duplicates")
    drc_profile_raw = layout_pdk_raw.get("drc_profile", {})
    if not isinstance(drc_profile_raw, dict):
        raise ValueError("pdk.layout.drc_profile must be a table")
    disabled_defines_raw = drc_profile_raw.get("disabled_defines", {})
    if not isinstance(disabled_defines_raw, dict):
        raise ValueError("pdk.layout.drc_profile.disabled_defines must be a table")
    drc_disabled_defines: dict[str, int] = {}
    for raw_name, raw_count in disabled_defines_raw.items():
        name = _identifier(
            raw_name, "pdk.layout.drc_profile.disabled_defines key"
        )
        if isinstance(raw_count, bool) or not isinstance(raw_count, int) or raw_count <= 0:
            raise ValueError(
                "pdk.layout.drc_profile.disabled_defines values must be positive integers"
            )
        drc_disabled_defines[name] = raw_count
    raw_configuration_warnings = drc_profile_raw.get(
        "configuration_warnings", []
    )
    if not isinstance(raw_configuration_warnings, list) or not all(
        isinstance(value, str) and value
        for value in raw_configuration_warnings
    ):
        raise ValueError(
            "pdk.layout.drc_profile.configuration_warnings must be a string array"
        )
    drc_configuration_warnings = tuple(raw_configuration_warnings)
    if len(set(drc_configuration_warnings)) != len(drc_configuration_warnings):
        raise ValueError(
            "pdk.layout.drc_profile.configuration_warnings contains duplicates"
        )
    raw_waiver_layers = drc_profile_raw.get("waiver_layers", [])
    if not isinstance(raw_waiver_layers, list) or not all(
        isinstance(value, str) and value for value in raw_waiver_layers
    ):
        raise ValueError("pdk.layout.drc_profile.waiver_layers must be a string array")
    drc_waiver_layers = tuple(raw_waiver_layers)
    if len(set(drc_waiver_layers)) != len(drc_waiver_layers):
        raise ValueError("pdk.layout.drc_profile.waiver_layers contains duplicates")
    pcell_policy_raw = layout_pdk_raw.get("pcell_policy", {})
    if not isinstance(pcell_policy_raw, dict):
        raise ValueError("pdk.layout.pcell_policy must be a table")

    def optional_policy_identifier(name: str) -> str | None:
        value = pcell_policy_raw.get(name)
        if value is None:
            return None
        return _identifier(value, f"pdk.layout.pcell_policy.{name}")

    def policy_identifier(name: str, default: str) -> str:
        return _identifier(
            pcell_policy_raw.get(name, default),
            f"pdk.layout.pcell_policy.{name}",
        )

    raw_callback_bypass = pcell_policy_raw.get(
        "cdf_callback_bypass_parameters", []
    )
    if not isinstance(raw_callback_bypass, list):
        raise ValueError(
            "pdk.layout.pcell_policy.cdf_callback_bypass_parameters must be an array"
        )
    callback_bypass = tuple(
        _identifier(
            value,
            "pdk.layout.pcell_policy.cdf_callback_bypass_parameters[]",
        )
        for value in raw_callback_bypass
    )
    if len(set(callback_bypass)) != len(callback_bypass):
        raise ValueError(
            "pdk.layout.pcell_policy.cdf_callback_bypass_parameters contains duplicates"
        )
    pcell_policy = PcellPolicy(
        finger_count_parameter=optional_policy_identifier(
            "finger_count_parameter"
        ),
        source_terminal=policy_identifier("source_terminal", "S"),
        drain_terminal=policy_identifier("drain_terminal", "D"),
        source_alias_prefix=policy_identifier("source_alias_prefix", "S_"),
        drain_alias_prefix=policy_identifier("drain_alias_prefix", "D_"),
        cdf_callback_parameter=optional_policy_identifier(
            "cdf_callback_parameter"
        ),
        cdf_callback_bypass_parameters=callback_bypass,
    )
    generation = load_layout_generation_config(
        layout_pdk_raw,
        pdk_path=pdk.path,
    )
    try:
        generation.geometry.path.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            "pdk.layout.generation.profile must stay below the project root"
        ) from exc
    layout_pdk = LayoutPdkConfig(
        configuration_sha256=hashlib.sha256(pdk_payload).hexdigest(),
        dbu_per_micron=dbu,
        layermap=_required_file(
            layout_pdk_raw.get("layermap"),
            "pdk.layout.layermap",
            base=pdk.asset_root or pdk.path.parent,
        ),
        drc_deck=_required_file(
            layout_pdk_raw.get("drc_deck"),
            "pdk.layout.drc_deck",
            base=pdk.asset_root or pdk.path.parent,
        ),
        lvs_deck=_required_file(
            layout_pdk_raw.get("lvs_deck"),
            "pdk.layout.lvs_deck",
            base=pdk.asset_root or pdk.path.parent,
        ),
        qrc_tech_file=_required_file(
            layout_pdk_raw.get("qrc_tech_file"),
            "pdk.layout.qrc_tech_file",
            base=pdk.asset_root or pdk.path.parent,
        ),
        primitive_masters=primitive_masters,
        primitive_subcircuits=primitive_subcircuits,
        drc_disabled_defines=drc_disabled_defines,
        drc_configuration_warnings=drc_configuration_warnings,
        drc_waiver_layers=drc_waiver_layers,
        pcell_policy=pcell_policy,
        generation=generation,
        xstream_flatten_pcells=xstream_flatten_pcells,
        xstream_suppressed_warnings=xstream_suppressed_warnings,
        xstream_bin=_optional_executable(
            layout_pdk_raw.get("xstream_bin"),
            "pdk.layout.xstream_bin",
            base=pdk.path.parent,
        ),
        calibre_bin=_optional_executable(
            layout_pdk_raw.get("calibre_bin"),
            "pdk.layout.calibre_bin",
            base=pdk.path.parent,
        ),
    )
    return LayoutSpec(
        path=spec_path,
        spec_sha256=hashlib.sha256(spec_payload).hexdigest(),
        project_root=root,
        library=library,
        cell=cell,
        view=view,
        generator=generator,
        generator_source=generator_source,
        generator_source_sha256=hashlib.sha256(generator_source.read_bytes()).hexdigest(),
        generator_source_declared=generator_source_declared,
        generator_dependencies=generator_dependencies,
        generator_dependency_sha256s=generator_dependency_sha256s,
        generator_modules=generator_modules,
        generator_module_sources=generator_module_sources,
        generator_module_sha256s=generator_module_sha256s,
        stage=stage,
        source_netlist=source_netlist,
        source_snapshot=source_snapshot,
        source_snapshots=source_snapshots,
        dependency_netlists=dependency_netlists,
        ports=declared_ports,
        directions=directions,
        pdk=pdk,
        layout_pdk=layout_pdk,
    )
