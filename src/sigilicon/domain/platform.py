"""Resolved project platform contracts behind one deep loading interface."""

from __future__ import annotations

from dataclasses import InitVar, dataclass, field
from pathlib import Path, PurePosixPath
import re
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Iterator, Mapping, Protocol, runtime_checkable

from sigilicon.contracts import (
    contract_schema,
    freeze_toml_document,
    is_frozen_toml_document,
    read_toml,
    require_config_header,
)
from sigilicon.domain.layout_technology import LayoutTechnology, parse_layout_technology
from sigilicon.domain.context import RepositoryIdentity
from sigilicon.domain.physical_verification import DeckSubstitution

if TYPE_CHECKING:
    from sigilicon.project import Project


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")
_PLATFORM_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_HEADER_FIELDS = {"schema", "contract_kind", "path_scope", "owner"}
_MAPPING_PROXY_TYPE = type(MappingProxyType({}))


@runtime_checkable
class PlatformResources(Protocol):
    """Minimal runtime-resource capability required by platform loading."""

    def require_directory(self, name: str) -> Path: ...


@dataclass(frozen=True)
class _SnapshotPlatformResources:
    identity: str
    path: Path

    def require_directory(self, name: str) -> Path:
        if name != self.identity:
            raise ValueError(f"platform snapshot has no resource {name!r}")
        return self.path


@dataclass(frozen=True)
class PlatformAsset:
    """One logical PDK asset with an optional explicit runtime binding."""

    logical: PurePosixPath
    location: Path | None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.logical, PurePosixPath)
            or self.logical.is_absolute()
            or any(part in {"", ".", ".."} for part in self.logical.parts)
        ):
            raise ValueError("platform asset path must be logical and relative")
        if self.location is not None:
            location = Path(self.location).absolute()
            if location != location.resolve():
                raise ValueError("platform asset binding must not traverse a symlink")
            object.__setattr__(self, "location", location)

    @property
    def name(self) -> str:
        return self.logical.name

    @property
    def bound(self) -> bool:
        return self.location is not None

    def require_path(self) -> Path:
        if self.location is None:
            raise ValueError(f"platform asset is not runtime-bound: {self.logical}")
        return self.location


@dataclass(frozen=True)
class SimulationModelSet:
    """One caller-selectable simulator model include set."""

    name: str
    file: PlatformAsset
    sections: tuple[str, ...]
    support_files: tuple[PlatformAsset, ...] = ()

    @property
    def files(self) -> tuple[PlatformAsset, ...]:
        return (self.file, *self.support_files)

    @property
    def paths(self) -> tuple[Path, ...]:
        """Return runtime paths, rejecting use before explicit binding."""

        return tuple(asset.require_path() for asset in self.files)

    @property
    def single_section(self) -> str:
        if len(self.sections) != 1:
            raise ValueError(
                f"platform model set {self.name!r} does not have one default section"
            )
        return self.sections[0]


@dataclass(frozen=True)
class SimulationPlatform:
    """Simulation capability supplied by one project platform."""

    path: Path
    default_model_set: str
    model_sets: Mapping[str, SimulationModelSet]

    @property
    def default(self) -> SimulationModelSet:
        return self.model_set(self.default_model_set)

    def model_set(self, name: str) -> SimulationModelSet:
        try:
            return self.model_sets[name]
        except KeyError as exc:
            raise ValueError(f"platform has no simulation model set {name!r}") from exc


@dataclass(frozen=True)
class OaPlatformConfig:
    """OA technology and primitive facts owned by the project platform."""

    path: Path
    technology_library: str
    reference_libraries: tuple[str, ...]
    primitive_subcircuits: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class OaLayerPurposeMapping:
    """One logical physical layer's explicit OA layer-purpose mapping."""

    layer: str
    drawing_purpose: str
    pin_purpose: str
    blockage_purpose: str


@dataclass(frozen=True)
class OaMaterializationMapping:
    """Atomic logical-layer and via mapping owned by a platform contract."""

    layers: Mapping[str, OaLayerPurposeMapping]
    vias: Mapping[str, str]


@dataclass(frozen=True)
class LayoutPlatform:
    """Geometry and optional OA stream export capability of one platform."""

    layout_path: Path
    dbu_per_micron: int
    layermap: PlatformAsset | None = None
    xstream_flatten_pcells: bool = True
    xstream_suppressed_warnings: tuple[str, ...] = ()
    oa_materialization: OaMaterializationMapping | None = None
    technology: LayoutTechnology | None = None


@dataclass(frozen=True)
class VerificationDeck:
    asset: PlatformAsset
    substitutions: tuple[DeckSubstitution, ...]


@dataclass(frozen=True)
class VerificationPlatform:
    path: Path
    checks: Mapping[str, VerificationDeck]
    qrc_tech_file: PlatformAsset | None = None

    def require_check(self, name: str) -> VerificationDeck:
        try:
            return self.checks[name]
        except KeyError as exc:
            raise ValueError(f"platform verification does not support {name}") from exc


_PLATFORM_AUTHORITY = object()


@dataclass(frozen=True)
class Platform:
    """One loader-sealed logical platform with optional runtime-bound assets."""

    _authority: InitVar[object]
    key: str
    path: Path
    owner: str
    name: str
    simulation: SimulationPlatform | None
    oa: OaPlatformConfig | None
    layout: LayoutPlatform | None
    verification: VerificationPlatform | None
    source_paths: tuple[Path, ...]
    catalog_document: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )
    asset_root: Path | None = None
    asset_root_resource: str | None = None
    source_documents: Mapping[Path, Mapping[str, Any]] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def __post_init__(self, _authority: object) -> None:
        if _authority is not _PLATFORM_AUTHORITY:
            raise ValueError("platform snapshots must be built by the platform loader")
        if any(not isinstance(asset, PlatformAsset) for asset in self.assets):
            raise ValueError("platform assets must use logical PlatformAsset values")

    @property
    def runtime_bound(self) -> bool:
        """Whether external asset paths were resolved for this invocation."""

        if self.asset_root_resource is not None and self.asset_root is None:
            return False
        return all(asset.bound for asset in self.assets)

    @property
    def assets(self) -> tuple[PlatformAsset, ...]:
        selected: list[PlatformAsset] = []
        if self.simulation is not None:
            selected.extend(
                asset
                for model_set in self.simulation.model_sets.values()
                for asset in model_set.files
            )
        if self.layout is not None and self.layout.layermap is not None:
            selected.append(self.layout.layermap)
        if self.verification is not None:
            selected.extend(deck.asset for deck in self.verification.checks.values())
            if self.verification.qrc_tech_file is not None:
                selected.append(self.verification.qrc_tech_file)
        return tuple(selected)

    @property
    def asset_paths(self) -> tuple[PurePosixPath, ...]:
        return tuple(sorted({asset.logical for asset in self.assets}))


def platform_resource_identities(platform: Platform) -> Mapping[Path, str]:
    """Name every external platform asset by its domain identity."""

    selected: dict[Path, str] = {}
    simulation = getattr(platform, "simulation", None)
    if simulation is not None:
        for name, model_set in sorted(simulation.model_sets.items()):
            for index, asset in enumerate(model_set.files):
                path = asset.require_path()
                selected[path.absolute()] = (
                    f"pdk:{platform.key}:simulation/{name}/{index}-{path.name}"
                )
    if platform.layout is not None and platform.layout.layermap is not None:
        selected[platform.layout.layermap.require_path()] = f"pdk:{platform.key}:layout/layermap"
    if platform.verification is not None:
        for name, deck in platform.verification.checks.items():
            selected[deck.asset.require_path()] = f"pdk:{platform.key}:verification/{name}"
        asset = platform.verification.qrc_tech_file
        if asset is not None:
            selected[asset.require_path()] = f"pdk:{platform.key}:verification/qrc"
    return MappingProxyType(selected)


def model_resource_identities(
    platform: Platform,
    model_set: SimulationModelSet,
) -> Mapping[Path, str]:
    """Name the selected simulator model assets within their platform."""

    selected = dict(platform_resource_identities(platform))
    for index, asset in enumerate(model_set.files):
        path = asset.require_path()
        selected[path.absolute()] = (
            f"pdk:{platform.key}:simulation/"
            f"{model_set.name}/{index}-{path.name}"
        )
    return MappingProxyType(selected)


@dataclass(frozen=True)
class PlatformCatalogSnapshot:
    """One validated project platform catalog read for an operation."""

    path: Path
    project_root: Path
    owner: str
    manifests: Mapping[str, Path]
    document: Mapping[str, Any]

    def manifest(self, key: str) -> Path:
        try:
            return self.manifests[key]
        except KeyError as exc:
            raise ValueError(f"platform catalog has no {key!r} entry") from exc


_PLATFORM_INVENTORY_AUTHORITY = object()


class PlatformSet(Mapping[str, Platform]):
    """One validated platform catalog with one binding state."""

    __slots__ = ("_catalog", "_platforms", "_repository")

    def __init__(
        self,
        *,
        _authority: object,
        project: Project,
        catalog: PlatformCatalogSnapshot,
        platforms: Mapping[str, Platform],
    ) -> None:
        if _authority is not _PLATFORM_INVENTORY_AUTHORITY:
            raise ValueError("platform set must be built by its loader")
        if (
            catalog.project_root != project.project_root
            or catalog.path != project.catalog("platform")
        ):
            raise ValueError("platform set catalog identity drift")
        _validate_immutable_platform_catalog(catalog)
        selected = dict(platforms)
        if set(selected) != set(catalog.manifests):
            raise ValueError("platform set does not cover its complete catalog")
        for key, platform in selected.items():
            if (
                not isinstance(platform, Platform)
                or platform.key != key
                or platform.path != catalog.manifest(key)
                or platform.catalog_document != catalog.document
                or not platform.source_paths
                or platform.source_paths[0] != catalog.path
            ):
                raise ValueError("platform set identity drift")
            _validate_immutable_platform_snapshot(platform)
            if set(platform.source_documents) != set(platform.source_paths[1:]):
                raise ValueError("platform source identity drift")
        object.__setattr__(
            self,
            "_repository",
            RepositoryIdentity.for_repository(project),
        )
        object.__setattr__(self, "_catalog", catalog)
        object.__setattr__(self, "_platforms", MappingProxyType(selected))

    @property
    def repository(self) -> RepositoryIdentity:
        return self._repository

    @property
    def catalog(self) -> PlatformCatalogSnapshot:
        return self._catalog

    @property
    def platforms(self) -> Mapping[str, Platform]:
        return self._platforms

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("platform set is immutable")

    def __getitem__(self, key: str) -> Platform:
        return self.platforms[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.platforms)

    def __len__(self) -> int:
        return len(self.platforms)

    def resolve(self, context: Project, key: str) -> Platform:
        context.manifest_source_document()
        self.repository.validate(context)
        if (
            self.catalog.project_root != context.project_root
            or self.catalog.path != context.catalog("platform")
        ):
            raise ValueError("platform set belongs to a different operation")
        try:
            platform = self.platforms[key]
        except KeyError as exc:
            raise ValueError(f"platform set has no {key!r} entry") from exc
        if (
            self.catalog.manifest(key) != platform.path
            or platform.key != key
            or not platform.source_paths
            or platform.source_paths[0] != self.catalog.path
        ):
            raise ValueError("platform set identity drift")
        resolve_platform_catalog(context, snapshot=self.catalog)
        return _validate_platform_snapshot(context, key, platform, resources=None)


PlatformSnapshot = Platform | PlatformSet


def _validate_immutable_platform_catalog(
    snapshot: PlatformCatalogSnapshot,
) -> None:
    if not isinstance(snapshot.manifests, _MAPPING_PROXY_TYPE) or not (
        is_frozen_toml_document(snapshot.document)
    ):
        raise ValueError("platform catalog snapshot immutable identity drift")


def _validate_immutable_platform_snapshot(snapshot: Platform) -> None:
    if not is_frozen_toml_document(snapshot.catalog_document):
        raise ValueError("platform snapshot catalog document identity drift")
    if not isinstance(snapshot.source_documents, _MAPPING_PROXY_TYPE) or any(
        not is_frozen_toml_document(document)
        for document in snapshot.source_documents.values()
    ):
        raise ValueError("platform snapshot source document identity drift")
    typed_mappings: list[Mapping[str, object]] = []
    if snapshot.simulation is not None:
        typed_mappings.append(snapshot.simulation.model_sets)
    if snapshot.verification is not None:
        typed_mappings.append(snapshot.verification.checks)
    if snapshot.oa is not None:
        typed_mappings.append(snapshot.oa.primitive_subcircuits)
    if (
        snapshot.layout is not None
        and snapshot.layout.oa_materialization is not None
    ):
        typed_mappings.extend(
            (
                snapshot.layout.oa_materialization.layers,
                snapshot.layout.oa_materialization.vias,
            )
        )
    if any(
        not isinstance(mapping, _MAPPING_PROXY_TYPE)
        for mapping in typed_mappings
    ):
        raise ValueError("platform snapshot typed mapping identity drift")


def _validate_platform_snapshot(
    context: Project,
    key: str,
    snapshot: Platform,
    *,
    resources: PlatformResources | None,
) -> Platform:
    """Validate a platform snapshot without consulting ambient process state."""

    context.manifest_source_document()
    if snapshot is None:
        raise TypeError("platform snapshot must be Platform")
    if snapshot.key != key:
        raise ValueError(
            f"platform snapshot {snapshot.key!r} disagrees with requested key {key!r}"
        )
    _validate_immutable_platform_snapshot(snapshot)
    selected_resources = resources
    if (
        selected_resources is None
        and snapshot.asset_root_resource is not None
        and snapshot.asset_root is not None
    ):
        selected_resources = _SnapshotPlatformResources(
            snapshot.asset_root_resource,
            snapshot.asset_root,
        )
    try:
        current = _load_platform(context, key, resources=selected_resources)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("platform snapshot source identity drift") from exc
    if current != snapshot:
        raise ValueError("platform identity drift: source or runtime changed")
    return snapshot

def resolve_platform_snapshot(
    context: Project,
    key: str,
    *,
    snapshot: PlatformSnapshot | None = None,
) -> Platform:
    """Resolve a public snapshot or select an operation-trusted inventory."""

    if isinstance(snapshot, PlatformSet):
        return snapshot.resolve(context, key)
    if snapshot is None:
        return load_platform(context, key)
    return _validate_platform_snapshot(context, key, snapshot, resources=None)


def resolve_platform_catalog(
    context: Project,
    *,
    snapshot: PlatformCatalogSnapshot | None = None,
) -> PlatformCatalogSnapshot:
    """Load a platform catalog or validate one operation-owned snapshot."""

    if snapshot is None:
        return load_platform_catalog(context)
    _validate_immutable_platform_catalog(snapshot)
    current = load_platform_catalog(context)
    if current != snapshot or current.document != snapshot.document:
        raise ValueError("platform catalog snapshot identity drift")
    return snapshot


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field} must be an identifier")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be non-empty text")
    return value


def _table(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a table")
    return value


def _reject_unknown(
    raw: Mapping[str, Any], allowed: set[str], field: str
) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"{field} contains unsupported fields: {sorted(unknown)}")


def _names(value: object, field: str, *, empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or (not value and not empty):
        raise ValueError(f"{field} must be a string array")
    result = tuple(_identifier(item, f"{field}[]") for item in value)
    if len(set(result)) != len(result):
        raise ValueError(f"{field} contains duplicates")
    return result


def _strings(value: object, field: str, *, empty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or (not value and not empty) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ValueError(f"{field} must be a string array")
    result = tuple(value)
    if len(set(result)) != len(result):
        raise ValueError(f"{field} contains duplicates")
    return result


def _safe_relative(base: Path, value: object, field: str, *, root: Path) -> Path:
    text = _text(value, field)
    relative = Path(text)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{field} must be a safe relative path")
    result = (base / relative).resolve()
    if not result.is_relative_to(root):
        raise ValueError(f"{field} escapes the project")
    return result


def _platform_asset(
    base: Path | None,
    value: object,
    field: str,
    *,
    require_asset: bool = True,
) -> PlatformAsset:
    text = _text(value, field)
    relative = PurePosixPath(text)
    if relative.is_absolute() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise ValueError(f"{field} must be a safe asset-relative path")
    if base is None:
        return PlatformAsset(relative, None)
    configured = (base / Path(relative)).absolute()
    if configured != configured.resolve():
        raise ValueError(f"{field} must not traverse a symlink")
    if require_asset and not configured.is_file():
        raise ValueError(f"{field} does not exist: {configured}")
    return PlatformAsset(relative, configured)


def _platform_asset_resource(key: str) -> str:
    """Return the semantic external-directory resource for one platform key."""

    if not isinstance(key, str) or _PLATFORM_KEY.fullmatch(key) is None:
        raise ValueError("platform key contains unsupported characters")
    return f"platform.{key}"


def _validate_platform_resource_names(platforms: Mapping[str, Any]) -> None:
    """Validate catalog keys used to derive external resource identities."""

    for key in platforms:
        if not isinstance(key, str) or _PLATFORM_KEY.fullmatch(key) is None:
            raise ValueError("platform catalog keys contain unsupported characters")
        _platform_asset_resource(key)


def _platform_asset_root(
    manifest: Path,
    key: str,
    raw: Mapping[str, Any],
    *,
    resources: PlatformResources | None,
) -> tuple[Path | None, str | None]:
    scope = raw.get("asset_scope", "project")
    if not isinstance(scope, str) or scope not in {"project", "external"}:
        raise ValueError("platform asset_scope must be 'project' or 'external'")
    if scope == "project":
        return manifest.parent, None

    root_resource = _platform_asset_resource(key)
    if resources is None:
        # Snapshot validation must remain independent of ambient environment.
        # The caller supplies the already sealed root through the snapshot.
        return None, root_resource
    asset_root = resources.require_directory(root_resource)
    if not isinstance(asset_root, Path):
        raise TypeError("platform asset root must be a Path")
    return asset_root.expanduser().absolute(), root_resource


def _contract(
    manifest: Path,
    contracts: Mapping[str, Any],
    name: str,
    *,
    root: Path,
    owner: str,
    required: bool = True,
) -> tuple[Path, Mapping[str, Any]] | None:
    value = contracts.get(name)
    if value is None and not required:
        return None
    path = _safe_relative(
        manifest.parent,
        value,
        f"platform.contracts.{name}",
        root=root,
    )
    raw = read_toml(path)
    require_config_header(
        raw,
        path,
        contract_kind=f"platform-{name}",
        schema=contract_schema(f"platform-{name}"),
        path_scope="platform",
        owner=owner,
    )
    return path, raw


def _load_simulation(
    path: Path, raw: Mapping[str, Any], *, asset_root: Path | None
) -> SimulationPlatform:
    _reject_unknown(
        raw,
        _HEADER_FIELDS | {"default_model_set", "model_sets"},
        "platform simulation contract",
    )
    default_name = _text(raw.get("default_model_set"), "default_model_set")
    sets_raw = _table(raw.get("model_sets"), "model_sets")
    model_sets: dict[str, SimulationModelSet] = {}
    for raw_name, value in sets_raw.items():
        name = _identifier(raw_name, "model_sets key")
        item = _table(value, f"model_sets.{name}")
        _reject_unknown(
            item,
            {"file", "sections", "support_files"},
            f"model_sets.{name}",
        )
        sections = _strings(
            item.get("sections"), f"model_sets.{name}.sections", empty=False
        )
        support_values = item.get("support_files", [])
        support_files = tuple(
            _platform_asset(
                asset_root,
                support,
                f"model_sets.{name}.support_files[{index}]",
            )
            for index, support in enumerate(
                _strings(support_values, f"model_sets.{name}.support_files")
            )
        )
        model_sets[name] = SimulationModelSet(
            name=name,
            file=_platform_asset(
                asset_root,
                item.get("file"),
                f"model_sets.{name}.file",
            ),
            sections=sections,
            support_files=support_files,
        )
    if default_name not in model_sets:
        raise ValueError("default_model_set must select a declared model set")
    model_sets[default_name].single_section
    return SimulationPlatform(
        path,
        default_name,
        MappingProxyType(model_sets),
    )


def _load_oa(path: Path, raw: Mapping[str, Any]) -> OaPlatformConfig:
    _reject_unknown(
        raw,
        _HEADER_FIELDS
        | {
            "technology_library",
            "reference_libraries",
            "primitive_subcircuits",
        },
        "platform OA contract",
    )
    primitive_raw = _table(raw.get("primitive_subcircuits", {}), "primitive_subcircuits")
    primitive_subcircuits = {
        _identifier(master, "primitive_subcircuits key"): _names(
            terminals, f"primitive_subcircuits.{master}"
        )
        for master, terminals in primitive_raw.items()
    }
    return OaPlatformConfig(
        path=path,
        technology_library=_identifier(
            raw.get("technology_library"), "technology_library"
        ),
        reference_libraries=_names(raw.get("reference_libraries"), "reference_libraries"),
        primitive_subcircuits=MappingProxyType(primitive_subcircuits),
    )


def _parse_oa_materialization(
    value: object,
) -> OaMaterializationMapping | None:
    if value is None:
        return None
    raw = _table(value, "layout.oa_materialization")
    _reject_unknown(raw, {"layers", "vias"}, "layout.oa_materialization")
    layers_raw = _table(raw.get("layers"), "layout.oa_materialization.layers")
    layers: dict[str, OaLayerPurposeMapping] = {}
    for logical_value, item_value in layers_raw.items():
        logical = _text(logical_value, "layout.oa_materialization.layers key")
        item = _table(
            item_value,
            f"layout.oa_materialization.layers.{logical}",
        )
        _reject_unknown(
            item,
            {
                "layer",
                "drawing_purpose",
                "pin_purpose",
                "blockage_purpose",
            },
            f"layout.oa_materialization.layers.{logical}",
        )
        layers[logical] = OaLayerPurposeMapping(
            layer=_text(item.get("layer"), f"layers.{logical}.layer"),
            drawing_purpose=_text(
                item.get("drawing_purpose"),
                f"layers.{logical}.drawing_purpose",
            ),
            pin_purpose=_text(
                item.get("pin_purpose"),
                f"layers.{logical}.pin_purpose",
            ),
            blockage_purpose=_text(
                item.get("blockage_purpose"),
                f"layers.{logical}.blockage_purpose",
            ),
        )
    vias_raw = _table(raw.get("vias"), "layout.oa_materialization.vias")
    vias = {
        _text(logical_value, "layout.oa_materialization.vias key"): _identifier(
            via_value,
            f"layout.oa_materialization.vias.{logical_value}",
        )
        for logical_value, via_value in vias_raw.items()
    }
    return OaMaterializationMapping(
        layers=MappingProxyType(layers),
        vias=MappingProxyType(vias),
    )


def _load_layout(
    layout_path: Path, layout_raw: Mapping[str, Any], *,
    asset_root: Path | None, require_assets: bool = True,
) -> LayoutPlatform:
    _reject_unknown(layout_raw, _HEADER_FIELDS | {
        "dbu_per_micron", "oa_materialization", "custom_layout", "layermap",
        "xstream_flatten_pcells", "xstream_suppressed_warnings",
    }, "platform layout contract")
    dbu = layout_raw.get("dbu_per_micron")
    if type(dbu) is not int or dbu <= 0:
        raise ValueError("layout.dbu_per_micron must be a positive integer")
    flatten = layout_raw.get("xstream_flatten_pcells", True)
    if not isinstance(flatten, bool):
        raise ValueError("layout.xstream_flatten_pcells must be boolean")
    warnings = _strings(layout_raw.get("xstream_suppressed_warnings", []), "layout.xstream_suppressed_warnings")
    if any(re.fullmatch(r"XSTRM-[0-9]+", warning) is None for warning in warnings):
        raise ValueError("xstream warnings must use XSTRM-<number> identities")
    return LayoutPlatform(
        layout_path=layout_path, dbu_per_micron=dbu,
        layermap=(_platform_asset(asset_root, layout_raw["layermap"], "layermap", require_asset=require_assets)
                  if "layermap" in layout_raw else None),
        xstream_flatten_pcells=flatten, xstream_suppressed_warnings=warnings,
        oa_materialization=_parse_oa_materialization(layout_raw.get("oa_materialization")),
        technology=(parse_layout_technology(layout_raw, owner=_text(layout_raw.get("owner"), "layout.owner"),
                                           payload_key="custom_layout") if "custom_layout" in layout_raw else None),
    )


def _load_verification(
    path: Path, raw: Mapping[str, Any], *, asset_root: Path | None, require_assets: bool,
) -> VerificationPlatform:
    _reject_unknown(raw, _HEADER_FIELDS | {"drc", "lvs", "qrc_tech_file"}, "platform verification contract")
    checks = {}
    for name in ("drc", "lvs"):
        if name not in raw:
            continue
        row = _table(raw[name], f"verification.{name}")
        _reject_unknown(row, {"deck", "substitutions"}, f"verification.{name}")
        substitutions = row.get("substitutions")
        if not isinstance(substitutions, (list, tuple)) or not substitutions:
            raise ValueError("verification deck requires explicit substitutions")
        edits = tuple(DeckSubstitution.from_record(item) for item in substitutions)
        parameters = set().union(*(edit.parameters for edit in edits))
        required = {"layout_path", "primary"} | (
            {"results_path", "summary_path"} if name == "drc" else {"source_path", "work_dir"})
        if not required.issubset(parameters):
            raise ValueError(f"verification {name} template must bind {sorted(required)}")
        checks[name] = VerificationDeck(
            _platform_asset(asset_root, row.get("deck"), f"{name}.deck", require_asset=require_assets),
            edits,
        )
    if not checks:
        raise ValueError("verification requires at least one DRC or LVS check")
    return VerificationPlatform(path, MappingProxyType(checks),
        _platform_asset(asset_root, raw["qrc_tech_file"], "qrc_tech_file", require_asset=require_assets)
        if "qrc_tech_file" in raw else None)


def _platform_catalog_document(
    context: Project,
    document: Mapping[str, Any],
) -> tuple[Path, Path, str, Mapping[str, Any]]:
    context.manifest_source_document()
    root = context.project_root
    catalog_path = context.catalog("platform")
    if not catalog_path.is_file() or not catalog_path.is_relative_to(root):
        raise ValueError("platform catalog must be a project-owned file")
    header = require_config_header(
        document,
        catalog_path,
        contract_kind="platform-catalog",
        path_scope="repository",
        owner=context.manifest_owner,
    )
    _reject_unknown(
        document,
        _HEADER_FIELDS | {"platforms"},
        "platform catalog",
    )
    platforms = _table(document.get("platforms"), "platform catalog platforms")
    return root, catalog_path, header.owner, platforms


def parse_platform_catalog(
    context: Project,
    document: Mapping[str, Any],
) -> PlatformCatalogSnapshot:
    """Validate an already read canonical platform catalog document."""

    root, catalog_path, owner, platforms = _platform_catalog_document(
        context,
        document,
    )
    manifests: dict[str, Path] = {}
    _validate_platform_resource_names(platforms)
    for key, value in platforms.items():
        if not isinstance(key, str) or _PLATFORM_KEY.fullmatch(key) is None:
            raise ValueError("platform catalog keys contain unsupported characters")
        manifests[key] = _safe_relative(
            catalog_path.parent,
            value,
            f"platforms.{key}",
            root=root,
        )
    return PlatformCatalogSnapshot(
        path=catalog_path,
        project_root=root,
        owner=owner,
        manifests=MappingProxyType(manifests),
        document=freeze_toml_document(document),
    )


def load_platform_catalog(context: Project) -> PlatformCatalogSnapshot:
    """Read and validate the project's canonical platform catalog once."""

    catalog_path = context.catalog("platform")
    return parse_platform_catalog(context, read_toml(catalog_path))


def _load_platform(
    context: Project,
    key: str,
    *,
    resources: PlatformResources | None,
    catalog: PlatformCatalogSnapshot | None = None,
) -> Platform:
    """Parse one platform, optionally resolving its external asset root."""

    if not isinstance(key, str) or _PLATFORM_KEY.fullmatch(key) is None:
        raise ValueError("platform key contains unsupported characters")
    if resources is not None and not isinstance(resources, PlatformResources):
        raise TypeError("platform resources must provide require_directory")
    if catalog is None:
        catalog_document = read_toml(context.catalog("platform"))
        root, catalog_path, _owner, platforms = _platform_catalog_document(
            context,
            catalog_document,
        )
        _validate_platform_resource_names(platforms)
        try:
            manifest_value = platforms[key]
        except KeyError as exc:
            raise ValueError(f"platform catalog has no {key!r} entry") from exc
        manifest = _safe_relative(
            catalog_path.parent,
            manifest_value,
            f"platforms.{key}",
            root=root,
        )
    else:
        catalog_snapshot = resolve_platform_catalog(context, snapshot=catalog)
        catalog_document = catalog_snapshot.document
        root = catalog_snapshot.project_root
        catalog_path = catalog_snapshot.path
        manifest = catalog_snapshot.manifest(key)
    raw = read_toml(manifest)
    header = require_config_header(
        raw,
        manifest,
        contract_kind="platform-definition",
        path_scope="platform",
    )
    _reject_unknown(
        raw,
        _HEADER_FIELDS | {"name", "asset_scope", "contracts"},
        "platform definition",
    )
    if header.owner != key:
        raise ValueError(f"platform manifest owner must be {key!r}")
    asset_root, root_resource = _platform_asset_root(
        manifest,
        key,
        raw,
        resources=resources,
    )
    contracts = _table(raw.get("contracts"), "platform.contracts")
    allowed = {"simulation", "oa", "layout", "verification"}
    if set(contracts) - allowed or not contracts:
        raise ValueError(
            "platform contracts must declare at least one supported capability"
        )
    simulation_contract = _contract(
        manifest,
        contracts,
        "simulation",
        root=root,
        owner=header.owner,
        required=False,
    )
    oa_contract = _contract(
        manifest, contracts, "oa", root=root, owner=header.owner, required=False
    )
    simulation = (
        None
        if simulation_contract is None
        else _load_simulation(
            simulation_contract[0],
            simulation_contract[1],
            asset_root=asset_root,
        )
    )
    oa = None if oa_contract is None else _load_oa(*oa_contract)
    source_paths: list[Path] = [catalog_path, manifest]
    source_documents = {manifest: raw}
    for selected in (simulation_contract, oa_contract):
        if selected is not None:
            source_paths.append(selected[0])
            source_documents[selected[0]] = selected[1]
    layout_contract = _contract(manifest, contracts, "layout", root=root, owner=header.owner, required=False)
    verification_contract = _contract(manifest, contracts, "verification", root=root, owner=header.owner, required=False)
    layout = None if layout_contract is None else _load_layout(
        *layout_contract, asset_root=asset_root, require_assets=asset_root is not None)
    verification = None if verification_contract is None else _load_verification(
        *verification_contract, asset_root=asset_root, require_assets=asset_root is not None)
    for selected in (layout_contract, verification_contract):
        if selected is not None:
            source_paths.append(selected[0])
            source_documents[selected[0]] = selected[1]
    sources = tuple(source_paths)
    return Platform(
        _authority=_PLATFORM_AUTHORITY,
        key=key,
        path=manifest,
        owner=header.owner,
        name=_text(raw.get("name", key), "platform.name"),
        simulation=simulation,
        oa=oa,
        layout=layout,
        verification=verification,
        source_paths=sources,
        catalog_document=freeze_toml_document(catalog_document),
        source_documents=MappingProxyType(
            {
                path: freeze_toml_document(document)
                for path, document in source_documents.items()
            }
        ),
        asset_root=asset_root,
        asset_root_resource=root_resource,
    )


def load_platform(
    context: Project,
    key: str,
    *,
    resources: PlatformResources | None = None,
    catalog: PlatformCatalogSnapshot | None = None,
) -> Platform:
    """Load one platform, optionally binding its external runtime assets."""

    if resources is not None and not isinstance(resources, PlatformResources):
        raise TypeError("platform resources must provide require_directory")
    return _load_platform(
        context,
        key,
        resources=resources,
        catalog=catalog,
    )


def _load_platforms(
    context: Project,
    *,
    resources: PlatformResources | None = None,
    catalog: PlatformCatalogSnapshot | None = None,
) -> PlatformSet:
    """Load every platform, resolving host assets only when resources are supplied."""

    selected_catalog = (
        load_platform_catalog(context)
        if catalog is None
        else resolve_platform_catalog(context, snapshot=catalog)
    )
    if resources is not None and not isinstance(resources, PlatformResources):
        raise TypeError("platform resources must provide require_directory")
    platforms = {
        key: load_platform(
            context,
            key,
            resources=resources,
            catalog=selected_catalog,
        )
        for key in selected_catalog.manifests
    }
    return PlatformSet(
        _authority=_PLATFORM_INVENTORY_AUTHORITY,
        project=context,
        catalog=selected_catalog,
        platforms=MappingProxyType(platforms),
    )


def load_platforms(
    context: Project,
    *,
    resources: PlatformResources | None = None,
    catalog: PlatformCatalogSnapshot | None = None,
) -> PlatformSet:
    """Load the catalog, optionally binding external runtime assets."""

    return _load_platforms(context, resources=resources, catalog=catalog)
