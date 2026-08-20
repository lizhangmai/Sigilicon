"""Pure project-design catalog contract.

The catalog is intentionally small: it declares ownership and lifecycle
entrypoints, while the individual design specifications remain the only
sources of circuit interface truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib


_KINDS = {"analog", "mixed-signal", "systemverilog"}
_OA_POLICIES = {"recursive-schematic", "systemverilog-text"}
_VERIFICATION_POLICIES = {"local", "integration-owned"}


@dataclass(frozen=True)
class DesignCatalogEntry:
    name: str
    directory: Path
    kind: str
    entrypoint: str
    design_specs: tuple[Path, ...]
    source_files: tuple[Path, ...]
    oa_policy: str
    verification_policy: str
    verification_owner: Path


@dataclass(frozen=True)
class DesignCatalog:
    path: Path
    project_root: Path
    design_root: Path
    entries: tuple[DesignCatalogEntry, ...]


def _relative_file(root: Path, value: object, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty relative path")
    relative = Path(value)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError(f"{field} must stay below the project root")
    result = root.joinpath(*relative.parts)
    if not result.is_file():
        raise ValueError(f"{field} does not exist: {result}")
    return result


def _strings(value: object, field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (not value and not allow_empty):
        qualifier = "" if not allow_empty else " (possibly empty)"
        raise ValueError(f"{field} must be a{qualifier} string array")
    result = tuple(value)
    if any(not isinstance(item, str) or not item for item in result):
        raise ValueError(f"{field} must contain non-empty strings")
    if len(set(result)) != len(result):
        raise ValueError(f"{field} contains duplicates")
    return result


def load_design_catalog(path: Path, *, project_root: Path | None = None) -> DesignCatalog:
    catalog_path = path.resolve()
    root = (project_root or catalog_path.parent.parent).resolve()
    design_root = catalog_path.parent
    if not design_root.is_relative_to(root):
        raise ValueError("design catalog must stay below the project root")
    try:
        with catalog_path.open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"cannot read design catalog {catalog_path}: {error}") from error
    rows = raw.get("entries")
    if not isinstance(rows, list) or not rows:
        raise ValueError("design catalog entries must be a non-empty table array")
    entries: list[DesignCatalogEntry] = []
    for index, row in enumerate(rows):
        field = f"entries[{index}]"
        if not isinstance(row, dict):
            raise ValueError(f"{field} must be a table")
        name = row.get("name")
        directory_name = row.get("directory")
        kind = row.get("kind")
        entrypoint = row.get("entrypoint")
        oa_policy = row.get("oa_policy")
        verification_policy = row.get("verification_policy")
        if not isinstance(name, str) or not name:
            raise ValueError(f"{field}.name must be a non-empty string")
        if not isinstance(directory_name, str) or Path(directory_name).name != directory_name:
            raise ValueError(f"{field}.directory must name one immediate catalog directory")
        if kind not in _KINDS:
            raise ValueError(f"{field}.kind must be one of {sorted(_KINDS)}")
        if not isinstance(entrypoint, str) or not entrypoint:
            raise ValueError(f"{field}.entrypoint must be a non-empty command owner")
        if oa_policy not in _OA_POLICIES:
            raise ValueError(f"{field}.oa_policy must be one of {sorted(_OA_POLICIES)}")
        if verification_policy not in _VERIFICATION_POLICIES:
            raise ValueError(
                f"{field}.verification_policy must be one of "
                f"{sorted(_VERIFICATION_POLICIES)}"
            )
        directory = design_root / directory_name
        if not directory.is_dir():
            raise ValueError(f"cataloged design directory does not exist: {directory}")
        spec_names = _strings(
            row.get("design_specs", []), f"{field}.design_specs", allow_empty=True
        )
        source_names = _strings(row.get("source_files"), f"{field}.source_files")
        if oa_policy == "recursive-schematic" and not spec_names:
            raise ValueError(f"{field} recursive schematic policy requires design_specs")
        if oa_policy == "systemverilog-text" and spec_names:
            raise ValueError(f"{field} SystemVerilog policy cannot declare design_specs")
        specs = tuple(
            _relative_file(directory, item, f"{field}.design_specs")
            for item in spec_names
        )
        sources = tuple(
            _relative_file(directory, item, f"{field}.source_files")
            for item in source_names
        )
        verification_owner = _relative_file(
            root, row.get("verification_owner"), f"{field}.verification_owner"
        )
        entries.append(
            DesignCatalogEntry(
                name=name,
                directory=directory,
                kind=kind,
                entrypoint=entrypoint,
                design_specs=specs,
                source_files=sources,
                oa_policy=oa_policy,
                verification_policy=verification_policy,
                verification_owner=verification_owner,
            )
        )
    names = [entry.name for entry in entries]
    directories = [entry.directory for entry in entries]
    if len(set(names)) != len(names) or len(set(directories)) != len(directories):
        raise ValueError("design catalog names and directories must be unique")
    return DesignCatalog(catalog_path, root, design_root, tuple(entries))
