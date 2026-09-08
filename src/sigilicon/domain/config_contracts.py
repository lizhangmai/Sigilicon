"""Shared metadata checks for repository-owned TOML contracts.

The repository intentionally keeps several domain-specific TOML schemas.  This
module does not flatten those schemas; it validates the small common envelope
that makes ownership, scope, and migration boundaries explicit.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Mapping

from sigilicon.contracts import (
    DocumentStore,
    contract_schema as _contract_schema,
    require_config_header as _require_config_header,
)

if TYPE_CHECKING:
    from sigilicon.project import Project


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def inspect_project_configuration_sources(
    context: Project,
    *,
    operation_catalog_inventory: Mapping[str, Path],
    sources: DocumentStore,
) -> dict[str, Any]:
    """Inspect configuration envelopes from one closed source inventory."""

    if sources.root != context.project_root:
        raise ValueError("document store belongs to another Project")
    expected_owner_names = {
        owner.name
        for owner in context.owners
        if owner.component.operation_catalog is not None
    }
    selected_catalogs = dict(operation_catalog_inventory)
    if set(selected_catalogs) != expected_owner_names:
        missing = sorted(expected_owner_names - set(selected_catalogs))
        extra = sorted(set(selected_catalogs) - expected_owner_names)
        raise ValueError(
            "owner operation catalog inventory does not match component selections: "
            f"missing={missing}, extra={extra}"
        )
    for owner_name, path in selected_catalogs.items():
        owner = context.owner(owner_name)
        configured = owner.component.operation_catalog
        if configured is None:
            raise ValueError(
                f"owner {owner.name!r} has no component operation_catalog selection"
            )
        configured_path = context.project_root.joinpath(*configured.parts)
        resolved = configured_path.resolve()
        if (
            configured_path != resolved
            or path != resolved
            or path != path.resolve()
            or not path.is_file()
            or not path.is_relative_to(owner.root)
        ):
            raise ValueError(
                f"owner operation catalog does not match owner {owner.name!r}"
            )
        document = sources.resolve(path)
        _require_config_header(
            document,
            path,
            contract_kind="owner-operations",
            path_scope="owner",
            owner=owner.name,
            schema=_contract_schema("owner-operations"),
        )
    sources.verify(
        "owner operation catalog snapshot",
        {path: sources.resolve(path) for path in selected_catalogs.values()},
    )
    root = context.project_root.resolve()
    project_contract = context.manifest_path
    repository_owner = context.manifest_owner
    operation_catalog_paths = set(selected_catalogs.values())
    platform_catalogs = {
        owner.name: context.project_root.joinpath(
            *owner.component.platform_catalog.parts
        ).resolve()
        for owner in context.owners
        if owner.component.platform_catalog is not None
    }
    platform_catalog_paths = set(platform_catalogs.values())
    exact_paths = {
        project_contract,
        *(path for _, path in context.catalog_paths),
        *operation_catalog_paths,
        *platform_catalog_paths,
    }
    scan_roots = set(context.configuration_roots)

    resolved_owner_roots = {owner.root: owner.name for owner in context.owners}
    platform_roots: dict[Path, str] = {}
    for owner_name, platform_catalog_path in platform_catalogs.items():
        owner = context.owner(owner_name)
        if not platform_catalog_path.is_relative_to(owner.root):
            raise ValueError("platform catalog must stay inside its owner root")
        platform_catalog_document = sources.resolve(platform_catalog_path)
        _require_config_header(
            platform_catalog_document,
            platform_catalog_path,
            contract_kind="platform-catalog",
            path_scope="owner",
            owner=owner_name,
            schema=_contract_schema("platform-catalog"),
        )
        platforms = platform_catalog_document.get("platforms")
        if not isinstance(platforms, Mapping):
            raise ValueError("platform catalog platforms must be a table")
        for key, value in platforms.items():
            if not isinstance(key, str) or not key:
                raise ValueError("platform catalog keys must be non-empty strings")
            if not isinstance(value, str) or not value:
                raise ValueError(f"platforms.{key} must be a non-empty relative path")
            relative = PurePosixPath(value)
            if (
                relative.is_absolute()
                or "\\" in value
                or relative.as_posix() != value
                or any(part in {"", ".", ".."} for part in relative.parts)
            ):
                raise ValueError(
                    f"platforms.{key} must be a canonical relative path"
                )
            manifest = platform_catalog_path.parent.joinpath(*relative.parts).resolve()
            if not manifest.is_relative_to(owner.root):
                raise ValueError(f"platforms.{key} manifest is missing or unsafe")
            document = sources.resolve(manifest)
            manifest_owner = _text(document.get("owner"), f"{manifest}: owner")
            if manifest_owner != owner_name:
                raise ValueError(
                    f"{manifest}: platform owner must be {owner_name!r}"
                )
            platform_owner_root = manifest.parent
            previous = platform_roots.get(platform_owner_root)
            if previous is not None and previous != owner_name:
                raise ValueError(
                    "platform catalogs disagree on a platform root"
                )
            platform_roots[platform_owner_root] = owner_name

    for path in (*exact_paths, *scan_roots, *resolved_owner_roots):
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(f"configuration source escapes the project root: {path}")
    documents = set(sources.documents)
    missing_exact = exact_paths - documents
    if missing_exact:
        raise ValueError(
            "configuration source inventory omitted selected contracts: "
            f"{sorted(missing_exact)}"
        )

    contract_kinds: set[str] = set()
    owners: set[str] = set()
    native_documents = 0
    envelope_fields = frozenset({"contract_kind", "path_scope", "owner"})
    repository_sources = {
        project_contract,
        *(path for _, path in context.catalog_paths),
    }
    for path in sorted(documents):
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(f"configuration source escapes the project root: {path}")
        raw = sources.resolve(resolved)
        present = envelope_fields & raw.keys()
        if not present:
            native_documents += 1
            continue
        if present != envelope_fields:
            missing = sorted(envelope_fields - present)
            raise ValueError(f"{resolved}: incomplete configuration header: {missing}")
        kind = _text(raw.get("contract_kind"), f"{resolved}: contract_kind")
        if kind == "owner-operations" and resolved not in operation_catalog_paths:
            raise ValueError(
                f"{resolved}: owner-operations must be selected by a component "
                "operation_catalog"
            )
        if kind == "platform-catalog" and resolved not in platform_catalog_paths:
            raise ValueError(
                f"{resolved}: platform-catalog must be selected by a component "
                "platform_catalog"
            )
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
            allowed_scopes = ("owner", "cell", "verification", "variant")
            if raw.get("path_scope") == "platform":
                matches = [
                    platform_owner
                    for platform_root, platform_owner in platform_roots.items()
                    if resolved.is_relative_to(platform_root)
                ]
                if not matches or any(
                    platform_owner != expected_owner
                    for platform_owner in matches
                ):
                    raise ValueError(
                        f"{resolved}: platform configuration is not selected "
                        "by its owner platform catalog"
                    )
                allowed_scopes = "platform"
        header = _require_config_header(
            raw,
            resolved,
            contract_kind=kind,
            path_scope=allowed_scopes,
            owner=expected_owner,
            schema=_contract_schema(kind),
        )
        if header.contract_kind == "verification-cell":
            from sigilicon.domain.verification_cell import parse_verification_cell

            spec = parse_verification_cell(
                resolved,
                raw,
                project=context,
                contract_documents=sources.documents,
            )
            for source_path, document in spec.source_documents.items():
                sources.verify(
                    "verification cell snapshot",
                    {source_path: document},
                )
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
