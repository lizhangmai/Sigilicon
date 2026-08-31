"""Snapshot exact planner-selected source closures across project/package roots."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path

from sigilicon.domain.repository import Project
from sigilicon.flow.model import SourceMember
from sigilicon.flow.source_assets import snapshot_source_member


def project_source_members(
    project: Project,
    paths: Iterable[Path],
    *,
    label: str,
    records: Mapping[Path, str] | None = None,
    external_roots: tuple[tuple[str, Path], ...] = (),
) -> tuple[SourceMember, ...]:
    """Snapshot planner-selected source bytes within explicitly declared roots."""

    normalized_records: dict[Path, str] | None = None
    if records is not None:
        normalized_records = {}
        for raw_path, record in records.items():
            if not isinstance(raw_path, (str, Path)):
                raise ValueError(f"{label} source record path must be a filesystem path")
            if not isinstance(record, str):
                raise ValueError(f"{label} source record must be UTF-8 text")
            path = Path(raw_path).resolve()
            previous = normalized_records.get(path)
            if previous is not None and previous != record:
                raise ValueError(f"{label} source record has duplicate path drift: {path}")
            normalized_records[path] = record

    resolved_paths = {Path(path).resolve() for path in paths}
    if normalized_records is not None and set(normalized_records) != resolved_paths:
        raise ValueError(f"{label} source paths and retained records disagree")

    project_root = project.project_root.resolve()
    package_root = Path(__file__).resolve().parents[2]
    selected: set[tuple[str, Path, Path]] = set()
    for resolved in resolved_paths:
        if not resolved.is_file():
            raise ValueError(f"{label} source is not a file: {resolved}")
        if resolved.is_relative_to(project_root):
            selected.add(("project", project_root, resolved))
        elif resolved.is_relative_to(package_root):
            selected.add(("sigilicon-package", package_root, resolved))
        else:
            matches = tuple(
                (scope, root.resolve())
                for scope, root in external_roots
                if resolved.is_relative_to(root.resolve())
            )
            if len(matches) != 1:
                raise ValueError(
                    f"{label} source is outside its declared roots: {resolved}"
                )
            scope, root = matches[0]
            selected.add((scope, root, resolved))
    return tuple(
        snapshot_source_member(
            path,
            source_root=root,
            scope=scope,
            record_text=(
                None if normalized_records is None else normalized_records[path]
            ),
            source_label=label,
        )
        for scope, root, path in sorted(
            selected,
            key=lambda item: (item[0], item[2].as_posix()),
        )
    )


__all__ = ["project_source_members"]
