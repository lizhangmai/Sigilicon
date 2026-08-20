"""Shared metadata and inventory checks for repository-owned TOML contracts.

The repository intentionally keeps several domain-specific TOML schemas.  This
module does not flatten those schemas; it validates the small common envelope
that makes ownership, scope, and migration boundaries explicit.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib
from typing import Any, Mapping


CONFIG_SCHEMA = 1
PATH_SCOPES = frozenset({"repository", "owner", "cell", "verification", "platform", "product", "variant"})


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


def _contract_kind_choices(value: object, field: str) -> str | tuple[str, ...]:
    """Accept one contract kind or an explicit set of equivalent kinds."""

    if isinstance(value, str):
        return _text(value, field)
    if not isinstance(value, list) or not value or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{field} must be a non-empty string or string array")
    choices = tuple(value)
    if len(set(choices)) != len(choices):
        raise ValueError(f"{field} contains duplicate contract kinds")
    return choices


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


def _safe_relative(root: Path, value: object, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty relative path")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{field} must stay below the project root")
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"{field} must stay below the project root")
    return resolved


def _validate_inventory_document(
    path: Path,
    *,
    project_root: Path,
    contract_kind: str | tuple[str, ...],
    path_scope: str,
    owner: str,
    schema: int,
) -> None:
    raw = read_toml(path)
    require_config_header(
        raw,
        path,
        contract_kind=contract_kind,
        path_scope=path_scope,
        owner=owner,
        schema=schema,
    )
    if raw.get("contract_kind") == "verification-cell":
        from sigilicon.domain.verification_cell import load_verification_cell

        load_verification_cell(path, project_root=project_root)


def validate_configuration_inventory(
    path: Path,
    *,
    project_root: Path,
) -> dict[str, Any]:
    """Validate the tracked active configuration families declared by an inventory."""

    root = project_root.resolve()
    inventory_path = path.resolve()
    if not inventory_path.is_relative_to(root) or not inventory_path.is_file():
        raise ValueError("configuration inventory must be a project-owned file")
    raw = read_toml(inventory_path)
    require_config_header(
        raw,
        inventory_path,
        contract_kind="configuration-inventory",
        path_scope="repository",
        owner="repository",
    )

    checked: set[Path] = set()
    files = raw.get("files", [])
    if not isinstance(files, list) or not all(isinstance(item, Mapping) for item in files):
        raise ValueError("configuration inventory files must be an array of tables")
    for index, item in enumerate(files):
        field = f"files[{index}]"
        target = _safe_relative(root, item.get("path"), f"{field}.path")
        if not target.is_file():
            raise ValueError(f"{field}.path does not exist: {target}")
        if target in checked:
            raise ValueError(f"configuration inventory selects a file more than once: {target}")
        checked.add(target)
        _validate_inventory_document(
            target,
            project_root=root,
            contract_kind=_contract_kind_choices(item.get("contract_kind"), f"{field}.contract_kind"),
            path_scope=_text(item.get("path_scope"), f"{field}.path_scope"),
            owner=_text(item.get("owner"), f"{field}.owner"),
            schema=item.get("schema", CONFIG_SCHEMA),
        )

    families = raw.get("families", [])
    if not isinstance(families, list) or not all(
        isinstance(item, Mapping) for item in families
    ):
        raise ValueError("configuration inventory families must be an array of tables")
    family_counts: dict[str, int] = {}
    for index, item in enumerate(families):
        field = f"families[{index}]"
        pattern = _text(item.get("glob"), f"{field}.glob")
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise ValueError(f"{field}.glob must stay below the project root")
        matches = tuple(sorted(root.glob(pattern)))
        excludes = item.get("exclude", [])
        if not isinstance(excludes, list) or any(not isinstance(value, str) for value in excludes):
            raise ValueError(f"{field}.exclude must be a string array")
        excluded = {(root / value).resolve() for value in excludes}
        matches = tuple(item for item in matches if item.is_file() and item not in excluded)
        if not matches:
            raise ValueError(f"{field}.glob matched no files: {pattern}")
        for target in matches:
            if target in checked:
                raise ValueError(f"configuration inventory selects a file more than once: {target}")
            checked.add(target)
            _validate_inventory_document(
                target,
                project_root=root,
                contract_kind=_contract_kind_choices(item.get("contract_kind"), f"{field}.contract_kind"),
                path_scope=_text(item.get("path_scope"), f"{field}.path_scope"),
                owner=_text(item.get("owner"), f"{field}.owner"),
                schema=item.get("schema", CONFIG_SCHEMA),
            )
        family_counts[field] = len(matches)

    native_families = raw.get("native_families", [])
    if not isinstance(native_families, list) or not all(
        isinstance(item, Mapping) for item in native_families
    ):
        raise ValueError("configuration inventory native_families must be an array of tables")
    native_counts: dict[str, int] = {}
    for index, item in enumerate(native_families):
        field = f"native_families[{index}]"
        pattern = _text(item.get("glob"), f"{field}.glob")
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise ValueError(f"{field}.glob must stay below the project root")
        matches = tuple(
            sorted(path for path in root.glob(pattern) if path.is_file())
        )
        if not matches:
            raise ValueError(f"{field}.glob matched no files: {pattern}")
        expected_schema = item.get("schema")
        if isinstance(expected_schema, bool) or not isinstance(expected_schema, int):
            raise ValueError(f"{field}.schema must be an integer")
        for target in matches:
            if target in checked:
                raise ValueError(f"configuration inventory selects a file more than once: {target}")
            checked.add(target)
            document = read_toml(target)
            if document.get("schema") != expected_schema:
                raise ValueError(
                    f"{target}: native schema must be {expected_schema}"
                )
        native_counts[field] = len(matches)

    excluded_files = raw.get("excluded_files", [])
    if not isinstance(excluded_files, list) or not all(
        isinstance(item, Mapping) for item in excluded_files
    ):
        raise ValueError("configuration inventory excluded_files must be an array of tables")
    excluded: set[Path] = set()
    for index, item in enumerate(excluded_files):
        field = f"excluded_files[{index}]"
        target = _safe_relative(root, item.get("path"), f"{field}.path")
        if not target.is_file():
            raise ValueError(f"{field}.path does not exist: {target}")
        if target in checked or target in excluded:
            raise ValueError(f"configuration inventory selects a file more than once: {target}")
        excluded.add(target)
        _text(item.get("reason"), f"{field}.reason")

    excluded_families = raw.get("excluded_families", [])
    if not isinstance(excluded_families, list) or not all(
        isinstance(item, Mapping) for item in excluded_families
    ):
        raise ValueError("configuration inventory excluded_families must be an array of tables")
    excluded_family_counts: dict[str, int] = {}
    for index, item in enumerate(excluded_families):
        field = f"excluded_families[{index}]"
        pattern = _text(item.get("glob"), f"{field}.glob")
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise ValueError(f"{field}.glob must stay below the project root")
        matches = tuple(sorted(root.glob(pattern)))
        excludes = item.get("exclude", [])
        if not isinstance(excludes, list) or any(not isinstance(value, str) for value in excludes):
            raise ValueError(f"{field}.exclude must be a string array")
        excluded_paths = {(root / value).resolve() for value in excludes}
        matches = tuple(item for item in matches if item.is_file() and item not in excluded_paths)
        if not matches:
            raise ValueError(f"{field}.glob matched no files: {pattern}")
        _text(item.get("reason"), f"{field}.reason")
        for target in matches:
            if target in checked or target in excluded:
                raise ValueError(f"configuration inventory selects a file more than once: {target}")
            excluded.add(target)
        excluded_family_counts[field] = len(matches)

    return {
        "passed": True,
        "inventory": inventory_path.relative_to(root).as_posix(),
        "files": len(files),
        "families": family_counts,
        "native_families": native_counts,
        "excluded_files": len(excluded_files),
        "excluded_families": excluded_family_counts,
    }
