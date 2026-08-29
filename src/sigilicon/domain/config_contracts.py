"""Shared metadata checks for repository-owned TOML contracts.

The repository intentionally keeps several domain-specific TOML schemas.  This
module does not flatten those schemas; it validates the small common envelope
that makes ownership, scope, and migration boundaries explicit.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from sigilicon.domain.repository import Project


CONFIG_SCHEMA = 1
PATH_SCOPES = frozenset(
    {"repository", "owner", "cell", "verification", "platform", "variant"}
)


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


def inspect_project_configurations(
    context: Project,
    *,
    owner_roots: Mapping[str, Path],
) -> dict[str, Any]:
    """Validate TOML below the roots selected by repository catalogs.

    Catalog locations define repository, IP-owner, and platform roots.
    Domain loaders remain responsible for native schemas; this pass proves that
    every TOML is parseable and that every common metadata envelope is complete.
    """

    root = context.project_root.resolve()
    project_contract = root / "sigilicon.toml"
    repository_owner = context.manifest_owner
    exact_paths = {
        project_contract,
        *(path for _, path in context.catalog_paths),
        *(path for _, path in context.flow_catalogs("design_targets")),
        *(path for _, path in context.flow_catalogs("layout_targets")),
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
    for path in sorted(documents):
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(f"configuration source escapes the project root: {path}")
        raw = read_toml(resolved)
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
            from sigilicon.domain.verification_cell import load_verification_cell

            load_verification_cell(resolved, project_root=root)
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
