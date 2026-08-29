"""Project-wide design ownership and lifecycle audit."""

from __future__ import annotations

from pathlib import Path

from sigilicon.domain.design_catalog import DesignCatalog, load_design_catalog
from sigilicon.domain.repository import Project
from sigilicon.workflows.design_lifecycle import inspect_design


def inspect_design_catalog(
    catalog_path: Path,
    *,
    project: Project | None = None,
    project_root: Path | None = None,
) -> tuple[DesignCatalog, dict[str, object]]:
    if (
        project is not None
        and project_root is not None
        and project_root.resolve() != project.project_root
    ):
        raise ValueError("project_root disagrees with the explicit project")
    root = project.project_root if project is not None else project_root
    catalog = load_design_catalog(catalog_path, project_root=root)
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
            inspect_design(
                spec,
                project=project,
                project_root=None if project is not None else catalog.project_root,
            )
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
