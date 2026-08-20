"""Project-wide design ownership and lifecycle audit."""

from __future__ import annotations

from pathlib import Path

from sigilicon.domain.design_catalog import DesignCatalog, load_design_catalog
from sigilicon.workflows.design_lifecycle import inspect_design


def inspect_design_catalog(
    catalog_path: Path,
    *,
    project_root: Path | None = None,
) -> tuple[DesignCatalog, dict[str, object]]:
    catalog = load_design_catalog(catalog_path, project_root=project_root)
    actual_directories = {
        path.resolve()
        for path in catalog.design_root.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    }
    cataloged_directories = {entry.directory.resolve() for entry in catalog.entries}
    missing = sorted(path.name for path in actual_directories - cataloged_directories)
    stale = sorted(path.name for path in cataloged_directories - actual_directories)
    if missing or stale:
        raise ValueError(
            f"design catalog coverage mismatch: unregistered={missing}, stale={stale}"
        )
    reports: list[dict[str, object]] = []
    for entry in catalog.entries:
        inspections = [
            inspect_design(spec, project_root=catalog.project_root)
            for spec in entry.design_specs
        ]
        reports.append(
            {
                "name": entry.name,
                "directory": str(entry.directory.relative_to(catalog.project_root)),
                "kind": entry.kind,
                "entrypoint": entry.entrypoint,
                "oa_policy": entry.oa_policy,
                "verification_policy": entry.verification_policy,
                "verification_owner": str(
                    entry.verification_owner.relative_to(catalog.project_root)
                ),
                "sources": [
                    str(path.relative_to(catalog.project_root))
                    for path in entry.source_files
                ],
                "designs": [inspection.as_dict() for inspection in inspections],
            }
        )
    return catalog, {
        "passed": True,
        "catalog": str(catalog.path.relative_to(catalog.project_root)),
        "design_directory_count": len(catalog.entries),
        "entries": reports,
    }
