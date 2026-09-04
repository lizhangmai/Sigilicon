"""Canonical design and PDK configuration independent of any simulator adapter."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping

from sigilicon.contracts import (
    freeze_toml_document,
    is_frozen_toml_document,
    read_toml,
    require_config_header,
)
from sigilicon.domain.netlist import NetlistSnapshot, load_netlist_snapshot, subckt_ports
from sigilicon.domain.platform import (
    Platform,
    PlatformSnapshot,
    resolve_platform_snapshot,
)
from sigilicon.domain.context import RepositoryIdentity

if TYPE_CHECKING:
    from sigilicon.project import Project


IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")
TOKEN_RE = re.compile(r"[A-Za-z0-9_.+\-]+\Z")
_MAPPING_PROXY_TYPE = type(MappingProxyType({}))


@dataclass(frozen=True)
class DesignSpec:
    path: Path
    repository: RepositoryIdentity
    library: str
    cell: str
    sync_mode: str
    source_netlist: Path
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    inouts: tuple[str, ...]
    supplies: tuple[str, ...]
    primary_supply: str | None
    ground_supply: str | None
    port_order: tuple[str, ...]
    directions: Mapping[str, str]
    pdk: Platform
    netlist_snapshot: NetlistSnapshot
    source_documents: Mapping[Path, Mapping[str, Any]] = field(
        default_factory=lambda: MappingProxyType({})
    )

    @property
    def project_root(self) -> Path:
        return self.repository.project_root

    @property
    def workspace_root(self) -> Path:
        return self.repository.workspace_root

    @property
    def ports(self) -> tuple[str, ...]:
        return self.port_order


def _read_toml(path: Path) -> dict[str, Any]:
    return read_toml(path)


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


def load_design_spec(
    path: Path,
    *,
    project: Project,
    platform: PlatformSnapshot | None = None,
    netlist_snapshot: NetlistSnapshot | None = None,
) -> DesignSpec:
    spec_path = path.resolve()
    repository = project
    root = repository.project_root
    raw = _read_toml(spec_path)
    if repository.owner_for(spec_path) is not None:
        require_config_header(
            raw,
            spec_path,
            contract_kind="cell-design",
            path_scope="cell",
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
    source_netlist = (spec_path.parent / source_value).resolve()
    if not source_netlist.is_file():
        raise ValueError(f"source netlist does not exist: {source_netlist}")
    if not source_netlist.is_relative_to(root):
        raise ValueError("design.source_netlist must stay below the project root")
    owner = repository.owner_for(spec_path)
    if owner is not None and not source_netlist.is_relative_to(owner.root):
        raise ValueError("design.source_netlist must stay inside its cataloged owner")

    inputs = _optional_names(ports.get("inputs"), "ports.inputs")
    outputs = _optional_names(ports.get("outputs"), "ports.outputs")
    inouts = _optional_names(ports.get("inouts"), "ports.inouts")
    supplies_raw = _names(ports.get("supplies"), "ports.supplies")
    order = _names(ports.get("order"), "ports.order")
    primary_supply = ports.get("primary_supply")
    ground_supply = ports.get("ground_supply")
    if primary_supply is None and ground_supply is None:
        if len(supplies_raw) == 2:
            primary_supply, ground_supply = supplies_raw
        elif len(supplies_raw) == 1:
            raise ValueError(
                "single-rail ports.supplies requires either ports.primary_supply "
                "or ports.ground_supply"
            )
        else:
            raise ValueError(
                "multi-domain ports.supplies requires ports.primary_supply "
                "and ports.ground_supply"
            )
    else:
        if primary_supply is not None:
            primary_supply = _string(
                primary_supply, "ports.primary_supply", identifier=True
            )
        if ground_supply is not None:
            ground_supply = _string(
                ground_supply, "ports.ground_supply", identifier=True
            )
    if len(supplies_raw) > 1 and (
        primary_supply is None or ground_supply is None
    ):
        raise ValueError(
            "ports.primary_supply and ports.ground_supply must be declared together"
        )
    if primary_supply is not None and primary_supply == ground_supply:
        raise ValueError("primary and ground supplies must be different")
    if any(
        supply is not None and supply not in supplies_raw
        for supply in (primary_supply, ground_supply)
    ):
        raise ValueError(
            "declared primary or ground supply must appear in ports.supplies"
        )
    grouped = inputs + outputs + inouts + supplies_raw
    if not inputs + outputs + inouts:
        raise ValueError("ports must contain at least one signal port")
    if len(set(grouped)) != len(grouped):
        raise ValueError("port names must be unique across inputs, outputs, and supplies")
    if len(order) != len(grouped) or set(order) != set(grouped):
        raise ValueError("ports.order must contain every grouped port exactly once")
    selected_netlist = (
        load_netlist_snapshot(source_netlist)
        if netlist_snapshot is None
        else netlist_snapshot
    )
    if selected_netlist.source_path != source_netlist:
        raise ValueError("design netlist snapshot identity drift")
    declared = subckt_ports(selected_netlist, cell)
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

    pdk = resolve_platform_snapshot(
        repository,
        _string(design.get("pdk"), "design.pdk"),
        snapshot=platform,
    )
    return DesignSpec(
        path=spec_path,
        repository=RepositoryIdentity.capture(repository),
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
        netlist_snapshot=selected_netlist,
        source_documents=MappingProxyType(
            {spec_path: freeze_toml_document(raw)}
        ),
    )


def resolve_design_spec(
    path: Path,
    *,
    project: Project,
    snapshot: DesignSpec | None = None,
) -> DesignSpec:
    """Load a design spec or validate one operation-owned snapshot."""

    if snapshot is None:
        return load_design_spec(path, project=project)
    spec_path = path.resolve()
    root = project.project_root
    if (
        snapshot.path != spec_path
        or not spec_path.is_relative_to(root)
        or not spec_path.is_file()
    ):
        raise ValueError("design snapshot identity drift")
    snapshot.repository.validate(project)
    owner = project.owner_for(spec_path)
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
        raise ValueError("design snapshot source document identity drift")
    raw = snapshot.source_documents[spec_path]
    if (
        not isinstance(snapshot.source_documents, _MAPPING_PROXY_TYPE)
        or not is_frozen_toml_document(raw)
    ):
        raise ValueError("design snapshot source document drift: mutable snapshot")
    if owner is not None:
        require_config_header(
            raw,
            spec_path,
            contract_kind="cell-design",
            path_scope="cell",
            owner=owner.name,
        )
    design = raw.get("design")
    ports = raw.get("ports")
    if not isinstance(design, Mapping) or not isinstance(ports, Mapping):
        raise ValueError("design snapshot source document drift")
    source_value = design.get("source_netlist")
    source_path = (
        None
        if not isinstance(source_value, str) or not source_value
        else (spec_path.parent / source_value).resolve()
    )
    supplies = ports.get("supplies")
    primary_supply = ports.get("primary_supply")
    ground_supply = ports.get("ground_supply")
    if (
        primary_supply is None
        and ground_supply is None
        and isinstance(supplies, (list, tuple))
        and len(supplies) == 2
    ):
        primary_supply, ground_supply = supplies
    if (
        design.get("library") != snapshot.library
        or design.get("cell") != snapshot.cell
        or design.get("sync_mode", "recursive") != snapshot.sync_mode
        or design.get("pdk") != snapshot.pdk.key
        or source_path != snapshot.source_netlist
        or snapshot.source_netlist != snapshot.source_netlist.resolve()
        or not snapshot.source_netlist.is_file()
        or snapshot.netlist_snapshot.source_path != snapshot.source_netlist
        or not snapshot.source_netlist.is_relative_to(root)
        or (
            owner is not None
            and not snapshot.source_netlist.is_relative_to(owner.root)
        )
        or tuple(ports.get("inputs") or ()) != snapshot.inputs
        or tuple(ports.get("outputs") or ()) != snapshot.outputs
        or tuple(ports.get("inouts") or ()) != snapshot.inouts
        or tuple(supplies or ()) != snapshot.supplies
        or tuple(ports.get("order", ())) != snapshot.port_order
        or primary_supply != snapshot.primary_supply
        or ground_supply != snapshot.ground_supply
        or ports.get("directions") != snapshot.directions
    ):
        raise ValueError("design snapshot source document drift")
    return snapshot
