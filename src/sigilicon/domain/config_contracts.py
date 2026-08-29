"""Shared metadata checks for repository-owned TOML contracts.

The repository intentionally keeps several domain-specific TOML schemas.  This
module does not flatten those schemas; it validates the small common envelope
that makes ownership, scope, and migration boundaries explicit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
import tomllib
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from sigilicon.domain.design import DesignSpec
    from sigilicon.domain.ip_integration import IpIntegrationContract
    from sigilicon.domain.ip_release import IpContract
    from sigilicon.domain.oa_library import OALibrarySource
    from sigilicon.domain.oa_simulation import OASimulationSpec
    from sigilicon.domain.repository import OwnerCatalogSnapshot, Project
    from sigilicon.domain.platform import PdkConfig, PlatformCatalogSnapshot


CONFIG_SCHEMA = 1
PATH_SCOPES = frozenset(
    {"repository", "owner", "cell", "verification", "platform", "variant"}
)
_MAPPING_PROXY_TYPE = type(MappingProxyType({}))


def freeze_toml_document(value: Any) -> Any:
    """Recursively freeze one parsed TOML value for operation snapshots."""

    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: freeze_toml_document(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(freeze_toml_document(item) for item in value)
    return value


def is_frozen_toml_document(value: object) -> bool:
    """Return whether one parsed TOML value is recursively immutable."""

    if isinstance(value, _MAPPING_PROXY_TYPE):
        return all(
            isinstance(key, str) and is_frozen_toml_document(item)
            for key, item in value.items()
        )
    if isinstance(value, tuple):
        return all(is_frozen_toml_document(item) for item in value)
    return isinstance(value, (str, int, float, bool, datetime, date, time))


def thaw_toml_document(value: Any) -> Any:
    """Copy a frozen TOML value back to the parser's dict/list shape."""

    if isinstance(value, Mapping):
        return {key: thaw_toml_document(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_toml_document(item) for item in value]
    return value


@dataclass(frozen=True)
class ConfigHeader:
    schema: int
    contract_kind: str
    path_scope: str
    owner: str


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def require_config_header(
    raw: Mapping[str, Any],
    path: Path,
    *,
    contract_kind: str | tuple[str, ...],
    path_scope: str | tuple[str, ...],
    owner: str | None = None,
    schema: int = CONFIG_SCHEMA,
) -> ConfigHeader:
    """Validate the common header for one domain-specific TOML document."""

    actual_schema = raw.get("schema")
    if isinstance(actual_schema, bool) or actual_schema != schema:
        raise ValueError(f"{path}: schema must be {schema}")
    actual_kind = _text(raw.get("contract_kind"), f"{path}: contract_kind")
    allowed_kinds = (contract_kind,) if isinstance(contract_kind, str) else contract_kind
    if actual_kind not in allowed_kinds:
        raise ValueError(
            f"{path}: contract_kind must be one of {sorted(allowed_kinds)}"
        )
    actual_scope = _text(raw.get("path_scope"), f"{path}: path_scope")
    allowed_scopes = (path_scope,) if isinstance(path_scope, str) else path_scope
    if actual_scope not in allowed_scopes or actual_scope not in PATH_SCOPES:
        raise ValueError(
            f"{path}: path_scope must be one of {sorted(set(allowed_scopes) & PATH_SCOPES)}"
        )
    actual_owner = _text(raw.get("owner"), f"{path}: owner")
    if owner is not None and actual_owner != owner:
        raise ValueError(f"{path}: owner must be {owner!r}, got {actual_owner!r}")
    return ConfigHeader(
        schema=schema,
        contract_kind=actual_kind,
        path_scope=actual_scope,
        owner=actual_owner,
    )


def read_toml(path: Path) -> dict[str, Any]:
    """Read one TOML document and require a table root."""

    try:
        with path.open("rb") as stream:
            value = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read TOML {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"TOML root must be a table: {path}")
    return value


def _merge_source_documents(
    target: dict[Path, Mapping[str, Any]],
    source_documents: Mapping[Path, Mapping[str, Any]],
    *,
    label: str,
) -> None:
    for path, document in source_documents.items():
        resolved = path.resolve()
        previous = target.get(resolved)
        if previous is not None and previous != document:
            raise ValueError(f"{label} disagrees with another source: {path}")
        target[resolved] = document


def inspect_project_configurations(
    context: Project,
    *,
    owner_roots: Mapping[str, Path],
    catalog_inventory: tuple[OwnerCatalogSnapshot, ...] | None = None,
    platform_catalog: PlatformCatalogSnapshot | None = None,
    platform_inventory: Mapping[str, PdkConfig] | None = None,
    release_inventory: Mapping[str, IpContract] | None = None,
    integration_inventory: Mapping[str, IpIntegrationContract] | None = None,
    oa_source_inventory: Mapping[Path, OALibrarySource] | None = None,
    oa_simulation_inventory: Mapping[Path, OASimulationSpec] | None = None,
    oa_design_inventory: Mapping[Path, DesignSpec] | None = None,
    layout_source_documents: Mapping[Path, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate TOML below the roots selected by repository catalogs.

    Catalog locations define repository, IP-owner, and platform roots.
    Domain loaders remain responsible for native schemas; this pass proves that
    every TOML is parseable and that every common metadata envelope is complete.
    """

    root = context.project_root.resolve()
    project_contract = context.manifest_path
    repository_owner = context.manifest_owner
    flow_catalog_inventory = (
        context.flow_catalog_inventory()
        if catalog_inventory is None
        else catalog_inventory
    )
    design_catalogs = context.flow_catalog_snapshots(
        "design_targets",
        inventory=flow_catalog_inventory,
    )
    layout_catalogs = context.flow_catalog_snapshots(
        "layout_targets",
        inventory=flow_catalog_inventory,
    )
    catalog_documents = {
        snapshot.path.resolve(): snapshot.document
        for snapshot in flow_catalog_inventory
    }
    manifest_document = context.manifest_source_document()
    if manifest_document:
        catalog_documents[project_contract] = manifest_document
    catalog_documents.update(
        {
            owner.component.path: owner.component.document
            for owner in context.owners
        }
    )
    ip_catalog = context.ip_catalog_snapshot()
    catalog_documents[ip_catalog.path] = ip_catalog.document
    if release_inventory is not None:
        from sigilicon.domain.ip_release import resolve_ip_contract

        release_rows = ip_catalog.document.get("targets", {})
        if not isinstance(release_rows, Mapping):
            raise ValueError("IP catalog targets must be a table")
        for name, contract in release_inventory.items():
            row = release_rows.get(name)
            if (
                not isinstance(row, Mapping)
                or set(row) != {"contract"}
                or not isinstance(row.get("contract"), str)
            ):
                raise ValueError(
                    f"IP release snapshot has no matching catalog target: {name}"
                )
            relative = Path(row["contract"])
            expected = (root / relative).resolve()
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or not expected.is_relative_to(root)
                or not expected.is_file()
                or contract.project is not context
                or contract.name != name
                or contract.path != expected
            ):
                raise ValueError(f"IP release snapshot identity drift: {name}")
            if contract.document:
                catalog_documents[contract.path] = contract.document
            contract = resolve_ip_contract(
                contract.path,
                project=context,
                snapshot=contract,
            )
            _merge_source_documents(
                catalog_documents,
                contract.interface_documents,
                label="IP release interface snapshot",
            )
    if integration_inventory is not None:
        from sigilicon.domain.ip_integration import resolve_ip_integration_contract

        component_rows = ip_catalog.document.get("components", {})
        if not isinstance(component_rows, Mapping):
            raise ValueError("IP catalog components must be a table")
        for name, contract in integration_inventory.items():
            row = component_rows.get(name)
            if (
                not isinstance(row, Mapping)
                or set(row) != {"contract", "root"}
                or not isinstance(row.get("contract"), str)
            ):
                raise ValueError(
                    f"IP integration snapshot has no matching component: {name}"
                )
            relative = Path(row["contract"])
            expected = (root / relative).resolve()
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or contract.project is not context
                or contract.name != name
                or contract.path != expected
            ):
                raise ValueError(f"IP integration snapshot identity drift: {name}")
            contract = resolve_ip_integration_contract(
                contract.path,
                project=context,
                snapshot=contract,
            )
            _merge_source_documents(
                catalog_documents,
                contract.source_documents,
                label="IP integration source snapshot",
            )
    if oa_source_inventory is not None:
        from sigilicon.domain.oa_library import resolve_oa_library_source

        for manifest, snapshot in oa_source_inventory.items():
            source = resolve_oa_library_source(
                manifest,
                project=context,
                snapshot=snapshot,
            )
            _merge_source_documents(
                catalog_documents,
                source.source_documents,
                label="OA source snapshot",
            )
    if oa_simulation_inventory is not None:
        from sigilicon.domain.oa_simulation import resolve_oa_simulation_spec

        for path, snapshot in oa_simulation_inventory.items():
            simulation = resolve_oa_simulation_spec(
                path,
                project=context,
                snapshot=snapshot,
            )
            _merge_source_documents(
                catalog_documents,
                simulation.source_documents,
                label="OA simulation snapshot",
            )
    if oa_design_inventory is not None:
        from sigilicon.domain.design import resolve_design_spec

        for path, snapshot in oa_design_inventory.items():
            design = resolve_design_spec(
                path,
                project=context,
                snapshot=snapshot,
            )
            _merge_source_documents(
                catalog_documents,
                design.source_documents,
                label="design snapshot",
            )
    if layout_source_documents is not None:
        _merge_source_documents(
            catalog_documents,
            layout_source_documents,
            label="layout snapshot",
        )
    resolved_platform_catalog = None
    if platform_inventory is not None and platform_catalog is None:
        raise ValueError("platform inventory requires its platform catalog snapshot")
    if platform_catalog is not None:
        from sigilicon.domain.platform import resolve_platform_catalog

        resolved_platform_catalog = resolve_platform_catalog(
            context,
            snapshot=platform_catalog,
        )
        catalog_documents[resolved_platform_catalog.path] = (
            resolved_platform_catalog.document
        )
    if platform_inventory is not None:
        from sigilicon.domain.platform import resolve_platform

        for key, snapshot in platform_inventory.items():
            assert resolved_platform_catalog is not None
            if resolved_platform_catalog.manifest(key) != snapshot.path:
                raise ValueError(
                    "platform snapshot manifest disagrees with its catalog"
                )
            platform = resolve_platform(
                context,
                key,
                snapshot=snapshot,
            )
            _merge_source_documents(
                catalog_documents,
                platform.source_documents,
                label="platform source snapshot",
            )
    exact_paths = {
        project_contract,
        *(path for _, path in context.catalog_paths),
        *(snapshot.path for snapshot in design_catalogs),
        *(snapshot.path for snapshot in layout_catalogs),
    }
    repository_owner_roots = {owner.root for owner in context.owners}
    scan_roots = set(repository_owner_roots)
    scan_roots.update(
        path.parent
        for _, path in context.catalog_paths
    )

    resolved_owner_roots: dict[Path, str] = {}
    for owner, directory in owner_roots.items():
        owner_name = _text(owner, "configuration owner")
        resolved = directory.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(
                f"configuration owner root escapes the project root: {directory}"
            )
        previous = resolved_owner_roots.get(resolved)
        if previous is not None and previous != owner_name:
            raise ValueError(
                f"configuration owner root has multiple owners: {resolved}"
            )
        resolved_owner_roots[resolved] = owner_name
    missing_components = repository_owner_roots - set(resolved_owner_roots)
    if missing_components:
        raise ValueError(
            "component roots lack a cataloged owner: "
            f"{sorted(str(path) for path in missing_components)}"
        )

    for path in (*exact_paths, *scan_roots, *resolved_owner_roots):
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(f"configuration source escapes the project root: {path}")
    for path in exact_paths:
        if not path.is_file():
            raise ValueError(f"configuration source is missing: {path}")
    for directory in scan_roots:
        if not directory.is_dir():
            raise ValueError(f"configuration owner root is missing: {directory}")

    documents = set(exact_paths)
    for directory in scan_roots:
        documents.update(path for path in directory.rglob("*.toml") if path.is_file())

    contract_kinds: set[str] = set()
    owners: set[str] = set()
    native_documents = 0
    envelope_fields = frozenset({"contract_kind", "path_scope", "owner"})
    repository_sources = {project_contract, *(path for _, path in context.catalog_paths)}
    platform_root = context.catalog("platform").parent
    operation_documents = dict(catalog_documents)
    for path in sorted(documents):
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(f"configuration source escapes the project root: {path}")
        raw = operation_documents.get(resolved)
        if raw is None:
            raw = read_toml(resolved)
            operation_documents[resolved] = raw
        present = envelope_fields & raw.keys()
        if not present:
            native_documents += 1
            continue
        if present != envelope_fields:
            missing = sorted(envelope_fields - present)
            raise ValueError(f"{resolved}: incomplete configuration header: {missing}")
        kind = _text(raw.get("contract_kind"), f"{resolved}: contract_kind")
        if resolved in repository_sources:
            allowed_scopes: str | tuple[str, ...] = "repository"
            expected_owner = repository_owner
        else:
            matches = [
                (owner_root, owner)
                for owner_root, owner in resolved_owner_roots.items()
                if resolved.is_relative_to(owner_root)
            ]
            if not matches:
                raise ValueError(f"{resolved}: configuration has no cataloged owner")
            owner_root, expected_owner = max(
                matches,
                key=lambda item: len(item[0].parts),
            )
            if owner_root in repository_owner_roots:
                allowed_scopes = ("owner", "cell", "verification", "variant")
            elif owner_root.is_relative_to(platform_root):
                allowed_scopes = "platform"
            else:
                raise ValueError(
                    f"{owner_root}: configuration owner root has no domain catalog"
                )
        header = require_config_header(
            raw,
            resolved,
            contract_kind=kind,
            path_scope=allowed_scopes,
            owner=expected_owner,
        )
        if header.contract_kind == "verification-cell":
            from sigilicon.domain.verification_cell import parse_verification_cell

            spec = parse_verification_cell(
                resolved,
                raw,
                project=context,
                contract_documents=operation_documents,
            )
            for source_path, document in spec.source_documents.items():
                operation_documents.setdefault(source_path, document)
        contract_kinds.add(header.contract_kind)
        owners.add(header.owner)

    return {
        "passed": True,
        "roots": sorted(
            path.relative_to(root).as_posix() for path in scan_roots
        ),
        "documents": len(documents),
        "contracts": len(documents) - native_documents,
        "native_documents": native_documents,
        "contract_kinds": sorted(contract_kinds),
        "owners": sorted(owners),
    }
