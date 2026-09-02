from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from sigilicon.artifacts import (
    ArtifactManifestError,
    ArtifactRecord,
    load_manifest,
    validate_manifest,
)
from sigilicon.paths import ProjectContext


def _record(tmp_path: Path, identity: str = "1" * 32) -> ArtifactRecord:
    execution = ProjectContext.from_project_root(tmp_path).artifacts.operation_run(
        owner="lib",
        operation="spectre",
        variant="nominal",
        run_id=identity,
    )
    return ArtifactRecord.begin(
        execution,
        entities={"owner": "lib", "variant": "nominal"},
        operation="spectre",
        backend="standalone",
    )


def test_artifact_manifest_records_git_source(
    tmp_path: Path,
) -> None:
    execution = ProjectContext.from_project_root(tmp_path).artifacts.operation_run(
        owner="lib",
        operation="spectre",
        variant="nominal",
        run_id="4" * 32,
    )
    record = ArtifactRecord.begin(
        execution,
        entities={"owner": "lib", "variant": "nominal"},
        operation="spectre",
        backend="virtuoso-oa",
        source={
            "project": {
                "commit": "a" * 40,
                "working_tree_dirty": False,
                "changes": [],
            }
        },
    )

    assert record.manifest["source"] == {
        "project": {
            "commit": "a" * 40,
            "working_tree_dirty": False,
            "changes": [],
        }
    }
    assert validate_manifest(record.manifest)["source"] == record.manifest["source"]



def test_status_machine_requires_proof_and_forbids_terminal_rewrite(tmp_path: Path) -> None:
    record = _record(tmp_path)
    with pytest.raises(RuntimeError, match="without completion evidence"):
        record.succeed(completion_evidence=())
    proof = record.path("outputs", "proof.txt")
    proof.write_text("confirmed\n", encoding="utf-8")
    record.add_file("outputs", proof)
    record.succeed(completion_evidence=(proof,))
    with pytest.raises(RuntimeError, match="illegal artifact status transition"):
        record.fail(RuntimeError("late failure"))


def test_completion_evidence_is_reverified_before_success(tmp_path: Path) -> None:
    record = _record(tmp_path)
    proof = record.write_text(
        "outputs",
        ("proof.txt",),
        "original\n",
        label="completion proof",
    )
    proof.write_text("tampered\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="changed after registration"):
        record.succeed(completion_evidence=(proof,))
    assert record.status == "running"
    assert load_manifest(record.paths.manifest)["status"] == "running"


def test_terminal_artifact_rejects_all_mutation_except_incident_link(
    tmp_path: Path,
) -> None:
    record = _record(tmp_path)
    proof = record.write_text("outputs", ("proof.txt",), "confirmed\n")
    record.succeed(completion_evidence=(proof,))

    with pytest.raises(RuntimeError, match="terminal artifact"):
        record.write_text("logs", ("late.log",), "late\n")


def test_run_completion_evidence_cannot_come_from_logs(tmp_path: Path) -> None:
    record = _record(tmp_path)
    log = record.write_text("logs", ("claimed-proof.log",), "looks successful\n")

    with pytest.raises(ArtifactManifestError, match="must use outputs/"):
        record.succeed(completion_evidence=(log,))
    assert record.status == "running"


def test_manifest_rejects_noncanonical_incident_reference(tmp_path: Path) -> None:
    record = _record(tmp_path)
    hostile = copy.deepcopy(record.manifest)
    hostile["operation_id"] = "a" * 32
    hostile["incident_reference"] = (
        f"system//operations/{'a' * 32}/incident.json"
    )

    with pytest.raises(ArtifactManifestError, match="unsafe incident reference"):
        validate_manifest(hostile)


def test_manifest_binds_kind_to_identity_entities_and_status_provenance(
    tmp_path: Path,
) -> None:
    record = _record(tmp_path)

    wrong_identity = copy.deepcopy(record.manifest)
    wrong_identity["attempt_id"] = wrong_identity["run_id"]
    wrong_identity["run_id"] = None
    with pytest.raises(ArtifactManifestError, match="requires run_id identity"):
        validate_manifest(wrong_identity)

    missing_entity = copy.deepcopy(record.manifest)
    del missing_entity["entities"]["owner"]
    with pytest.raises(ArtifactManifestError, match="entities do not match"):
        validate_manifest(missing_entity)

    forged_running = copy.deepcopy(record.manifest)
    forged_running["uncertain_reason"] = "not actually running"
    with pytest.raises(ArtifactManifestError, match="terminal provenance"):
        validate_manifest(forged_running)


def test_artifact_files_reject_symlinks_even_when_target_stays_inside_role(
    tmp_path: Path,
) -> None:
    record = _record(tmp_path)
    target = record.path("outputs", "proof.txt")
    target.write_text("confirmed\n", encoding="utf-8")
    alias = record.path("outputs", "proof-alias.txt")
    alias.symlink_to(target.name)

    with pytest.raises(RuntimeError, match="cannot traverse a symlink"):
        record.add_file("outputs", alias)

    real_directory = record.path("outputs", "real-directory")
    real_directory.mkdir()
    directory_alias = record.path("outputs", "directory-alias")
    directory_alias.symlink_to(real_directory.name, target_is_directory=True)
    with pytest.raises(RuntimeError, match="cannot traverse a symlink"):
        record.directory("outputs", "directory-alias")


def test_partial_and_uncertain_require_structured_provenance(tmp_path: Path) -> None:
    partial = _record(tmp_path, "5" * 32)
    with pytest.raises(RuntimeError, match="requires provenance"):
        partial._transition("partial")
    partial.fail(
        RuntimeError("symbol failed"),
        partial_failure={
            "completed_cells": ["leaf"],
            "failed_cell": "top",
            "failed_stage": "symbol",
        },
    )
    assert load_manifest(partial.paths.manifest)["status"] == "partial"

    uncertain = _record(tmp_path, "6" * 32)
    with pytest.raises(RuntimeError, match="requires a reason"):
        uncertain._transition("uncertain")
    uncertain.fail(RuntimeError("lost reply"), uncertain_reason="completion unknown")
    state = load_manifest(uncertain.paths.manifest)
    assert state["status"] == "uncertain"
    assert state["uncertain_reason"] == "completion unknown"


def test_concurrent_runs_never_overwrite_each_other(tmp_path: Path) -> None:
    identities = [f"{index:032x}" for index in range(1, 17)]

    def create(identity: str) -> Path:
        record = _record(tmp_path, identity)
        proof = record.path("outputs", "proof.txt")
        proof.write_text(identity, encoding="utf-8")
        record.add_file("outputs", proof)
        record.succeed(completion_evidence=(proof,))
        return record.paths.root

    with ThreadPoolExecutor(max_workers=8) as executor:
        roots = list(executor.map(create, identities))

    assert len(set(roots)) == len(identities)
    assert {load_manifest(root / "manifest.json")["run_id"] for root in roots} == set(
        identities
    )
