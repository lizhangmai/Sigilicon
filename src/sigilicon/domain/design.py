"""Canonical design and PDK configuration independent of any simulator backend."""

from __future__ import annotations

import re
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from sigilicon.domain.config_contracts import require_config_header
from sigilicon.domain.netlist import NetlistSnapshot, load_netlist_snapshot, subckt_ports
from sigilicon.paths import ProjectContext


IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")
TOKEN_RE = re.compile(r"[A-Za-z0-9_.+\-]+\Z")


@dataclass(frozen=True)
class PdkConfig:
    path: Path
    name: str
    technology_library: str
    reference_libraries: tuple[str, ...]
    model_file: Path
    model_section: str
    asset_root: Path | None = None
    installation_root_environment: str | None = None


@dataclass(frozen=True)
class DesignSpec:
    path: Path
    project_root: Path
    library: str
    cell: str
    sync_mode: str
    source_netlist: Path
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    inouts: tuple[str, ...]
    supplies: tuple[str, ...]
    primary_supply: str
    ground_supply: str
    port_order: tuple[str, ...]
    directions: Mapping[str, str]
    pdk: PdkConfig
    netlist_snapshot: NetlistSnapshot

    @property
    def ports(self) -> tuple[str, ...]:
        return self.port_order


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            value = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read TOML {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"TOML root must be a table: {path}")
    return value


def _table(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a table")
    return value


def _string(value: Any, field: str, *, identifier: bool = False) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    pattern = IDENTIFIER_RE if identifier else TOKEN_RE
    if not pattern.fullmatch(value):
        raise ValueError(f"{field} contains unsupported characters: {value!r}")
    return value


def _names(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} must be a non-empty string array")
    names = tuple(_string(item, f"{field}[]", identifier=True) for item in value)
    if len(set(names)) != len(names):
        raise ValueError(f"{field} contains duplicate names")
    return names


def _optional_names(value: Any, field: str) -> tuple[str, ...]:
    """Load an optional port group while preserving strict name validation."""

    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array of identifiers")
    names = tuple(_string(item, f"{field}[]", identifier=True) for item in value)
    if len(set(names)) != len(names):
        raise ValueError(f"{field} contains duplicate names")
    return names


def _load_pdk(context: ProjectContext, key: str) -> PdkConfig:
    pdk_path = context.platform_config(key)
    raw = _read_toml(pdk_path)
    refs = _names(raw.get("reference_libraries"), "pdk.reference_libraries")
    model_value = raw.get("model_file")
    if not isinstance(model_value, str) or not model_value:
        raise ValueError("pdk.model_file must be a non-empty path")
    installation_environment = raw.get("installation_root_environment")
    package_root_value = raw.get("package_root")
    asset_root: Path | None = None
    if installation_environment is not None or package_root_value is not None:
        if (
            not isinstance(installation_environment, str)
            or re.fullmatch(r"[A-Z][A-Z0-9_]*", installation_environment) is None
        ):
            raise ValueError("pdk.installation_root_environment must be an environment name")
        if not isinstance(package_root_value, str) or not package_root_value:
            raise ValueError("pdk.package_root must be a non-empty relative path")
        package_root = Path(package_root_value)
        if package_root.is_absolute() or ".." in package_root.parts:
            raise ValueError("pdk.package_root must be a safe relative path")
        installation_value = os.environ.get(installation_environment)
        if not installation_value:
            raise ValueError(
                f"PDK installation root environment is unset: {installation_environment}"
            )
        asset_root = (Path(installation_value).expanduser() / package_root).resolve()
    model_file = Path(model_value).expanduser()
    if not model_file.is_absolute():
        model_file = ((asset_root or pdk_path.parent) / model_file).resolve()
    return PdkConfig(
        path=pdk_path,
        name=str(raw.get("name", key)),
        technology_library=_string(
            raw.get("technology_library"),
            "pdk.technology_library",
            identifier=True,
        ),
        reference_libraries=refs,
        model_file=model_file,
        model_section=_string(raw.get("model_section"), "pdk.model_section"),
        asset_root=asset_root,
        installation_root_environment=installation_environment,
    )


def load_pdk_config(project_root: Path, key: str) -> PdkConfig:
    """Load one project PDK declaration for non-schematic flow domains."""

    return _load_pdk(ProjectContext.from_project_root(project_root), key)


def load_design_spec(path: Path, *, project_root: Path | None = None) -> DesignSpec:
    spec_path = path.resolve()
    if project_root is None:
        raise ValueError("project_root or ProjectContext is required for a design spec")
    context = ProjectContext.from_project_root(project_root)
    root = context.project_root
    raw = _read_toml(spec_path)
    ip_root = context.ip_root
    legacy_root = context.legacy_ip_root
    if spec_path.is_relative_to(ip_root) and not spec_path.is_relative_to(legacy_root):
        owner = spec_path.relative_to(ip_root).parts[0].replace("_", "-")
        require_config_header(
            raw,
            spec_path,
            contract_kind="cell-design",
            path_scope="cell",
            owner=owner,
        )
    design = _table(raw.get("design"), "design")
    ports = _table(raw.get("ports"), "ports")
    library = _string(design.get("library"), "design.library", identifier=True)
    cell = _string(design.get("cell"), "design.cell", identifier=True)
    sync_mode = design.get("sync_mode", "recursive")
    if sync_mode not in {"recursive", "target-only"}:
        raise ValueError("design.sync_mode must be recursive or target-only")
    source_value = design.get("source_netlist")
    if not isinstance(source_value, str) or not source_value:
        raise ValueError("design.source_netlist must be a non-empty path")
    source_netlist = Path(os.path.abspath(spec_path.parent / source_value))
    if not source_netlist.is_file():
        raise ValueError(f"source netlist does not exist: {source_netlist}")

    inputs = _optional_names(ports.get("inputs"), "ports.inputs")
    outputs = _optional_names(ports.get("outputs"), "ports.outputs")
    inouts = _optional_names(ports.get("inouts"), "ports.inouts")
    supplies_raw = _names(ports.get("supplies"), "ports.supplies")
    order = _names(ports.get("order"), "ports.order")
    primary_supply = ports.get("primary_supply")
    ground_supply = ports.get("ground_supply")
    if primary_supply is None and ground_supply is None:
        if len(supplies_raw) != 2:
            raise ValueError(
                "multi-domain ports.supplies requires ports.primary_supply "
                "and ports.ground_supply"
            )
        primary_supply, ground_supply = supplies_raw
    elif primary_supply is None or ground_supply is None:
        raise ValueError(
            "ports.primary_supply and ports.ground_supply must be declared together"
        )
    else:
        primary_supply = _string(
            primary_supply, "ports.primary_supply", identifier=True
        )
        ground_supply = _string(
            ground_supply, "ports.ground_supply", identifier=True
        )
    if primary_supply == ground_supply:
        raise ValueError("primary and ground supplies must be different")
    if primary_supply not in supplies_raw or ground_supply not in supplies_raw:
        raise ValueError(
            "ports.primary_supply and ports.ground_supply must both appear in "
            "ports.supplies"
        )
    grouped = inputs + outputs + inouts + supplies_raw
    if not inputs + outputs + inouts:
        raise ValueError("ports must contain at least one signal port")
    if len(set(grouped)) != len(grouped):
        raise ValueError("port names must be unique across inputs, outputs, and supplies")
    if len(order) != len(grouped) or set(order) != set(grouped):
        raise ValueError("ports.order must contain every grouped port exactly once")
    netlist_snapshot = load_netlist_snapshot(source_netlist)
    declared = subckt_ports(netlist_snapshot, cell)
    if declared != order:
        raise ValueError(f"ports.order {order} does not match {cell} declaration {declared}")

    directions_raw = _table(ports.get("directions"), "ports.directions")
    if set(directions_raw) != set(grouped):
        raise ValueError("ports.directions must define every port exactly once")
    allowed = {"input", "output", "inputOutput"}
    directions: dict[str, str] = {}
    for name, direction in directions_raw.items():
        if not isinstance(direction, str) or direction not in allowed:
            raise ValueError(f"invalid direction for {name}: {direction!r}")
        directions[name] = direction

    pdk = _load_pdk(context, _string(design.get("pdk"), "design.pdk"))
    return DesignSpec(
        path=spec_path,
        project_root=root,
        library=library,
        cell=cell,
        sync_mode=sync_mode,
        source_netlist=source_netlist,
        inputs=inputs,
        outputs=outputs,
        inouts=inouts,
        supplies=supplies_raw,
        primary_supply=primary_supply,
        ground_supply=ground_supply,
        port_order=order,
        directions=directions,
        pdk=pdk,
        netlist_snapshot=netlist_snapshot,
    )
