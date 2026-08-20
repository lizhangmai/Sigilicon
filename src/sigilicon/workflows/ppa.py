"""Reusable artifact lifecycle helpers for design-owned PPA workflows."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Mapping

from sigilicon.artifacts import (
    ArtifactRecord,
    atomic_write_json,
    file_sha256,
    load_manifest,
    new_identity,
    read_json_object,
)
from sigilicon.domain.provenance import digest
from sigilicon.external_tools import run_process_group
from sigilicon.paths import ArtifactExecutionPaths, ProjectContext


@dataclass(frozen=True)
class PpaStageResult:
    """Resolved output of one managed PPA stage."""

    stage: str
    model: str
    run_id: str
    run_dir: Path
    manifest_path: Path
    result_path: Path
    run_fingerprint: str
    reused: bool


def ppa_fingerprints(
    *,
    stage: str,
    source_commit: str,
    sources: Mapping[str, Path],
    setup: Mapping[str, Any],
) -> tuple[str, str, str]:
    """Build source, setup and run fingerprints for one PPA stage."""

    source_rows = {
        name: {"path": str(path), "sha256": file_sha256(path)}
        for name, path in sorted(sources.items())
    }
    source = digest(
        {"stage": stage, "commit": source_commit, "sources": source_rows}
    )
    setup_fingerprint = digest({"stage": stage, "setup": dict(setup)})
    run = digest(
        {
            "stage": stage,
            "source_fingerprint": source,
            "setup_fingerprint": setup_fingerprint,
        }
    )
    return source, setup_fingerprint, run


def result_from_current(
    paths: ArtifactExecutionPaths,
    *,
    stage: str,
    model: str,
    run_fingerprint: str,
) -> PpaStageResult | None:
    """Resolve a matching successful current pointer, if one exists."""

    current_path = paths.namespace_root / "current.json"
    if not current_path.is_file():
        return None
    current = read_json_object(current_path, "PPA current pointer")
    if (
        current.get("artifact_kind") != "analysis"
        or current.get("status") != "succeeded"
        or current.get("run_fingerprint") != run_fingerprint
    ):
        return None
    artifact_root = paths.artifact_root.resolve()
    manifest_path = artifact_root / str(current.get("manifest"))
    result_path = artifact_root / str(current.get("result"))
    manifest = load_manifest(manifest_path)
    if (
        manifest.get("status") != "succeeded"
        or manifest.get("run_id") != current.get("run_id")
        or manifest.get("fingerprints", {}).get("run") != run_fingerprint
        or not result_path.is_file()
        or file_sha256(result_path) != current.get("result_sha256")
    ):
        raise RuntimeError(f"PPA current pointer is inconsistent: {current_path}")
    return PpaStageResult(
        stage=stage,
        model=model,
        run_id=str(current["run_id"]),
        run_dir=manifest_path.parent,
        manifest_path=manifest_path,
        result_path=result_path,
        run_fingerprint=run_fingerprint,
        reused=True,
    )


def publish_current(
    record: ArtifactRecord, result_path: Path, run_fingerprint: str
) -> None:
    """Atomically publish a successful artifact pointer."""

    root = record.paths.artifact_root.resolve()
    atomic_write_json(
        record.paths.namespace_root / "current.json",
        {
            "status": "succeeded",
            "artifact_kind": record.paths.artifact_kind,
            "run_id": record.paths.identity,
            "run_fingerprint": run_fingerprint,
            "manifest": record.paths.manifest.relative_to(root).as_posix(),
            "result": result_path.relative_to(root).as_posix(),
            "result_sha256": file_sha256(result_path),
        },
    )


def begin_ppa_record(
    *,
    project_root: Path,
    library: str,
    cell: str,
    model: str,
    operation: str,
    backend: str,
    source_fingerprint: str,
    setup_fingerprint: str,
    run_fingerprint: str,
) -> ArtifactRecord:
    """Begin a design-owned PPA artifact using the shared lifecycle."""

    paths = ProjectContext.from_project_root(project_root).artifacts.analysis_run(
        library, cell, "ppa", model, new_identity()
    )
    record = ArtifactRecord.begin(
        paths,
        entities={
            "library": library,
            "cell": cell,
            "analysis": "ppa",
            "model": model,
        },
        operation=operation,
        backend=backend,
        source_fingerprint=source_fingerprint,
        setup_fingerprint=setup_fingerprint,
        run_fingerprint=run_fingerprint,
    )
    record.bind_operation(new_identity())
    return record


def stage_result(
    *, stage: str, model: str, record: ArtifactRecord, result: Path, run: str
) -> PpaStageResult:
    """Create the stable return value for a newly completed stage."""

    return PpaStageResult(
        stage=stage,
        model=model,
        run_id=record.paths.identity,
        run_dir=record.paths.root,
        manifest_path=record.paths.manifest,
        result_path=result,
        run_fingerprint=run,
        reused=False,
    )


def load_current_stage(
    *,
    project_root: Path,
    library: str,
    cell: str,
    stage: str,
    model: str,
) -> tuple[PpaStageResult, dict[str, Any]]:
    """Load and validate the currently published result for a required stage."""

    template = ProjectContext.from_project_root(project_root).artifacts.analysis_run(
        library, cell, "ppa", model, "0" * 32
    )
    current_path = template.namespace_root / "current.json"
    if not current_path.is_file():
        raise RuntimeError(f"required PPA stage has no current pointer: {stage}")
    current = read_json_object(current_path, f"{stage} PPA current pointer")
    result = result_from_current(
        template,
        stage=stage,
        model=model,
        run_fingerprint=str(current.get("run_fingerprint")),
    )
    if result is None:
        raise RuntimeError(f"required PPA stage current pointer is invalid: {stage}")
    return result, read_json_object(result.result_path, f"{stage} PPA result")
