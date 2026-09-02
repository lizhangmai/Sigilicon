"""Shared metadata checks for repository-owned TOML contracts.

The repository intentionally keeps several domain-specific TOML schemas.  This
module does not flatten those schemas; it validates the small common envelope
that makes ownership, scope, and migration boundaries explicit.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import tomllib
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping

from sigilicon.artifacts import read_nofollow_text
from sigilicon.contracts import (
    contract_schema as _contract_schema,
    freeze_toml_document as _freeze_toml_document,
    is_frozen_toml_document as _is_frozen_toml_document,
    require_config_header as _require_config_header,
)

if TYPE_CHECKING:
    from sigilicon.project import Project


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


_REPOSITORY_SOURCE_INVENTORY_AUTHORITY = object()


def _repository_configuration_roots(project: Project) -> frozenset[Path]:
    """Return the catalog-selected roots scanned by repository checks."""

    return frozenset(
        {
            *(owner.root for owner in project.owners),
            *(
                path.parent
                for role, path in project.catalog_paths
                if role != "ip"
            ),
        }
    )


def _repository_configuration_paths(directory: Path) -> tuple[Path, ...]:
    """Enumerate regular TOML sources while rejecting symlinked subtrees."""

    if directory != directory.resolve() or not directory.is_dir():
        raise ValueError(
            f"configuration owner root is missing or unsafe: {directory}"
        )
    pending = [directory]
    result: list[Path] = []
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as scanned:
                entries = sorted(scanned, key=lambda entry: entry.name)
        except OSError as exc:
            raise ValueError(
                f"cannot inspect configuration owner root {current}: {exc}"
            ) from exc
        for entry in entries:
            path = Path(entry.path)
            try:
                if entry.is_symlink():
                    if path.suffix == ".toml" or entry.is_dir():
                        raise ValueError(
                            f"configuration source path is a symlink: {path}"
                        )
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                elif entry.is_file(follow_symlinks=False):
                    if path.suffix == ".toml":
                        result.append(path)
                elif path.suffix == ".toml":
                    raise ValueError(
                        f"configuration source must be a regular file: {path}"
                    )
            except OSError as exc:
                raise ValueError(
                    f"cannot inspect configuration source {path}: {exc}"
                ) from exc
    return tuple(result)


def _read_frozen_toml(path: Path) -> Mapping[str, Any]:
    """Capture one exact regular TOML source without following links."""

    try:
        value = tomllib.loads(read_nofollow_text(path))
    except (OSError, RuntimeError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read TOML {path}: {exc}") from exc
    return _freeze_toml_document(value)


class RepositorySourceInventory:
    """Immutable source closure trusted by one repository scan operation.

    Construction captures every TOML below the catalog-selected scan roots.
    Domain loaders may then verify their richer snapshots against this closed
    inventory, but they cannot add sources discovered later in the operation.
    """

    __slots__ = ("_documents", "_project")

    def __init__(
        self,
        *,
        _authority: object,
        project: Project,
        documents: Mapping[Path, Mapping[str, Any]],
    ) -> None:
        if _authority is not _REPOSITORY_SOURCE_INVENTORY_AUTHORITY:
            raise ValueError("repository source inventory must use its factory")
        object.__setattr__(self, "_project", project)
        object.__setattr__(self, "_documents", MappingProxyType(dict(documents)))

    @classmethod
    def for_project(
        cls,
        project: Project,
    ) -> RepositorySourceInventory:
        """Capture the complete configuration inventory for one operation."""

        expected_documents: dict[Path, Mapping[str, Any]] = {}

        def seed(label: str, path: Path, document: Mapping[str, Any]) -> None:
            resolved = path.resolve()
            if (
                path != resolved
                or not resolved.is_relative_to(project.project_root)
                or not _is_frozen_toml_document(document)
            ):
                raise ValueError(f"{label} source identity drift: {path}")
            previous = expected_documents.get(resolved)
            if previous is not None and previous != document:
                raise ValueError(f"{label} disagrees with another source: {path}")
            expected_documents[resolved] = document

        manifest_document = project.manifest_source_document()
        if manifest_document:
            seed(
                "project manifest snapshot",
                project.manifest_path,
                manifest_document,
            )
        for owner in project.owners:
            seed(
                "owner component snapshot",
                owner.component.path,
                owner.component.document,
            )
        ip_catalog = project.ip_catalog_snapshot()
        seed(
            "IP catalog snapshot",
            ip_catalog.path,
            ip_catalog.document,
        )
        paths = {
            project.manifest_path,
            *(path for _, path in project.catalog_paths),
        }
        for directory in _repository_configuration_roots(project):
            paths.update(_repository_configuration_paths(directory))
        documents: dict[Path, Mapping[str, Any]] = {}
        for path in sorted(paths):
            resolved = path.resolve()
            if path != resolved or not resolved.is_relative_to(project.project_root):
                raise ValueError(f"configuration source is unsafe: {path}")
            documents[resolved] = _read_frozen_toml(resolved)
        inventory = cls(
            _authority=_REPOSITORY_SOURCE_INVENTORY_AUTHORITY,
            project=project,
            documents=documents,
        )
        inventory.verify("project source snapshot", expected_documents)
        return inventory

    @property
    def project(self) -> Project:
        return self._project

    @property
    def documents(self) -> Mapping[Path, Mapping[str, Any]]:
        return self._documents

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("repository source inventory is immutable")

    def require_project(self, project: Project) -> None:
        """Require the exact operation that captured this inventory."""

        if project is not self.project:
            raise ValueError("repository source inventory belongs to another operation")

    def verify(
        self,
        label: str,
        source_documents: Mapping[Path, Mapping[str, Any]],
    ) -> None:
        """Require domain snapshots to match the captured source inventory."""

        if not isinstance(label, str) or not label:
            raise ValueError("repository source inventory label must be non-empty")
        if not isinstance(source_documents, Mapping):
            raise TypeError("repository source documents must be a mapping")
        for path, document in source_documents.items():
            if (
                not isinstance(path, Path)
                or path != path.resolve()
                or not path.is_relative_to(self.project.project_root)
                or path.suffix != ".toml"
            ):
                raise ValueError(f"{label} source path identity drift: {path}")
            if not _is_frozen_toml_document(document):
                raise ValueError(f"{label} source document must be frozen: {path}")
            previous = self.documents.get(path)
            if previous is None:
                raise ValueError(
                    f"{label} source is outside the captured inventory: {path}"
                )
            if previous != document:
                raise ValueError(f"{label} disagrees with another source: {path}")

    def resolve(self, path: Path) -> Mapping[str, Any]:
        """Return one captured document or reject an inventory miss."""

        if not isinstance(path, Path) or path != path.resolve():
            raise ValueError("repository source inventory lookup must be resolved")
        try:
            return self.documents[path]
        except KeyError as exc:
            raise ValueError(
                f"configuration source is outside the captured inventory: {path}"
            ) from exc


def inspect_project_configuration_sources(
    context: Project,
    *,
    operation_catalog_inventory: Mapping[str, Path],
    sources: RepositorySourceInventory,
) -> dict[str, Any]:
    """Inspect configuration envelopes from one closed source inventory."""

    sources.require_project(context)
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
            schema=2,
        )
    sources.verify(
        "owner operation catalog snapshot",
        {path: sources.resolve(path) for path in selected_catalogs.values()},
    )
    root = context.project_root.resolve()
    project_contract = context.manifest_path
    repository_owner = context.manifest_owner
    operation_catalog_paths = set(selected_catalogs.values())
    exact_paths = {
        project_contract,
        *(path for _, path in context.catalog_paths),
        *operation_catalog_paths,
    }
    repository_owner_roots = {owner.root for owner in context.owners}
    scan_roots = _repository_configuration_roots(context)

    resolved_owner_roots = {
        owner.root: owner.name for owner in context.owners
    }
    platform_catalog_path = context.catalog("platform")
    platform_catalog_document = sources.resolve(platform_catalog_path)
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
        if not manifest.is_relative_to(root):
            raise ValueError(f"platforms.{key} manifest is missing or unsafe")
        document = sources.resolve(manifest)
        owner = _text(document.get("owner"), f"{manifest}: owner")
        platform_owner_root = manifest.parent
        previous = resolved_owner_roots.get(platform_owner_root)
        if previous is not None and previous != owner:
            raise ValueError(
                "platform and component catalogs disagree on an owner root"
            )
        resolved_owner_roots[platform_owner_root] = owner

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
    repository_sources = {project_contract, *(path for _, path in context.catalog_paths)}
    platform_root = context.catalog("platform").parent
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
