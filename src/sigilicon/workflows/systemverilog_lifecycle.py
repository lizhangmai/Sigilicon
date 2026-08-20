"""Legacy artifact-backed lifecycle for SystemVerilog OA text views."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sigilicon.artifacts import ArtifactRecord, file_sha256, load_manifest, new_identity
from sigilicon.domain.provenance import digest
from sigilicon.paths import ProjectContext
from sigilicon.virtuoso.legacy_ade import capture_ade_component
from sigilicon.virtuoso.provenance import oa_view_digest
from sigilicon.virtuoso.systemverilog import import_systemverilog_view
from sigilicon.virtuoso.workspace import OperationPolicy, workspace_operation


@dataclass(frozen=True)
class SystemVerilogSyncResult:
    manifest_path: Path
    library: str
    cell: str
    source_sha256: str
    oa_sha256: str


def systemverilog_source_fingerprint(
    *, library: str, cell: str, source: Path
) -> str:
    return digest(
        {
            "library": library,
            "cell": cell,
            "source_sha256": file_sha256(source),
        }
    )


def sync_systemverilog_source(
    client: Any,
    *,
    project_root: Path,
    library: str,
    cell: str,
    source: Path,
    overwrite: bool = False,
    timeout: int = 300,
) -> SystemVerilogSyncResult:
    paths = ProjectContext.from_project_root(project_root)
    source_path = source.resolve()
    if not source_path.is_file() or not source_path.is_relative_to(project_root.resolve()):
        raise ValueError("SystemVerilog source must be a project-owned file")
    source_sha256 = file_sha256(source_path)
    fingerprint = systemverilog_source_fingerprint(
        library=library, cell=cell, source=source_path
    )
    record = ArtifactRecord.begin(
        paths.artifacts.design_sync_attempt(library, cell, new_identity()),
        entities={"library": library, "cell": cell},
        operation="sync-systemverilog-source",
        backend="virtuoso-oa",
        source_fingerprint=fingerprint,
    )
    operation = None
    try:
        staged = record.copy_file(
            "inputs", (source_path.name,), source_path, label="canonical SystemVerilog source"
        )
        with workspace_operation(
            client,
            paths.workspace_root,
            "sync-systemverilog-source",
            policy=OperationPolicy.DIRECT_MUTATION,
        ) as operation, operation.view_lease(
            library,
            cells=(cell,),
            views=((cell, "systemVerilog"),),
        ):
            operation.register_artifact(record)
            with operation.mutation_scope(
                library,
                cells=(cell,),
                views=((cell, "systemVerilog"),),
                phase=f"SystemVerilog source import {library}/{cell}",
            ):
                import_systemverilog_view(
                    client,
                    library=library,
                    cell=cell,
                    source=staged,
                    source_sha256=source_sha256,
                    log_dir=record.directory("logs", "systemverilog"),
                    work_dir=record.directory("work", "systemverilog"),
                    operation=operation,
                    overwrite=overwrite,
                    timeout=timeout,
                )
            receipt = capture_ade_component(
                paths.workspace_root, library, cell, "systemverilog"
            )
        evidence = record.write_json(
            "evidence",
            ("completion.json",),
            {
                "library": library,
                "cell": cell,
                "view": "systemVerilog",
                "source_sha256": source_sha256,
                "source_fingerprint": fingerprint,
                **receipt,
            },
            label="SystemVerilog OA source synchronization proof",
        )
        manifest = record.succeed(
            completion_evidence=(evidence,), details={"oa_component": receipt}
        )
        return SystemVerilogSyncResult(
            manifest_path=manifest,
            library=library,
            cell=cell,
            source_sha256=source_sha256,
            oa_sha256=receipt["oa_sha256"],
        )
    except BaseException as error:
        if record.status == "running":
            record.fail(
                error,
                uncertain_reason=(operation.uncertain_reason if operation else None),
            )
        raise


def attest_systemverilog_source(
    manifest_path: Path,
    *,
    project_root: Path,
    source: Path,
) -> dict[str, object]:
    paths = ProjectContext.from_project_root(project_root)
    manifest = load_manifest(manifest_path)
    if manifest.get("status") != "succeeded":
        raise RuntimeError("SystemVerilog synchronization receipt is not succeeded")
    entities = manifest.get("entities", {})
    if manifest.get("operation") != "sync-systemverilog-source":
        raise RuntimeError("manifest is not a SystemVerilog OA synchronization receipt")
    library = str(entities.get("library", ""))
    cell = str(entities.get("cell", ""))
    expected_fingerprint = systemverilog_source_fingerprint(
        library=library, cell=cell, source=source.resolve()
    )
    if manifest.get("fingerprints", {}).get("source") != expected_fingerprint:
        raise RuntimeError("SystemVerilog source no longer matches the synchronization receipt")
    receipt = manifest.get("details", {}).get("oa_component", {})
    actual = oa_view_digest(
        paths.workspace_root / library / cell / "systemVerilog",
        allowed_symlink_root=project_root.resolve(),
    )
    if receipt.get("oa_sha256") != actual:
        raise RuntimeError("SystemVerilog OA view no longer matches its synchronization receipt")
    return {
        "passed": True,
        "library": library,
        "cell": cell,
        "view": "systemVerilog",
        "source_fingerprint": expected_fingerprint,
        "oa_sha256": actual,
        "manifest": str(manifest_path.resolve()),
    }
