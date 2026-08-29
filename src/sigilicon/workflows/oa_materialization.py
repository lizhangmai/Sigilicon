"""Production OA/Virtuoso plus XStream materialization ToolAdapter."""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import stat
import tomllib
from types import MappingProxyType
from typing import Any, Callable, Mapping

from sigilicon.artifacts import atomic_write_json, read_nofollow_text
from sigilicon.domain.platform import (
    OaLayerPurposeMapping,
    load_oa_materialization_mapping,
)
from sigilicon.flow.environment import capability_available
from sigilicon.flow.model import (
    ActionContext,
    AdapterExecution,
    CollectedActionResult,
    FlowExecutionError,
    ResolvedPlatformAsset,
)
from sigilicon.layout.materialization import MaterializationPlan
from sigilicon.layout.materialization_execution import (
    LayoutArtifactFormat,
    MaterializationCompletion,
    MaterializationExecutionStatus,
    MaterializationExecutionTarget,
    materialization_receipt_from_json,
    validate_layout_content,
    validate_materialization_request,
)
from sigilicon.layout.pnr._geometry import transform_sized_rect
from sigilicon.layout.pnr.model import (
    PhysicalDesignJob,
    PhysicalDesignResult,
    Rect,
)
from sigilicon.layout.pnr.serialization import (
    physical_design_job_id,
    physical_design_result_id,
)
from sigilicon.paths import ProjectContext
from sigilicon.virtuoso.bridge import skill_quote
from sigilicon.virtuoso.client import get_client
from sigilicon.virtuoso.library import ensure_project_library
from sigilicon.virtuoso.oa import execute_owned_cellview_skill
from sigilicon.virtuoso.workspace import OperationPolicy, workspace_operation
from sigilicon.virtuoso.xstream import (
    XStreamExportError,
    XStreamExportRequest,
    canonicalize_xstream_gdsii,
    run_xstream_export,
)
from sigilicon.workflows.physical_design import (
    collect_materialization_execution_result,
    materialization_execution_facts,
    read_materialization_execution_request,
    write_materialization_receipt,
)


_HEADER_FIELDS = {"schema", "contract_kind", "path_scope", "owner"}
_OA_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")
_BACKEND = "sigilicon.oa-virtuoso-xstream"
_CAPABILITIES = (
    "tool.virtuoso-bridge",
    "tool.xstream",
    "license.cadence-oa",
)
_ASSET_MEMBERS = (
    "oa-target",
    "technology-library",
    "layer-map",
    "master-layouts",
    "via-map",
    "xstream-layer-map",
    "xstream-options",
)


def _reject_unknown(raw: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise FlowExecutionError(f"{label} contains unknown fields: {sorted(unknown)}")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        raise FlowExecutionError(f"{label} must be non-empty text")
    return value


def _oa_name(value: object, label: str) -> str:
    result = _text(value, label)
    if _OA_NAME.fullmatch(result) is None:
        raise FlowExecutionError(f"{label} must be a legal OA identifier")
    return result


def _table(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FlowExecutionError(f"{label} must be a table")
    return value


def _read_toml(path: Path, label: str) -> Mapping[str, Any]:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
            raise FlowExecutionError(f"{label} must be a regular file")
        return tomllib.loads(read_nofollow_text(path))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise FlowExecutionError(f"cannot read {label}: {exc}") from exc


def _header(
    raw: Mapping[str, Any],
    *,
    kind: str,
    scope: str,
    label: str,
) -> str:
    if raw.get("schema") != 1:
        raise FlowExecutionError(f"{label} must use schema 1")
    if raw.get("contract_kind") != kind:
        raise FlowExecutionError(f"{label} contract_kind must be {kind!r}")
    if raw.get("path_scope") != scope:
        raise FlowExecutionError(f"{label} path_scope must be {scope!r}")
    return _text(raw.get("owner"), f"{label} owner")


@dataclass(frozen=True)
class _AdapterConfiguration:
    project: ProjectContext
    oa_timeout_seconds: int
    xstream_timeout_seconds: int


@dataclass(frozen=True)
class _OaTarget:
    owner: str
    name: str
    library: str
    cell: str
    view: str
    library_path: Path
    replace_existing: bool


@dataclass(frozen=True)
class _MasterMapping:
    library: str
    cell: str
    view: str


@dataclass(frozen=True)
class _MaterializationAssets:
    target: _OaTarget
    technology_library: str
    dbu_per_micron: int
    layers: Mapping[str, OaLayerPurposeMapping]
    masters: Mapping[str, _MasterMapping]
    vias: Mapping[str, str]
    xstream_layer_map: Path
    flatten_pcells: bool
    suppressed_warnings: tuple[str, ...]


def _configuration(context: ActionContext) -> _AdapterConfiguration:
    allowed = {
        "oa_timeout_seconds",
        "xstream_timeout_seconds",
    }
    _reject_unknown(context.adapter_config, allowed, "OA materialization Adapter config")
    project = context.require_project_scope().project
    timeouts: list[int] = []
    for name, default in (
        ("oa_timeout_seconds", 120),
        ("xstream_timeout_seconds", 120),
    ):
        value = context.adapter_config.get(name, default)
        if type(value) is not int or value <= 0:
            raise FlowExecutionError(f"{name} must be a positive integer")
        timeouts.append(value)
    return _AdapterConfiguration(project, *timeouts)


def _asset(context: ActionContext) -> ResolvedPlatformAsset:
    asset = context.platform_assets.get("physical-layout")
    if asset is None or asset.kind != "platform.layout-view-set":
        raise FlowExecutionError(
            "OA materialization requires a physical-layout platform view"
        )
    missing = [role for role in _ASSET_MEMBERS if asset.member(role) is None]
    if missing:
        raise FlowExecutionError(
            f"physical-layout platform view omitted members: {missing}"
        )
    return asset


def _member(asset: ResolvedPlatformAsset, role: str) -> Path:
    member = asset.member(role)
    assert member is not None
    path = member.location
    if not path.is_file() or path.is_symlink():
        raise FlowExecutionError(
            f"physical-layout member {role!r} is not a regular file"
        )
    return path


def _load_target(
    path: Path,
    *,
    project_root: Path,
    workspace_root: Path,
) -> tuple[_OaTarget, Mapping[str, _MasterMapping]]:
    raw = _read_toml(path, "OA materialization target")
    owner = _header(
        raw,
        kind="physical-materialization-target",
        scope="owner",
        label="OA materialization target",
    )
    _reject_unknown(
        raw,
        _HEADER_FIELDS
        | {
            "name",
            "library",
            "cell",
            "view",
            "library_path",
            "managed_scratch",
            "replace_existing",
            "masters",
        },
        "OA materialization target",
    )
    if raw.get("managed_scratch") is not True:
        raise FlowExecutionError(
            "OA materialization target must explicitly declare managed_scratch = true"
        )
    relative_text = _text(raw.get("library_path"), "OA target library_path")
    relative = Path(relative_text)
    if relative.is_absolute() or ".." in relative.parts:
        raise FlowExecutionError("OA target library_path must be project-relative")
    library_candidate = project_root / relative
    try:
        candidate_metadata = library_candidate.lstat()
    except FileNotFoundError:
        candidate_metadata = None
    except OSError as exc:
        raise FlowExecutionError(
            f"managed OA target library cannot be inspected: {exc}"
        ) from exc
    if candidate_metadata is not None and stat.S_ISLNK(candidate_metadata.st_mode):
        raise FlowExecutionError("managed OA target library must not be a symlink")
    library_path = library_candidate.resolve()
    if not library_path.is_relative_to(workspace_root):
        raise FlowExecutionError("OA target library must stay inside the workspace")
    if candidate_metadata is None:
        # A production run may create this explicitly declared managed scratch
        # library.  Existing targets still have to be real, non-symlink
        # directories before the bridge can be contacted.
        metadata = None
    else:
        metadata = candidate_metadata
    if metadata is not None and not stat.S_ISDIR(metadata.st_mode):
        raise FlowExecutionError("managed OA target library must be a real directory")
    replace_existing = raw.get("replace_existing")
    if type(replace_existing) is not bool:
        raise FlowExecutionError("OA target replace_existing must be boolean")
    masters_raw = _table(raw.get("masters", {}), "OA target masters")
    masters: dict[str, _MasterMapping] = {}
    for logical, value in masters_raw.items():
        logical_name = _text(logical, "logical master")
        item = _table(value, f"masters.{logical_name}")
        _reject_unknown(item, {"library", "cell", "view"}, f"masters.{logical_name}")
        masters[logical_name] = _MasterMapping(
            _oa_name(item.get("library"), f"masters.{logical_name}.library"),
            _oa_name(item.get("cell"), f"masters.{logical_name}.cell"),
            _oa_name(item.get("view"), f"masters.{logical_name}.view"),
        )
    return (
        _OaTarget(
            owner=owner,
            name=_text(raw.get("name"), "OA target name"),
            library=_oa_name(raw.get("library"), "OA target library"),
            cell=_oa_name(raw.get("cell"), "OA target cell"),
            view=_oa_name(raw.get("view"), "OA target view"),
            library_path=library_path,
            replace_existing=replace_existing,
        ),
        MappingProxyType(masters),
    )


def _load_technology_library(path: Path) -> str:
    raw = _read_toml(path, "OA technology-library asset")
    _header(raw, kind="platform-oa", scope="platform", label="OA platform asset")
    _reject_unknown(
        raw,
        _HEADER_FIELDS
        | {"technology_library", "reference_libraries", "primitive_subcircuits"},
        "OA platform asset",
    )
    return _oa_name(raw.get("technology_library"), "OA technology library")


def _load_geometry_mapping(
    layer_path: Path,
    via_path: Path,
) -> tuple[int, Mapping[str, OaLayerPurposeMapping], Mapping[str, str]]:
    if layer_path != via_path:
        raise FlowExecutionError(
            "layer-map and via-map must select one atomic platform layout contract"
        )
    try:
        dbu, mapping = load_oa_materialization_mapping(layer_path)
    except (OSError, ValueError, TypeError) as exc:
        raise FlowExecutionError(
            f"invalid OA geometry mapping asset: {exc}"
        ) from exc
    return dbu, mapping.layers, mapping.vias


def _load_xstream_options(path: Path) -> tuple[bool, tuple[str, ...]]:
    raw = _read_toml(path, "XStream options asset")
    _header(
        raw,
        kind="platform-verification",
        scope="platform",
        label="XStream options asset",
    )
    _reject_unknown(
        raw,
        _HEADER_FIELDS
        | {
            "layermap",
            "drc_deck",
            "lvs_deck",
            "qrc_tech_file",
            "xstream_flatten_pcells",
            "xstream_suppressed_warnings",
            "xstream_bin",
            "calibre_bin",
        },
        "XStream options asset",
    )
    flatten = raw.get("xstream_flatten_pcells", True)
    if type(flatten) is not bool:
        raise FlowExecutionError("xstream_flatten_pcells must be boolean")
    warnings_raw = raw.get("xstream_suppressed_warnings", [])
    if not isinstance(warnings_raw, list) or any(
        not isinstance(item, str) or re.fullmatch(r"XSTRM-[0-9]+", item) is None
        for item in warnings_raw
    ):
        raise FlowExecutionError(
            "xstream_suppressed_warnings must contain XSTRM-<number> identities"
        )
    warnings = tuple(warnings_raw)
    if len(set(warnings)) != len(warnings):
        raise FlowExecutionError("xstream_suppressed_warnings contains duplicates")
    return flatten, warnings


def _load_assets(
    context: ActionContext,
    configuration: _AdapterConfiguration,
) -> _MaterializationAssets:
    asset = _asset(context)
    project = configuration.project
    target, masters = _load_target(
        _member(asset, "oa-target"),
        project_root=project.project_root,
        workspace_root=project.workspace_root,
    )
    dbu, layers, vias = _load_geometry_mapping(
        _member(asset, "layer-map"),
        _member(asset, "via-map"),
    )
    master_path = _member(asset, "master-layouts")
    if master_path != _member(asset, "oa-target"):
        raise FlowExecutionError(
            "master-layouts must be owned atomically by the OA target contract"
        )
    flatten, warnings = _load_xstream_options(_member(asset, "xstream-options"))
    return _MaterializationAssets(
        target=target,
        technology_library=_load_technology_library(
            _member(asset, "technology-library")
        ),
        dbu_per_micron=dbu,
        layers=layers,
        masters=masters,
        vias=vias,
        xstream_layer_map=_member(asset, "xstream-layer-map"),
        flatten_pcells=flatten,
        suppressed_warnings=warnings,
    )


def _support_issues(
    job: PhysicalDesignJob,
    plan: MaterializationPlan,
    assets: _MaterializationAssets,
) -> tuple[str, ...]:
    issues: list[str] = []
    target = assets.target
    if (target.owner, target.name, target.cell) != (
        plan.target.owner,
        plan.target.name,
        plan.target.name,
    ):
        issues.append("OA target owner/name/cell does not match the Materialization Plan")
    if job.technology.dbu_per_micron != assets.dbu_per_micron:
        issues.append("platform OA mapping DBU does not match the canonical job")
    missing_masters = sorted(
        {item.master for item in plan.instances} - set(assets.masters)
    )
    if missing_masters:
        issues.append(f"master layouts are unmapped: {missing_masters}")
    used_layers = {
        item.layer for item in plan.route_segments
    } | {
        shape.layer
        for blockage in job.design.routing_blockages
        for shape in blockage.shapes
    } | {
        access.layer for port in job.design.ports for access in port.accesses
    }
    missing_layers = sorted(used_layers - set(assets.layers))
    if missing_layers:
        issues.append(f"OA layer-purpose mappings are missing: {missing_layers}")
    missing_vias = sorted(
        {item.via_definition for item in plan.route_vias} - set(assets.vias)
    )
    if missing_vias:
        issues.append(f"OA via mappings are missing: {missing_vias}")
    port_nets: dict[str, set[str]] = {port.name: set() for port in job.design.ports}
    for net in job.design.nets:
        for pin in net.pins:
            if pin.instance is None and pin.pin in port_nets:
                port_nets[pin.pin].add(net.name)
    ambiguous = sorted(name for name, nets in port_nets.items() if len(nets) > 1)
    if ambiguous:
        issues.append(f"top-level ports belong to multiple nets: {ambiguous}")
    return tuple(issues)


def _micron(value: int, dbu_per_micron: int) -> str:
    result = value / dbu_per_micron
    if not math.isfinite(result):
        raise FlowExecutionError("OA coordinate is not finite")
    return f"{result:.9f}".rstrip("0").rstrip(".") or "0"


def _lpp(mapping: OaLayerPurposeMapping, purpose: str) -> str:
    return f"list({skill_quote(mapping.layer)} {skill_quote(purpose)})"


def _property(identity: str) -> tuple[str, str]:
    return (
        "unless(fig error(%s))" % skill_quote(f"cannot materialize {identity}"),
        "dbReplaceProp(fig \"sigiliconIdentity\" \"string\" %s)"
        % skill_quote(identity),
    )


def _net_statements(net: str) -> tuple[str, ...]:
    return (
        "net = dbFindNetByName(cv %s)" % skill_quote(net),
        "unless(net net = dbCreateNet(cv %s))" % skill_quote(net),
        "unless(net error(%s))" % skill_quote(f"cannot create materialized net {net}"),
    )


def render_oa_materialization_skill(
    job: PhysicalDesignJob,
    result: PhysicalDesignResult,
    plan: MaterializationPlan,
    assets: _MaterializationAssets,
) -> str:
    """Render one exact, auditable OA transaction from canonical typed artifacts."""

    statements: list[str] = []
    masters = {item.name: item for item in job.design.masters}
    blockages = {item.name: item for item in job.design.routing_blockages}
    for item in plan.instances:
        mapping = assets.masters[item.master]
        master = masters[item.master]
        expected_terminals = " ".join(
            skill_quote(pin.name) for pin in sorted(master.pins, key=lambda pin: pin.name)
        )
        statements.extend(
            (
                "master = dbOpenCellViewByType(%s %s %s \"\" \"r\")"
                % (
                    skill_quote(mapping.library),
                    skill_quote(mapping.cell),
                    skill_quote(mapping.view),
                ),
                "unless(master error(%s))"
                % skill_quote(
                    f"cannot open mapped master {mapping.library}/{mapping.cell}/{mapping.view}"
                ),
                "unless(master~>shapes error(%s))"
                % skill_quote(f"mapped master has no geometry: {item.master}"),
                "terminals = sort(foreach(mapcar terminal master~>terminals terminal~>name) 'alphalessp)",
                "unless(equal(terminals list(%s)) error(%s))"
                % (
                    expected_terminals,
                    skill_quote(f"mapped master terminals mismatch: {item.master}"),
                ),
                "fig = dbCreateInst(cv master %s list(%s %s) %s)"
                % (
                    skill_quote(item.instance),
                    _micron(item.placement.origin.x, assets.dbu_per_micron),
                    _micron(item.placement.origin.y, assets.dbu_per_micron),
                    skill_quote(item.placement.orientation.value),
                ),
                *_property(f"instance:{item.instance}"),
                "unless(dbClose(master) error(%s))"
                % skill_quote(f"cannot close mapped master for {item.instance}"),
                "master = nil",
            )
        )
    for item in plan.routing_blockages:
        source = blockages[item.blockage]
        for index, shape in enumerate(source.shapes):
            rectangle = transform_sized_rect(
                shape.shape,
                source.width_dbu,
                source.height_dbu,
                item.placement,
            )
            mapping = assets.layers[shape.layer]
            identity = f"blockage:{item.blockage}:shape:{index}"
            statements.extend(
                (
                    "fig = dbCreateRect(cv %s list(list(%s %s) list(%s %s)))"
                    % (
                        _lpp(mapping, mapping.blockage_purpose),
                        _micron(rectangle.x_min, assets.dbu_per_micron),
                        _micron(rectangle.y_min, assets.dbu_per_micron),
                        _micron(rectangle.x_max, assets.dbu_per_micron),
                        _micron(rectangle.y_max, assets.dbu_per_micron),
                    ),
                    *_property(identity),
                )
            )
    for item in plan.route_segments:
        mapping = assets.layers[item.layer]
        statements.extend(_net_statements(item.net))
        statements.extend(
            (
                "fig = dbCreatePath(cv %s list(list(%s %s) list(%s %s)) %s)"
                % (
                    _lpp(mapping, mapping.drawing_purpose),
                    _micron(item.start.x, assets.dbu_per_micron),
                    _micron(item.start.y, assets.dbu_per_micron),
                    _micron(item.end.x, assets.dbu_per_micron),
                    _micron(item.end.y, assets.dbu_per_micron),
                    _micron(item.width_dbu, assets.dbu_per_micron),
                ),
                "when(fig fig~>net = net)",
                *_property(item.identity),
            )
        )
    for item in plan.route_vias:
        statements.extend(_net_statements(item.net))
        statements.extend(
            (
                "viaDef = techFindViaDefByName(techGetTechFile(cv) %s)"
                % skill_quote(assets.vias[item.via_definition]),
                "unless(viaDef error(%s))"
                % skill_quote(f"cannot find mapped via {item.via_definition}"),
                "fig = dbCreateVia(cv viaDef list(%s %s) \"R0\")"
                % (
                    _micron(item.origin.x, assets.dbu_per_micron),
                    _micron(item.origin.y, assets.dbu_per_micron),
                ),
                "when(fig fig~>net = net)",
                *_property(item.identity),
            )
        )
    port_nets: dict[str, str] = {port.name: port.name for port in job.design.ports}
    for net in job.design.nets:
        for pin in net.pins:
            if pin.instance is None:
                port_nets[pin.pin] = net.name
    for port in job.design.ports:
        net_name = port_nets[port.name]
        statements.extend(_net_statements(net_name))
        statements.extend(
            (
                "term = dbFindTermByName(cv %s)" % skill_quote(port.name),
                "unless(term term = dbCreateTerm(net %s \"inputOutput\"))"
                % skill_quote(port.name),
                "unless(term error(%s))"
                % skill_quote(f"cannot create top-level terminal {port.name}"),
            )
        )
        for index, access in enumerate(port.accesses):
            mapping = assets.layers[access.layer]
            rectangle = access.shape
            identity = f"port:{port.name}:access:{index}"
            statements.extend(
                (
                    "fig = dbCreateRect(cv %s list(list(%s %s) list(%s %s)))"
                    % (
                        _lpp(mapping, mapping.drawing_purpose),
                        _micron(rectangle.x_min, assets.dbu_per_micron),
                        _micron(rectangle.y_min, assets.dbu_per_micron),
                        _micron(rectangle.x_max, assets.dbu_per_micron),
                        _micron(rectangle.y_max, assets.dbu_per_micron),
                    ),
                    *_property(identity),
                    "pinFig = dbCreateRect(cv %s list(list(%s %s) list(%s %s)))"
                    % (
                        _lpp(mapping, mapping.pin_purpose),
                        _micron(rectangle.x_min, assets.dbu_per_micron),
                        _micron(rectangle.y_min, assets.dbu_per_micron),
                        _micron(rectangle.x_max, assets.dbu_per_micron),
                        _micron(rectangle.y_max, assets.dbu_per_micron),
                    ),
                    "unless(pinFig error(%s))"
                    % skill_quote(f"cannot create top-level pin figure {port.name}"),
                    "pinObj = dbCreatePin(net pinFig)",
                    "unless(pinObj error(%s))"
                    % skill_quote(f"cannot create top-level pin {port.name}"),
                )
            )
    for name, value in (
        ("sigiliconJobIdentity", physical_design_job_id(job)),
        ("sigiliconResultIdentity", physical_design_result_id(result)),
        ("sigiliconPlanIdentity", plan.artifact_id),
        ("sigiliconTargetOwner", plan.target.owner),
        ("sigiliconTargetName", plan.target.name),
    ):
        statements.append(
            "dbReplaceProp(cv %s \"string\" %s)"
            % (skill_quote(name), skill_quote(value))
        )
    statements.extend(
        (
            'unless(dbSave(cv) error("materialized OA layout save failed"))',
            'result = "t"',
        )
    )
    body = "\n      ".join(statements)
    target = assets.target
    existing = (
        ""
        if target.replace_existing
        else "when(ddGetObj(%s %s %s) error(\"managed materialization view exists\"))"
        % (
            skill_quote(target.library),
            skill_quote(target.cell),
            skill_quote(target.view),
        )
    )
    open_mode = "w" if target.replace_existing else "a"
    return f'''prog((cv master fig pinFig viaDef net term pinObj terminal terminals result)
  cv = nil
  master = nil
  {existing}
  unwindProtect(
    progn(
      cv = dbOpenCellViewByType({skill_quote(target.library)} {skill_quote(target.cell)}
        {skill_quote(target.view)} "maskLayout" {skill_quote(open_mode)})
      unless(cv error("cannot create managed materialization target"))
      {body})
    progn(
      when(master unless(dbClose(master) error("mapped master close failed")) master = nil)
      when(cv unless(dbClose(cv) error("materialized OA layout close failed")) cv = nil)))
  return(result)
)'''


def _expected_identities(
    job: PhysicalDesignJob,
    plan: MaterializationPlan,
) -> tuple[str, ...]:
    blockages = {item.name: item for item in job.design.routing_blockages}
    return tuple(
        sorted(
            (
                *(f"instance:{item.instance}" for item in plan.instances),
                *(
                    f"blockage:{item.blockage}:shape:{index}"
                    for item in plan.routing_blockages
                    for index, _shape in enumerate(blockages[item.blockage].shapes)
                ),
                *(item.identity for item in plan.route_segments),
                *(item.identity for item in plan.route_vias),
                *(
                    f"port:{port.name}:access:{index}"
                    for port in job.design.ports
                    for index, _access in enumerate(port.accesses)
                ),
            )
        )
    )


def render_oa_materialization_validation_skill(
    job: PhysicalDesignJob,
    result: PhysicalDesignResult,
    plan: MaterializationPlan,
    target: _OaTarget,
) -> str:
    expected = "list(" + " ".join(
        skill_quote(item) for item in _expected_identities(job, plan)
    ) + ")"
    properties = (
        ("sigiliconJobIdentity", physical_design_job_id(job)),
        ("sigiliconResultIdentity", physical_design_result_id(result)),
        ("sigiliconPlanIdentity", plan.artifact_id),
        ("sigiliconTargetOwner", plan.target.owner),
        ("sigiliconTargetName", plan.target.name),
    )
    checks = "\n      ".join(
        "unless(equal(dbGetq(cv %s) %s) error(%s))"
        % (
            name,
            skill_quote(value),
            skill_quote(f"materialized OA property mismatch: {name}"),
        )
        for name, value in properties
    )
    return f'''prog((cv expected actual item identity result)
  cv = nil
  unwindProtect(
    progn(
      cv = dbOpenCellViewByType({skill_quote(target.library)} {skill_quote(target.cell)}
        {skill_quote(target.view)} "maskLayout" "r")
      unless(cv error("cannot open materialized OA layout for validation"))
      {checks}
      expected = sort(copy({expected}) 'alphalessp)
      actual = nil
      foreach(item append(cv~>instances cv~>shapes)
        identity = dbGetq(item sigiliconIdentity)
        when(identity actual = cons(identity actual)))
      actual = sort(actual 'alphalessp)
      unless(equal(expected actual)
        error(sprintf(nil "materialized OA identities mismatch: %L" actual)))
      result = t)
    when(cv unless(dbClose(cv) error("materialized OA validation close failed")) cv = nil))
  return(result)
)'''


def _bridge_execute(
    client: Any,
    operation: Any,
    source: str,
    *,
    label: str,
    target: _OaTarget,
    timeout: int,
    mutation: bool,
) -> None:
    output = execute_owned_cellview_skill(
        client,
        operation,
        source,
        label=label,
        library=target.library,
        cell=target.cell,
        view=target.view,
        timeout=timeout,
        mutation=mutation,
    )
    if output != "t":
        raise RuntimeError(f"{label} was not confirmed: {output}")


def _write_regular_copy(source: Path, destination: Path, label: str) -> None:
    destination.write_bytes(_read_regular_bytes(source, label))


def _read_regular_bytes(source: Path, label: str) -> bytes:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            source,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(f"{label} is not a regular file")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise RuntimeError(f"{label} changed while it was read")
    except OSError as exc:
        raise RuntimeError(f"cannot inspect {label}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return b"".join(chunks)


class OaXStreamMaterializationAdapter:
    """Materialize canonical plans through one managed OA target and XStream."""

    def __init__(
        self,
        *,
        _client_factory: Callable[[], Any] = get_client,
    ) -> None:
        self._client_factory = _client_factory

    def _inputs(
        self,
        context: ActionContext,
    ) -> tuple[
        PhysicalDesignJob,
        PhysicalDesignResult,
        MaterializationPlan,
        MaterializationExecutionTarget,
        _AdapterConfiguration,
        _MaterializationAssets,
    ]:
        job, result, plan, target = read_materialization_execution_request(context)
        if target.format is not LayoutArtifactFormat.GDSII:
            raise FlowExecutionError("OA/XStream Adapter supports only GDSII")
        configuration = _configuration(context)
        assets = _load_assets(context, configuration)
        if (assets.target.owner, assets.target.name) != (target.owner, target.name):
            raise FlowExecutionError(
                "OA target asset does not match the explicit execution target"
            )
        return job, result, plan, target, configuration, assets

    def _resources(self, context: ActionContext) -> tuple[str, Path]:
        missing = [name for name in _CAPABILITIES if name not in context.capabilities]
        if missing:
            raise FlowExecutionError(
                f"OA/XStream Adapter capabilities are missing: {missing}"
            )
        xstream = context.capabilities["tool.xstream"]
        if xstream.executable is None:
            raise FlowExecutionError(
                "XStream capability requires a resolved executable"
            )
        if not capability_available(xstream):
            raise FlowExecutionError("resolved XStream executable is unavailable")
        bridge = context.capabilities["tool.virtuoso-bridge"]
        license_capability = context.capabilities["license.cadence-oa"]
        return (
            "+".join((bridge.identity, xstream.identity, license_capability.identity)),
            xstream.executable,
        )

    def validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        diagnostics: list[str] = []
        try:
            (
                job,
                result,
                plan,
                target,
                _configuration_value,
                _assets_value,
            ) = self._inputs(context)
            validate_materialization_request(
                job,
                result,
                plan,
                target,
            )
            self._resources(context)
        except (FlowExecutionError, OSError, ValueError, TypeError) as exc:
            diagnostics.append(str(exc))
        return tuple(diagnostics)

    def prepare(self, context: ActionContext) -> None:
        (context.log_root / "oa-xstream").mkdir()

    def _receipt(
        self,
        context: ActionContext,
        *,
        status: MaterializationExecutionStatus,
        backend: str,
        executed: bool,
        completed: bool,
        content_validated: bool,
        exit_code: int | None,
        message: str,
        layout_path: Path | None = None,
    ) -> None:
        receipt = write_materialization_receipt(
            context,
            status=status,
            completion=MaterializationCompletion(
                backend=backend,
                executed=executed,
                completed=completed,
                content_validated=content_validated,
                exit_code=exit_code,
            ),
            message=message,
            layout_path=layout_path,
        )
        del receipt

    def _execute_backend(
        self,
        context: ActionContext,
        client: Any,
        job: PhysicalDesignJob,
        result: PhysicalDesignResult,
        plan: MaterializationPlan,
        configuration: _AdapterConfiguration,
        assets: _MaterializationAssets,
        *,
        executable: Path,
    ) -> tuple[Path, int]:
        project = configuration.project
        cds_lib = project.workspace_root / "cds.lib"
        if not cds_lib.is_file() or cds_lib.is_symlink():
            raise RuntimeError(f"managed OA workspace cds.lib is unavailable: {cds_lib}")
        write_source = render_oa_materialization_skill(job, result, plan, assets)
        validate_source = render_oa_materialization_validation_skill(
            job,
            result,
            plan,
            assets.target,
        )
        (context.work_root / "materialize.il").write_text(
            write_source,
            encoding="utf-8",
        )
        (context.work_root / "validate-materialization.il").write_text(
            validate_source,
            encoding="utf-8",
        )
        target = assets.target
        with workspace_operation(
            client,
            project.workspace_root,
            f"materialize {target.library}/{target.cell}/{target.view}",
            policy=OperationPolicy.DIRECT_MUTATION,
        ) as operation:
            with operation.mutation_scope(
                target.library,
                cells=None,
                phase="ensure managed materialization library",
                expected_library_path=target.library_path,
                require_view_lease=False,
            ):
                ensure_project_library(
                    client,
                    library=target.library,
                    path=target.library_path,
                    technology_library=assets.technology_library,
                    cds_lib=cds_lib,
                    operation=operation,
                    timeout=configuration.oa_timeout_seconds,
                )
            info = client.library.get(target.library, timeout=30)
            if Path(info.path).resolve() != target.library_path:
                raise RuntimeError(
                    "registered managed OA library path disagrees with its target contract"
                )
            if info.technology_library != assets.technology_library:
                raise RuntimeError(
                    "managed OA library technology binding disagrees with platform assets"
                )
            with operation.view_lease(
                target.library,
                cells=(target.cell,),
                views=((target.cell, target.view),),
            ):
                with operation.mutation_scope(
                    target.library,
                    cells=(target.cell,),
                    views=((target.cell, target.view),),
                    phase="physical-design materialization",
                    expected_library_path=target.library_path,
                ):
                    _bridge_execute(
                        client,
                        operation,
                        write_source,
                        label="write physical-design Materialization Plan",
                        target=target,
                        timeout=configuration.oa_timeout_seconds,
                        mutation=True,
                    )
                _bridge_execute(
                    client,
                    operation,
                    validate_source,
                    label="validate physical-design Materialization Plan",
                    target=target,
                    timeout=configuration.oa_timeout_seconds,
                    mutation=False,
                )
            exported = run_xstream_export(
                XStreamExportRequest(
                    executable=executable,
                    library=target.library,
                    cell=target.cell,
                    view=target.view,
                    technology_library=assets.technology_library,
                    layer_map=assets.xstream_layer_map,
                    cds_lib=cds_lib,
                    work_root=context.work_root / "xstream",
                    timeout_seconds=configuration.xstream_timeout_seconds,
                    flatten_pcells=assets.flatten_pcells,
                    suppressed_warnings=assets.suppressed_warnings,
                )
            )
        atomic_write_json(
            context.work_root / "xstream-command.json",
            {
                "argv": list(exported.command),
                "cwd": str(context.work_root / "xstream"),
                "timeout_seconds": configuration.xstream_timeout_seconds,
            },
        )
        logs = context.log_root / "oa-xstream"
        (logs / "xstream-stdout.log").write_text(exported.stdout, encoding="utf-8")
        _write_regular_copy(
            exported.native_log_path,
            logs / "strmout.log",
            "XStream native log",
        )
        _write_regular_copy(
            exported.summary_path,
            logs / "strmout.sum",
            "XStream summary",
        )
        raw = _read_regular_bytes(exported.gds_path, "XStream GDSII output")
        validate_layout_content(raw, LayoutArtifactFormat.GDSII)
        canonical = canonicalize_xstream_gdsii(raw)
        layout_path = context.output_path("layout", "layout.gds")
        try:
            layout_path.write_bytes(canonical)
            validate_layout_content(
                _read_regular_bytes(layout_path, "managed GDSII output"),
                LayoutArtifactFormat.GDSII,
            )
        except Exception:
            layout_path.unlink(missing_ok=True)
            raise
        return layout_path, exported.exit_code

    def execute(self, context: ActionContext) -> AdapterExecution:
        job, result, plan, target, configuration, assets = self._inputs(context)
        request = validate_materialization_request(job, result, plan, target)
        if not request.valid:
            self._receipt(
                context,
                status=MaterializationExecutionStatus.INVALID_PLAN_IDENTITY,
                backend=_BACKEND,
                executed=False,
                completed=False,
                content_validated=False,
                exit_code=None,
                message="materialization request failed typed identity validation",
            )
        else:
            unsupported = _support_issues(job, plan, assets)
            if unsupported:
                self._receipt(
                    context,
                    status=MaterializationExecutionStatus.UNSUPPORTED,
                    backend=_BACKEND,
                    executed=False,
                    completed=False,
                    content_validated=False,
                    exit_code=None,
                    message="; ".join(unsupported),
                )
            else:
                try:
                    backend, executable = self._resources(context)
                except (FlowExecutionError, OSError) as exc:
                    self._receipt(
                        context,
                        status=MaterializationExecutionStatus.BACKEND_UNAVAILABLE,
                        backend=_BACKEND,
                        executed=False,
                        completed=False,
                        content_validated=False,
                        exit_code=None,
                        message=f"OA/XStream backend unavailable: {exc}",
                    )
                else:
                    try:
                        client = self._client_factory()
                    except Exception as exc:
                        self._receipt(
                            context,
                            status=MaterializationExecutionStatus.BACKEND_UNAVAILABLE,
                            backend=backend,
                            executed=False,
                            completed=False,
                            content_validated=False,
                            exit_code=None,
                            message=f"Virtuoso bridge backend unavailable: {exc}",
                        )
                    else:
                        try:
                            layout_path, exit_code = self._execute_backend(
                                context,
                                client,
                                job,
                                result,
                                plan,
                                configuration,
                                assets,
                                executable=executable,
                            )
                        except XStreamExportError as exc:
                            diagnostic_message = ""
                            if exc.diagnostic_path is not None:
                                try:
                                    _write_regular_copy(
                                        exc.diagnostic_path,
                                        context.log_root
                                        / "oa-xstream"
                                        / "xstream-failure.log",
                                        "XStream failure diagnostic",
                                    )
                                    diagnostic_message = (
                                        "; managed diagnostic: "
                                        "logs/materialize/oa-xstream/xstream-failure.log"
                                    )
                                except Exception as diagnostic_error:
                                    diagnostic_message = (
                                        "; diagnostic persistence failed: "
                                        f"{diagnostic_error}"
                                    )
                            self._receipt(
                                context,
                                status=MaterializationExecutionStatus.EXECUTION_FAILED,
                                backend=backend,
                                executed=True,
                                completed=False,
                                content_validated=False,
                                exit_code=exc.exit_code,
                                message=(
                                    f"OA/XStream execution failed: {exc}"
                                    f"{diagnostic_message}"
                                ),
                            )
                        except Exception as exc:
                            self._receipt(
                                context,
                                status=MaterializationExecutionStatus.EXECUTION_FAILED,
                                backend=backend,
                                executed=True,
                                completed=False,
                                content_validated=False,
                                exit_code=None,
                                message=f"OA/Virtuoso materialization failed: {exc}",
                            )
                        else:
                            self._receipt(
                                context,
                                status=MaterializationExecutionStatus.MATERIALIZED,
                                backend=backend,
                                executed=True,
                                completed=True,
                                content_validated=True,
                                exit_code=exit_code,
                                message=(
                                    "canonical plan materialized through managed OA/Virtuoso "
                                    "and authoritative XStream GDSII export"
                                ),
                                layout_path=layout_path,
                            )
        receipt_path = context.output_path(
            "receipt",
            "materialization-receipt.json",
        )
        receipt = materialization_receipt_from_json(
            receipt_path.read_text(encoding="utf-8")
        )
        return AdapterExecution.succeeded(
            details=materialization_execution_facts(receipt)
        )

    def collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        return collect_materialization_execution_result(context, execution)


__all__ = [
    "OaXStreamMaterializationAdapter",
    "render_oa_materialization_skill",
    "render_oa_materialization_validation_skill",
]
