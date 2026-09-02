"""Resolved project platform contracts behind one deep loading interface."""

from __future__ import annotations

from dataclasses import InitVar, dataclass, field
import os
from pathlib import Path, PurePosixPath
import re
from types import MappingProxyType
from typing import Any, Iterator, Mapping, Protocol, runtime_checkable

from sigilicon.contracts import (
    freeze_toml_document,
    is_frozen_toml_document,
    read_toml,
    require_config_header,
)
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
class SimulationModelSet:
    """One caller-selectable simulator model include set."""

    name: str
    file: Path
    sections: tuple[str, ...]
    support_files: tuple[Path, ...] = ()

    @property
    def files(self) -> tuple[Path, ...]:
        return (self.file, *self.support_files)

    @property
    def single_section(self) -> str:
        if len(self.sections) != 1:
            raise ValueError(
                f"platform model set {self.name!r} does not have one default section"
            )
        return self.sections[0]


@dataclass(frozen=True)
class SimulationPlatformConfig:
    """Resolved simulation capability supplied by one project platform."""

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
class LayoutPdkConfig:
    """Resolved layout and physical-verification platform capability."""

    layout_path: Path
    verification_path: Path
    dbu_per_micron: int
    layermap: Path
    drc_deck: Path
    lvs_deck: Path
    qrc_tech_file: Path | None
    xstream_flatten_pcells: bool = True
    xstream_suppressed_warnings: tuple[str, ...] = ()
    xstream_bin: Path | None = None
    calibre_bin: Path | None = None
    oa_materialization: OaMaterializationMapping | None = None


@dataclass(frozen=True)
class SimulationModelContract:
    """One model set whose assets remain logical references."""

    name: str
    file: PurePosixPath
    sections: tuple[str, ...]
    support_files: tuple[PurePosixPath, ...] = ()

    @property
    def files(self) -> tuple[PurePosixPath, ...]:
        return (self.file, *self.support_files)

    @property
    def single_section(self) -> str:
        if len(self.sections) != 1:
            raise ValueError(
                f"platform model set {self.name!r} does not have one default section"
            )
        return self.sections[0]


@dataclass(frozen=True)
class SimulationPlatformContract:
    """Source-owned simulation contract without host paths."""

    path: Path
    default_model_set: str
    model_sets: Mapping[str, SimulationModelContract]

    @property
    def default(self) -> SimulationModelContract:
        return self.model_set(self.default_model_set)

    def model_set(self, name: str) -> SimulationModelContract:
        try:
            return self.model_sets[name]
        except KeyError as exc:
            raise ValueError(f"platform has no simulation model set {name!r}") from exc


@dataclass(frozen=True)
class LayoutPlatformContract:
    """Source-owned layout contract with logical verification assets."""

    layout_path: Path
    verification_path: Path
    dbu_per_micron: int
    layermap: PurePosixPath
    drc_deck: PurePosixPath
    lvs_deck: PurePosixPath
    qrc_tech_file: PurePosixPath | None
    xstream_flatten_pcells: bool = True
    xstream_suppressed_warnings: tuple[str, ...] = ()
    xstream_bin: PurePosixPath | None = None
    calibre_bin: PurePosixPath | None = None
    oa_materialization: OaMaterializationMapping | None = None


_PDK_CONFIG_AUTHORITY = object()


@dataclass(frozen=True)
class PdkConfig:
    """Loader-sealed platform and its validated operation source snapshot."""

    _authority: InitVar[object]
    key: str
    path: Path
    owner: str
    name: str
    simulation: SimulationPlatformConfig
    oa: OaPlatformConfig
    layout: LayoutPdkConfig | None
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
        if _authority is not _PDK_CONFIG_AUTHORITY:
            raise ValueError("platform snapshots must be built by the platform loader")

    @property
    def runtime_bound(self) -> bool:
        """Whether external asset paths were resolved for this invocation."""

        return self.asset_root is not None


_PLATFORM_CONTRACT_AUTHORITY = object()


@dataclass(frozen=True)
class PlatformContract:
    """Project-owned platform contract with logical, unresolved asset references."""

    _authority: InitVar[object]
    key: str
    path: Path
    owner: str
    name: str
    simulation: SimulationPlatformContract
    oa: OaPlatformConfig
    layout: LayoutPlatformContract | None
    asset_root_resource: str | None
    source_paths: tuple[Path, ...]
    catalog_document: Mapping[str, Any]
    source_documents: Mapping[Path, Mapping[str, Any]]

    def __post_init__(self, _authority: object) -> None:
        if _authority is not _PLATFORM_CONTRACT_AUTHORITY:
            raise ValueError("platform contracts must be built by the platform loader")
        if any(
            not isinstance(path, PurePosixPath)
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
            for path in self.asset_paths
        ):
            raise ValueError("platform contract asset paths must be logical and relative")

    @property
    def asset_paths(self) -> tuple[PurePosixPath, ...]:
        assets = [
            path
            for model_set in self.simulation.model_sets.values()
            for path in model_set.files
        ]
        if self.layout is not None:
            assets.extend(
                path
                for path in (
                    self.layout.layermap,
                    self.layout.drc_deck,
                    self.layout.lvs_deck,
                    self.layout.qrc_tech_file,
                    self.layout.xstream_bin,
                    self.layout.calibre_bin,
                )
                if path is not None
            )
        return tuple(sorted(set(assets)))

    @property
    def runtime_bound(self) -> bool:
        return False


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


class PlatformInventory(Mapping[str, PdkConfig]):
    """Validated platform set trusted only within one repository operation.

    Standalone APIs accept only loader-sealed ``PdkConfig`` snapshots and
    validate their complete source identity. Repository workflows use this
    immutable capability after loading the catalog and every selected platform
    once.
    """

    __slots__ = ("_catalog", "_platforms", "_project")

    def __init__(
        self,
        *,
        _authority: object,
        project: Project,
        catalog: PlatformCatalogSnapshot,
        platforms: Mapping[str, PdkConfig],
    ) -> None:
        if _authority is not _PLATFORM_INVENTORY_AUTHORITY:
            raise ValueError("platform inventory must be built by its loader")
        if (
            catalog.project_root != project.project_root
            or catalog.path != project.catalog("platform")
        ):
            raise ValueError("platform inventory catalog identity drift")
        _validate_immutable_platform_catalog(catalog)
        selected = dict(platforms)
        if set(selected) != set(catalog.manifests):
            raise ValueError("platform inventory does not cover its complete catalog")
        for key, platform in selected.items():
            _validate_immutable_platform_snapshot(platform)
            if (
                not platform.runtime_bound
                or platform.key != key
                or platform.path != catalog.manifest(key)
                or platform.catalog_document != catalog.document
                or not platform.source_paths
                or platform.source_paths[0] != catalog.path
                or set(platform.source_documents) != set(platform.source_paths[1:])
            ):
                raise ValueError("platform inventory identity drift")
        object.__setattr__(self, "_project", project)
        object.__setattr__(self, "_catalog", catalog)
        object.__setattr__(self, "_platforms", MappingProxyType(selected))

    @property
    def project(self) -> Project:
        return self._project

    @property
    def catalog(self) -> PlatformCatalogSnapshot:
        return self._catalog

    @property
    def platforms(self) -> Mapping[str, PdkConfig]:
        return self._platforms

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("platform inventory is immutable")

    def __getitem__(self, key: str) -> PdkConfig:
        return self.platforms[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.platforms)

    def __len__(self) -> int:
        return len(self.platforms)

    def resolve_catalog(self, context: Project) -> PlatformCatalogSnapshot:
        """Return the catalog after a cheap operation-identity check."""

        if (
            context is not self.project
            or self.catalog.project_root != context.project_root
            or self.catalog.path != context.catalog("platform")
        ):
            raise ValueError("platform inventory belongs to a different operation")
        return self.catalog

    def resolve(self, context: Project, key: str) -> PdkConfig:
        """Select one already-validated platform for this exact operation."""

        self.resolve_catalog(context)
        try:
            platform = self.platforms[key]
        except KeyError as exc:
            raise ValueError(f"platform inventory has no {key!r} entry") from exc
        if (
            self.catalog.manifest(key) != platform.path
            or platform.key != key
            or not platform.source_paths
            or platform.source_paths[0] != self.catalog.path
        ):
            raise ValueError("platform inventory identity drift")
        return platform


class PlatformContractInventory(Mapping[str, PlatformContract]):
    """Complete source-only platform catalog for repository planning."""

    __slots__ = ("_catalog", "_platforms", "_project")

    def __init__(
        self,
        *,
        _authority: object,
        project: Project,
        catalog: PlatformCatalogSnapshot,
        platforms: Mapping[str, PlatformContract],
    ) -> None:
        if _authority is not _PLATFORM_INVENTORY_AUTHORITY:
            raise ValueError("platform contract inventory must be built by its loader")
        if (
            catalog.project_root != project.project_root
            or catalog.path != project.catalog("platform")
        ):
            raise ValueError("platform contract inventory catalog identity drift")
        _validate_immutable_platform_catalog(catalog)
        selected = dict(platforms)
        if set(selected) != set(catalog.manifests):
            raise ValueError("platform contract inventory is incomplete")
        for key, contract in selected.items():
            if (
                not isinstance(contract, PlatformContract)
                or contract.key != key
                or contract.path != catalog.manifest(key)
                or contract.catalog_document != catalog.document
            ):
                raise ValueError("platform contract inventory identity drift")
        object.__setattr__(self, "_project", project)
        object.__setattr__(self, "_catalog", catalog)
        object.__setattr__(self, "_platforms", MappingProxyType(selected))

    @property
    def project(self) -> Project:
        return self._project

    @property
    def catalog(self) -> PlatformCatalogSnapshot:
        return self._catalog

    @property
    def platforms(self) -> Mapping[str, PlatformContract]:
        return self._platforms

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("platform contract inventory is immutable")

    def __getitem__(self, key: str) -> PlatformContract:
        return self.platforms[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.platforms)

    def __len__(self) -> int:
        return len(self.platforms)

    def resolve(self, context: Project, key: str) -> PlatformContract:
        if context is not self.project:
            raise ValueError("platform contract inventory belongs to another operation")
        try:
            return self.platforms[key]
        except KeyError as exc:
            raise ValueError(f"platform contract inventory has no {key!r} entry") from exc


PlatformSnapshot = (
    PdkConfig
    | PlatformInventory
    | PlatformContract
    | PlatformContractInventory
)
ResolvedPlatform = PdkConfig | PlatformContract
ResolvedLayoutPlatform = LayoutPdkConfig | LayoutPlatformContract


def _validate_immutable_platform_catalog(
    snapshot: PlatformCatalogSnapshot,
) -> None:
    if not isinstance(snapshot.manifests, _MAPPING_PROXY_TYPE) or not (
        is_frozen_toml_document(snapshot.document)
    ):
        raise ValueError("platform catalog snapshot immutable identity drift")


def _validate_immutable_platform_snapshot(snapshot: PdkConfig) -> None:
    if not is_frozen_toml_document(snapshot.catalog_document):
        raise ValueError("platform snapshot catalog document identity drift")
    if not isinstance(snapshot.source_documents, _MAPPING_PROXY_TYPE) or any(
        not is_frozen_toml_document(document)
        for document in snapshot.source_documents.values()
    ):
        raise ValueError("platform snapshot source document identity drift")
    typed_mappings: list[Mapping[str, object]] = [
        snapshot.simulation.model_sets,
        snapshot.oa.primitive_subcircuits,
    ]
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


def resolve_platform(
    context: Project,
    key: str,
    *,
    resources: PlatformResources,
    snapshot: PdkConfig | None = None,
) -> PdkConfig:
    """Load a platform or validate one loader-sealed plan snapshot.

    ``resources`` is the only source of host-dependent platform facts.  A
    supplied snapshot is still checked against the explicit resources so
    that changing an external asset root cannot silently change an operation.
    """

    if not isinstance(resources, PlatformResources):
        raise TypeError("platform resources must provide require_directory")
    if snapshot is None:
        return load_platform(context, key, resources=resources)
    return _validate_platform_snapshot(
        context,
        key,
        snapshot,
        resources=resources,
    )


def _validate_platform_snapshot(
    context: Project,
    key: str,
    snapshot: PdkConfig,
    *,
    resources: PlatformResources | None,
) -> PdkConfig:
    """Validate a platform snapshot without consulting ambient process state."""

    if snapshot is None:
        raise TypeError("platform snapshot must be PdkConfig")
    if snapshot.key != key:
        raise ValueError(
            f"platform snapshot {snapshot.key!r} disagrees with requested key {key!r}"
        )
    _validate_immutable_platform_snapshot(snapshot)
    root = context.project_root
    catalog_path = context.catalog("platform")
    if (
        not isinstance(snapshot.catalog_document, Mapping)
        or not snapshot.catalog_document
    ):
        raise ValueError("platform snapshot omits its platform catalog")
    catalog_root, selected_catalog_path, _catalog_owner, platforms = (
        _platform_catalog_document(
            context,
            snapshot.catalog_document,
        )
    )
    try:
        manifest_value = platforms[key]
    except KeyError as exc:
        raise ValueError(f"platform catalog has no {key!r} entry") from exc
    selected_manifest = _safe_relative(
        selected_catalog_path.parent,
        manifest_value,
        f"platforms.{key}",
        root=catalog_root,
    )
    if (
        selected_catalog_path != catalog_path
        or selected_manifest != snapshot.path
        or not snapshot.source_paths
        or snapshot.source_paths[0] != catalog_path
    ):
        raise ValueError("platform snapshot belongs to a different project catalog")
    if snapshot.path not in snapshot.source_paths:
        raise ValueError("platform snapshot manifest is absent from its source identity")
    if any(
        not isinstance(path, Path)
        or path != path.resolve()
        or not path.is_relative_to(root)
        or not path.is_file()
        for path in snapshot.source_paths
    ):
        raise ValueError("platform snapshot source identity drift")
    document_paths = set(snapshot.source_documents)
    if not document_paths:
        raise ValueError("platform snapshot source document identity drift")
    if document_paths:
        if document_paths != set(snapshot.source_paths[1:]) or any(
            path != path.resolve() or not path.is_relative_to(root)
            for path in document_paths
        ):
            raise ValueError("platform snapshot source document identity drift")
        manifest_document = snapshot.source_documents.get(snapshot.path)
        if not isinstance(manifest_document, Mapping):
            raise ValueError("platform snapshot source document drift")
        require_config_header(
            manifest_document,
            snapshot.path,
            contract_kind="platform-definition",
            path_scope="platform",
            owner=snapshot.owner,
        )
        _reject_unknown(
            manifest_document,
            _HEADER_FIELDS | {"key", "name", "asset_scope", "contracts"},
            "platform definition",
        )
        resolved_asset_root, root_resource = _platform_asset_root(
            snapshot.path,
            key,
            manifest_document,
            resources=resources,
        )
        asset_root = (
            snapshot.asset_root
            if resources is None and root_resource is not None
            else resolved_asset_root
        )
        if (
            manifest_document.get("key") != key
            or _text(manifest_document.get("name", key), "platform.name")
            != snapshot.name
            or snapshot.asset_root != asset_root
            or snapshot.asset_root_resource != root_resource
        ):
            raise ValueError("platform identity drift")
        contract_asset_root = asset_root or snapshot.path.parent
        contracts = manifest_document.get("contracts")
        if not isinstance(contracts, Mapping):
            raise ValueError("platform snapshot source document drift")
        allowed_contracts = {"simulation", "oa", "layout", "verification"}
        if set(contracts) - allowed_contracts or not {
            "simulation",
            "oa",
        }.issubset(contracts):
            raise ValueError("platform snapshot source document drift")
        if ("layout" in contracts) != ("verification" in contracts):
            raise ValueError("platform snapshot source document drift")
        simulation_path = _safe_relative(
            snapshot.path.parent,
            contracts.get("simulation"),
            "platform.contracts.simulation",
            root=root,
        )
        oa_path = _safe_relative(
            snapshot.path.parent,
            contracts.get("oa"),
            "platform.contracts.oa",
            root=root,
        )
        simulation_document = snapshot.source_documents.get(simulation_path)
        oa_document = snapshot.source_documents.get(oa_path)
        if not isinstance(simulation_document, Mapping) or not isinstance(
            oa_document,
            Mapping,
        ):
            raise ValueError("platform identity drift")
        require_config_header(
            simulation_document,
            simulation_path,
            contract_kind="platform-simulation",
            path_scope="platform",
            owner=snapshot.owner,
        )
        require_config_header(
            oa_document,
            oa_path,
            contract_kind="platform-oa",
            path_scope="platform",
            owner=snapshot.owner,
        )
        parsed_simulation = _load_simulation(
            simulation_path,
            simulation_document,
            asset_root=contract_asset_root,
        )
        parsed_oa = _load_oa(oa_path, oa_document)
        if parsed_simulation != snapshot.simulation or parsed_oa != snapshot.oa:
            raise ValueError("platform identity drift")
        has_layout_contracts = "layout" in contracts
        if (snapshot.layout is None) != (not has_layout_contracts):
            raise ValueError("platform identity drift")
        contract_paths = [simulation_path, oa_path]
        if snapshot.layout is not None:
            layout_path = _safe_relative(
                snapshot.path.parent,
                contracts.get("layout"),
                "platform.contracts.layout",
                root=root,
            )
            verification_path = _safe_relative(
                snapshot.path.parent,
                contracts.get("verification"),
                "platform.contracts.verification",
                root=root,
            )
            layout_document = snapshot.source_documents.get(layout_path)
            verification_document = snapshot.source_documents.get(verification_path)
            if (
                snapshot.layout.layout_path != layout_path
                or snapshot.layout.verification_path != verification_path
                or not isinstance(layout_document, Mapping)
                or not isinstance(verification_document, Mapping)
            ):
                raise ValueError("platform identity drift")
            require_config_header(
                layout_document,
                layout_path,
                contract_kind="platform-layout",
                path_scope="platform",
                owner=snapshot.owner,
            )
            require_config_header(
                verification_document,
                verification_path,
                contract_kind="platform-verification",
                path_scope="platform",
                owner=snapshot.owner,
            )
            parsed_layout = _load_layout(
                layout_path,
                layout_document,
                verification_path,
                verification_document,
                asset_root=contract_asset_root,
                require_assets=asset_root is not None,
            )
            if parsed_layout != snapshot.layout:
                raise ValueError("platform identity drift")
            contract_paths.extend((layout_path, verification_path))
        if snapshot.source_paths != (
            catalog_path,
            snapshot.path,
            *contract_paths,
        ):
            raise ValueError("platform snapshot source identity drift")
    return snapshot


def resolve_platform_snapshot(
    context: Project,
    key: str,
    *,
    snapshot: PlatformSnapshot | None = None,
) -> ResolvedPlatform:
    """Resolve a public snapshot or select an operation-trusted inventory."""

    if isinstance(snapshot, PlatformInventory):
        return snapshot.resolve(context, key)
    if isinstance(snapshot, PlatformContractInventory):
        return snapshot.resolve(context, key)
    if isinstance(snapshot, PlatformContract):
        if snapshot.key != key:
            raise ValueError("platform contract disagrees with requested key")
        current = load_platform_contract(context, key)
        if current != snapshot:
            raise ValueError("platform contract source identity drift")
        return snapshot
    if snapshot is None:
        return load_platform_contract(context, key)
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
    validated = parse_platform_catalog(context, snapshot.document)
    if (
        snapshot.project_root != validated.project_root
        or snapshot.path != validated.path
    ):
        raise ValueError("platform catalog snapshot belongs to a different project")
    if snapshot.owner != validated.owner or snapshot.manifests != validated.manifests:
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


def _asset_path(base: Path, value: object, field: str) -> Path:
    text = _text(value, field)
    relative = Path(text)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{field} must be a safe asset-relative path")
    return (base / relative).resolve()


def _asset_source_path(base: Path, value: object, field: str) -> Path:
    text = _text(value, field)
    relative = Path(text)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{field} must be a safe asset-relative path")
    configured = (base / relative).absolute()
    if configured != configured.resolve():
        raise ValueError(f"{field} must not traverse a symlink")
    return configured


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


def _required_file(
    base: Path,
    value: object,
    field: str,
    *,
    require_asset: bool = True,
) -> Path:
    result = _asset_source_path(base, value, field)
    if require_asset and not result.is_file():
        raise ValueError(f"{field} does not exist: {result}")
    return result


def _optional_executable(
    base: Path,
    value: object,
    field: str,
    *,
    require_asset: bool = True,
) -> Path | None:
    if value is None:
        return None
    result = _asset_path(base, value, field)
    if require_asset and (not result.is_file() or not os.access(result, os.X_OK)):
        raise ValueError(f"{field} must be executable: {result}")
    return result


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
        path_scope="platform",
        owner=owner,
    )
    return path, raw


def _load_simulation(
    path: Path, raw: Mapping[str, Any], *, asset_root: Path
) -> SimulationPlatformConfig:
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
            _asset_source_path(
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
            file=_asset_source_path(
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
    return SimulationPlatformConfig(
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


def load_oa_materialization_mapping(
    path: Path,
) -> tuple[int, OaMaterializationMapping]:
    """Load one atomic platform layout contract for an OA backend."""

    contract_path = Path(path).resolve()
    raw = read_toml(contract_path)
    require_config_header(
        raw,
        contract_path,
        contract_kind="platform-layout",
        path_scope="platform",
    )
    _reject_unknown(
        raw,
        _HEADER_FIELDS | {"dbu_per_micron", "oa_materialization", "custom_layout"},
        "platform layout contract",
    )
    dbu = raw.get("dbu_per_micron")
    if isinstance(dbu, bool) or not isinstance(dbu, int) or dbu <= 0:
        raise ValueError("layout.dbu_per_micron must be a positive integer")
    mapping = _parse_oa_materialization(raw.get("oa_materialization"))
    if mapping is None:
        raise ValueError("platform layout contract omits oa_materialization")
    return dbu, mapping


def _load_layout(
    layout_path: Path,
    layout_raw: Mapping[str, Any],
    verification_path: Path,
    verification_raw: Mapping[str, Any],
    *,
    asset_root: Path,
    require_assets: bool = True,
) -> LayoutPdkConfig:
    _reject_unknown(
        layout_raw,
        _HEADER_FIELDS | {"dbu_per_micron", "oa_materialization", "custom_layout"},
        "platform layout contract",
    )
    _reject_unknown(
        verification_raw,
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
        "platform verification contract",
    )
    dbu = layout_raw.get("dbu_per_micron")
    if isinstance(dbu, bool) or not isinstance(dbu, int) or dbu <= 0:
        raise ValueError("layout.dbu_per_micron must be a positive integer")
    oa_materialization = _parse_oa_materialization(
        layout_raw.get("oa_materialization")
    )
    xstream_flatten = verification_raw.get("xstream_flatten_pcells", True)
    if not isinstance(xstream_flatten, bool):
        raise ValueError("verification.xstream_flatten_pcells must be boolean")
    warnings = _strings(
        verification_raw.get("xstream_suppressed_warnings", []),
        "verification.xstream_suppressed_warnings",
    )
    if any(
        not warning.startswith("XSTRM-")
        or not warning.removeprefix("XSTRM-").isdigit()
        for warning in warnings
    ):
        raise ValueError("xstream warnings must use XSTRM-<number> identities")
    return LayoutPdkConfig(
        layout_path=layout_path,
        verification_path=verification_path,
        dbu_per_micron=dbu,
        layermap=_required_file(
            asset_root,
            verification_raw.get("layermap"),
            "layermap",
            require_asset=require_assets,
        ),
        drc_deck=_required_file(
            asset_root,
            verification_raw.get("drc_deck"),
            "drc_deck",
            require_asset=require_assets,
        ),
        lvs_deck=_required_file(
            asset_root,
            verification_raw.get("lvs_deck"),
            "lvs_deck",
            require_asset=require_assets,
        ),
        qrc_tech_file=(
            _required_file(
                asset_root,
                verification_raw["qrc_tech_file"],
                "qrc_tech_file",
                require_asset=require_assets,
            )
            if "qrc_tech_file" in verification_raw
            else None
        ),
        xstream_flatten_pcells=xstream_flatten,
        xstream_suppressed_warnings=warnings,
        xstream_bin=_optional_executable(
            asset_root,
            verification_raw.get("xstream_bin"),
            "xstream_bin",
            require_asset=require_assets,
        ),
        calibre_bin=_optional_executable(
            asset_root,
            verification_raw.get("calibre_bin"),
            "calibre_bin",
            require_asset=require_assets,
        ),
        oa_materialization=oa_materialization,
    )


def _platform_catalog_document(
    context: Project,
    document: Mapping[str, Any],
) -> tuple[Path, Path, str, Mapping[str, Any]]:
    root = context.project_root
    catalog_path = context.catalog("platform")
    if not catalog_path.is_file() or not catalog_path.is_relative_to(root):
        raise ValueError("platform catalog must be a project-owned file")
    header = require_config_header(
        document,
        catalog_path,
        contract_kind="platform-catalog",
        path_scope="repository",
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
) -> PdkConfig:
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
        _HEADER_FIELDS | {"key", "name", "asset_scope", "contracts"},
        "platform definition",
    )
    if raw.get("key") != key:
        raise ValueError(f"platform manifest key must be {key!r}")
    asset_root, root_resource = _platform_asset_root(
        manifest,
        key,
        raw,
        resources=resources,
    )
    contract_asset_root = asset_root or manifest.parent
    contracts = _table(raw.get("contracts"), "platform.contracts")
    allowed = {"simulation", "oa", "layout", "verification"}
    if set(contracts) - allowed or not {"simulation", "oa"}.issubset(contracts):
        raise ValueError(
            "platform contracts must declare simulation and oa, with optional "
            "layout and verification"
        )
    if ("layout" in contracts) != ("verification" in contracts):
        raise ValueError("platform layout and verification contracts must be paired")
    simulation_contract = _contract(
        manifest, contracts, "simulation", root=root, owner=header.owner
    )
    oa_contract = _contract(manifest, contracts, "oa", root=root, owner=header.owner)
    assert simulation_contract is not None and oa_contract is not None
    simulation_path, simulation_raw = simulation_contract
    oa_path, oa_raw = oa_contract
    simulation = _load_simulation(
        simulation_path, simulation_raw, asset_root=contract_asset_root
    )
    oa = _load_oa(oa_path, oa_raw)
    source_paths: list[Path] = [
        catalog_path,
        manifest,
        simulation_path,
        oa_path,
    ]
    layout: LayoutPdkConfig | None = None
    if "layout" in contracts:
        layout_contract = _contract(
            manifest, contracts, "layout", root=root, owner=header.owner
        )
        verification_contract = _contract(
            manifest, contracts, "verification", root=root, owner=header.owner
        )
        assert layout_contract is not None and verification_contract is not None
        layout_path, layout_raw = layout_contract
        verification_path, verification_raw = verification_contract
        layout = _load_layout(
            layout_path,
            layout_raw,
            verification_path,
            verification_raw,
            asset_root=contract_asset_root,
            require_assets=asset_root is not None,
        )
        source_paths.extend((layout_path, verification_path))
    sources = tuple(source_paths)
    source_documents = {
        manifest: raw,
        simulation_path: simulation_raw,
        oa_path: oa_raw,
    }
    if layout is not None:
        source_documents[layout.layout_path] = layout_raw
        source_documents[layout.verification_path] = verification_raw
    return PdkConfig(
        _authority=_PDK_CONFIG_AUTHORITY,
        key=key,
        path=manifest,
        owner=header.owner,
        name=_text(raw.get("name", key), "platform.name"),
        simulation=simulation,
        oa=oa,
        layout=layout,
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
    resources: PlatformResources,
    catalog: PlatformCatalogSnapshot | None = None,
) -> PdkConfig:
    """Resolve one platform against explicit runtime assets."""

    if not isinstance(resources, PlatformResources):
        raise TypeError("platform resources must provide require_directory")
    platform = _load_platform(
        context,
        key,
        resources=resources,
        catalog=catalog,
    )
    if platform.asset_root is None:
        raise ValueError("platform runtime asset root was not resolved")
    return platform


def load_platform_contract(
    context: Project,
    key: str,
    *,
    catalog: PlatformCatalogSnapshot | None = None,
) -> PlatformContract:
    """Validate one platform's project-owned source without host bindings."""

    platform = _load_platform(context, key, resources=None, catalog=catalog)
    asset_base = platform.asset_root or platform.path.parent

    def logical(path: Path) -> PurePosixPath:
        try:
            relative = path.relative_to(asset_base)
        except ValueError as exc:
            raise ValueError("platform asset escaped its logical root") from exc
        return PurePosixPath(relative.as_posix())

    model_sets = {
        name: SimulationModelContract(
            name=model_set.name,
            file=logical(model_set.file),
            sections=model_set.sections,
            support_files=tuple(logical(path) for path in model_set.support_files),
        )
        for name, model_set in platform.simulation.model_sets.items()
    }
    simulation = SimulationPlatformContract(
        path=platform.simulation.path,
        default_model_set=platform.simulation.default_model_set,
        model_sets=MappingProxyType(model_sets),
    )
    runtime_layout = platform.layout
    layout = (
        None
        if runtime_layout is None
        else LayoutPlatformContract(
            layout_path=runtime_layout.layout_path,
            verification_path=runtime_layout.verification_path,
            dbu_per_micron=runtime_layout.dbu_per_micron,
            layermap=logical(runtime_layout.layermap),
            drc_deck=logical(runtime_layout.drc_deck),
            lvs_deck=logical(runtime_layout.lvs_deck),
            qrc_tech_file=(
                None
                if runtime_layout.qrc_tech_file is None
                else logical(runtime_layout.qrc_tech_file)
            ),
            xstream_flatten_pcells=runtime_layout.xstream_flatten_pcells,
            xstream_suppressed_warnings=(
                runtime_layout.xstream_suppressed_warnings
            ),
            xstream_bin=(
                None
                if runtime_layout.xstream_bin is None
                else logical(runtime_layout.xstream_bin)
            ),
            calibre_bin=(
                None
                if runtime_layout.calibre_bin is None
                else logical(runtime_layout.calibre_bin)
            ),
            oa_materialization=runtime_layout.oa_materialization,
        )
    )
    return PlatformContract(
        _authority=_PLATFORM_CONTRACT_AUTHORITY,
        key=platform.key,
        path=platform.path,
        owner=platform.owner,
        name=platform.name,
        simulation=simulation,
        oa=platform.oa,
        layout=layout,
        asset_root_resource=platform.asset_root_resource,
        source_paths=platform.source_paths,
        catalog_document=platform.catalog_document,
        source_documents=platform.source_documents,
    )


def load_platform_inventory(
    context: Project,
    *,
    resources: PlatformResources,
    catalog: PlatformCatalogSnapshot | None = None,
) -> PlatformInventory:
    """Load the complete project platform set once for one operation."""

    selected_catalog = (
        load_platform_catalog(context)
        if catalog is None
        else resolve_platform_catalog(context, snapshot=catalog)
    )
    if not isinstance(resources, PlatformResources):
        raise TypeError("platform resources must provide require_directory")
    platforms = {
        key: load_platform(context, key, resources=resources, catalog=selected_catalog)
        for key in selected_catalog.manifests
    }
    return PlatformInventory(
        _authority=_PLATFORM_INVENTORY_AUTHORITY,
        project=context,
        catalog=selected_catalog,
        platforms=MappingProxyType(platforms),
    )


def load_platform_contract_inventory(
    context: Project,
    *,
    catalog: PlatformCatalogSnapshot | None = None,
) -> PlatformContractInventory:
    """Validate every project-owned platform contract without host assets."""

    selected_catalog = (
        load_platform_catalog(context)
        if catalog is None
        else resolve_platform_catalog(context, snapshot=catalog)
    )
    platforms = {
        key: load_platform_contract(context, key, catalog=selected_catalog)
        for key in selected_catalog.manifests
    }
    return PlatformContractInventory(
        _authority=_PLATFORM_INVENTORY_AUTHORITY,
        project=context,
        catalog=selected_catalog,
        platforms=MappingProxyType(platforms),
    )
