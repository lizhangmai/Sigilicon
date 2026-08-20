from __future__ import annotations

import copy
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from sigilicon.ams.provenance import ams_fingerprint
from sigilicon.ams.spec import load_ams_spec
from sigilicon.artifacts import (
    ArtifactManifestError,
    ArtifactRecord,
    load_manifest,
    validate_manifest,
)
from sigilicon.external_tools import ProcessGroupCleanupUncertainError
from sigilicon.paths import ProjectContext
from sigilicon.workflows.ams_standalone import run_standalone


PROC_FD_PREFIX = f"/proc/{os.getpid()}/fd/"


def _record(tmp_path: Path, identity: str = "1" * 32) -> ArtifactRecord:
    execution = ProjectContext.from_project_root(tmp_path).artifacts.standalone_run(
        "lib", "tb", identity
    )
    return ArtifactRecord.begin(
        execution,
        entities={"library": "lib", "cell": "dut", "testbench": "tb"},
        operation="simulate",
        backend="standalone",
        setup_fingerprint="2" * 64,
        run_fingerprint="3" * 64,
    )


def test_artifact_manifest_keeps_exact_and_semantic_fingerprints_distinct(
    tmp_path: Path,
) -> None:
    execution = ProjectContext.from_project_root(tmp_path).artifacts.standalone_run(
        "lib", "tb", "4" * 32
    )
    record = ArtifactRecord.begin(
        execution,
        entities={"library": "lib", "cell": "dut", "testbench": "tb"},
        operation="simulate",
        backend="virtuoso-oa",
        source_fingerprint="a" * 64,
        semantic_fingerprint="b" * 64,
        setup_fingerprint="c" * 64,
        run_fingerprint="d" * 64,
    )

    assert record.manifest["fingerprints"] == {
        "source": "a" * 64,
        "semantic": "b" * 64,
        "setup": "c" * 64,
        "run": "d" * 64,
    }
    assert validate_manifest(record.manifest)["fingerprints"]["semantic"] == "b" * 64


def test_standalone_uses_roles_and_the_current_manifest_contract(
    monkeypatch, project_factory, tmp_path: Path
) -> None:
    root, path = project_factory()
    spec = load_ams_spec(path, project_root=root)
    xrun = tmp_path / "xrun"
    xrun.write_text("offline sentinel\n", encoding="utf-8")
    output = f"""
FLOW_FINGERPRINT {ams_fingerprint(spec)}
TRUTH vector=0 inputs=0 expected=1 observed=1 pass=1
TRUTH vector=1 inputs=1 expected=0 observed=0 pass=1
SUMMARY vectors=2 failed=0
"""
    invocation: dict[str, object] = {}

    def fake_xrun(command, **kwargs):
        kwargs["before_spawn"]()
        invocation["command"] = tuple(command)
        invocation.update(kwargs)
        for descriptor in kwargs["pass_fds"]:
            assert Path(f"{PROC_FD_PREFIX}{descriptor}").exists()
        return SimpleNamespace(stdout=output, returncode=0)

    monkeypatch.setattr("sigilicon.workflows.ams_standalone.xrun_env", lambda _xrun: {})
    monkeypatch.setattr(
        "sigilicon.workflows.ams_standalone.run_process_group",
        fake_xrun,
    )
    monkeypatch.setattr(
        "sigilicon.workflows.ams_standalone.new_identity", lambda: "4" * 32
    )

    result = run_standalone(
        spec,
        artifact_root=tmp_path / "artifacts",
        xrun=xrun,
    )

    assert result.namespace_dir == (
        tmp_path / "artifacts/verification/designLib/tb_inv/standalone"
    )
    assert result.run_dir == result.namespace_dir / "runs" / ("4" * 32)
    assert result.truth_table.parent.name == "results"
    assert (result.run_dir / "inputs").is_dir()
    analog = (result.run_dir / "inputs/analog.scs").read_text(encoding="utf-8")
    assert PROC_FD_PREFIX not in analog
    assert f'reffile="{result.run_dir}/inputs/' in analog
    references = json.loads(
        (result.run_dir / "inputs/external-input-references.json").read_text(
            encoding="utf-8"
        )
    )
    assert references["source_netlist"]["path"] == str(spec.design.source_netlist)
    assert len(references["source_netlist"]["sha256"]) == 64
    assert len(references["pdk_model"]["sha256"]) == 64
    tool_analog = (result.run_dir / "work/analog.tool.scs").read_text(
        encoding="utf-8"
    )
    assert f'reffile="{PROC_FD_PREFIX}' in tool_analog
    assert f'include "{PROC_FD_PREFIX}' in tool_analog
    assert str(invocation["cwd"]).startswith(PROC_FD_PREFIX)
    assert len(invocation["pass_fds"]) == 11
    assert all(
        value.startswith(PROC_FD_PREFIX)
        for value in invocation["command"][-2:]
    )
    assert invocation["command"][-2].endswith(".sv")
    assert invocation["command"][-1].endswith(".scs")
    assert (result.run_dir / "logs/xrun.stdout.log").is_file()
    assert (result.run_dir / "work").is_dir()
    state = load_manifest(result.run_dir / "manifest.json")
    assert state["status"] == "succeeded"
    assert state["run_id"] == "4" * 32
    assert state["operation_id"] == "4" * 32
    assert state["attempt_id"] is None
    assert state["fingerprints"]["setup"] == ams_fingerprint(spec)
    assert state["details"]["variables"] == {}
    assert state["completion_evidence"] == ["results/truth_table.csv"]


def test_standalone_unproven_process_cleanup_is_uncertain_with_incident(
    monkeypatch, project_factory, tmp_path: Path
) -> None:
    root, path = project_factory()
    spec = load_ams_spec(path, project_root=root)
    xrun = tmp_path / "xrun"
    xrun.write_text("offline sentinel\n", encoding="utf-8")
    identities = iter(("5" * 32, "6" * 32))

    monkeypatch.setattr("sigilicon.workflows.ams_standalone.xrun_env", lambda _xrun: {})
    monkeypatch.setattr(
        "sigilicon.workflows.ams_standalone.run_process_group",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ProcessGroupCleanupUncertainError("xrun group cleanup was not proven")
        ),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.ams_standalone.new_identity", lambda: next(identities)
    )

    with pytest.raises(ProcessGroupCleanupUncertainError, match="not proven"):
        run_standalone(
            spec,
            artifact_root=tmp_path / "artifacts",
            xrun=xrun,
        )

    run_dir = (
        tmp_path
        / "artifacts/verification/designLib/tb_inv/standalone/runs"
        / ("5" * 32)
    )
    state = load_manifest(run_dir / "manifest.json")
    assert state["status"] == "uncertain"
    assert state["operation_id"] == "6" * 32
    assert state["uncertain_reason"] == (
        "standalone process-group cleanup could not be proven: "
        "xrun group cleanup was not proven"
    )
    assert state["incident_reference"] == (
        f"system/operations/{'6' * 32}/incident.json"
    )
    incident = json.loads(
        (tmp_path / "artifacts" / state["incident_reference"]).read_text(
            encoding="utf-8"
        )
    )
    assert incident["status"] == "uncertain"
    assert incident["policy"] == "isolated-process-group"
    assert incident["ownership_scopes"] == [{"kind": "xrun-process-group"}]


def test_standalone_incident_write_failure_keeps_uncertain_provenance(
    monkeypatch, project_factory, tmp_path: Path
) -> None:
    root, path = project_factory()
    spec = load_ams_spec(path, project_root=root)
    xrun = tmp_path / "xrun"
    xrun.write_text("offline sentinel\n", encoding="utf-8")
    identities = iter(("7" * 32, "8" * 32))
    monkeypatch.setattr("sigilicon.workflows.ams_standalone.xrun_env", lambda _xrun: {})
    monkeypatch.setattr(
        "sigilicon.workflows.ams_standalone.run_process_group",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ProcessGroupCleanupUncertainError("xrun cleanup unknown")
        ),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.ams_standalone.write_operation_incident",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("journal unavailable")),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.ams_standalone.new_identity", lambda: next(identities)
    )

    with pytest.raises(ProcessGroupCleanupUncertainError, match="cleanup unknown"):
        run_standalone(spec, artifact_root=tmp_path / "artifacts", xrun=xrun)

    state = load_manifest(
        tmp_path
        / "artifacts/verification/designLib/tb_inv/standalone/runs"
        / ("7" * 32)
        / "manifest.json"
    )
    assert state["status"] == "uncertain"
    assert state["incident_reference"] is None
    assert state["details"]["incident_recording_error"] == (
        "OSError: journal unavailable"
    )


def test_status_machine_requires_proof_and_forbids_terminal_rewrite(tmp_path: Path) -> None:
    record = _record(tmp_path)
    with pytest.raises(RuntimeError, match="without completion evidence"):
        record.succeed(completion_evidence=())
    proof = record.path("results", "proof.txt")
    proof.write_text("confirmed\n", encoding="utf-8")
    record.add_file("results", proof)
    record.succeed(completion_evidence=(proof,))
    with pytest.raises(RuntimeError, match="illegal artifact status transition"):
        record.fail(RuntimeError("late failure"))


def test_completion_evidence_is_reverified_before_success(tmp_path: Path) -> None:
    record = _record(tmp_path)
    proof = record.write_text(
        "results",
        ("proof.txt",),
        "original\n",
        label="completion proof",
    )
    proof.write_text("tampered\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="changed after registration"):
        record.succeed(completion_evidence=(proof,))
    assert record.status == "running"
    assert load_manifest(record.paths.manifest)["status"] == "running"


def test_atomic_transition_failure_leaves_memory_and_disk_running(
    monkeypatch,
    tmp_path: Path,
) -> None:
    record = _record(tmp_path)
    proof = record.write_text("results", ("proof.txt",), "confirmed\n")

    def fail_write(_path, _value):
        raise OSError("manifest replace failed")

    monkeypatch.setattr("sigilicon.artifacts.atomic_write_json", fail_write)
    with pytest.raises(OSError, match="manifest replace failed"):
        record.succeed(completion_evidence=(proof,))
    assert record.status == "running"
    assert json.loads(record.paths.manifest.read_text())["status"] == "running"


def test_terminal_artifact_rejects_all_mutation_except_incident_link(
    tmp_path: Path,
) -> None:
    record = _record(tmp_path)
    proof = record.write_text("results", ("proof.txt",), "confirmed\n")
    record.succeed(completion_evidence=(proof,))

    with pytest.raises(RuntimeError, match="terminal artifact"):
        record.write_text("logs", ("late.log",), "late\n")
    with pytest.raises(RuntimeError, match="terminal artifact"):
        record.add_file("results", proof)
    with pytest.raises(RuntimeError, match="terminal artifact"):
        record.directory("results", "late")
    with pytest.raises(RuntimeError, match="terminal artifact"):
        record.bind_operation("a" * 32)


def test_manifest_rejects_unsafe_links_and_invalid_file_metadata(tmp_path: Path) -> None:
    record = _record(tmp_path)
    proof = record.path("results", "proof.txt")
    proof.write_text("confirmed\n", encoding="utf-8")
    record.add_file("results", proof)

    unsafe_link = copy.deepcopy(record.manifest)
    unsafe_link["links"]["references"]["setup_manifest"] = "../../outside.json"
    with pytest.raises(ArtifactManifestError, match="unsafe manifest references link"):
        validate_manifest(unsafe_link)

    invalid_digest = copy.deepcopy(record.manifest)
    invalid_digest["files"]["results"][0]["sha256"] = "not-a-digest"
    with pytest.raises(ArtifactManifestError, match="invalid digest or size"):
        validate_manifest(invalid_digest)

    for noncanonical in (
        "results//proof.txt",
        "results/./proof.txt",
        "results/proof.txt/",
    ):
        unsafe_path = copy.deepcopy(record.manifest)
        unsafe_path["links"]["references"]["proof"] = noncanonical
        with pytest.raises(ArtifactManifestError, match="unsafe manifest references link"):
            validate_manifest(unsafe_path)


def test_run_completion_evidence_cannot_come_from_logs(tmp_path: Path) -> None:
    record = _record(tmp_path)
    log = record.write_text("logs", ("claimed-proof.log",), "looks successful\n")

    with pytest.raises(ArtifactManifestError, match="must use results/"):
        record.succeed(completion_evidence=(log,))
    assert record.status == "running"


def test_attempt_completion_evidence_cannot_come_from_logs(tmp_path: Path) -> None:
    execution = ProjectContext.from_project_root(tmp_path).artifacts.design_sync_attempt(
        "lib", "dut", "7" * 32
    )
    record = ArtifactRecord.begin(
        execution,
        entities={"library": "lib", "cell": "dut"},
        operation="sync-design",
        backend="oa",
        source_fingerprint="8" * 64,
    )
    log = record.write_text("logs", ("claimed-proof.log",), "looks successful\n")

    with pytest.raises(ArtifactManifestError, match="must use evidence/"):
        record.succeed(completion_evidence=(log,))
    assert record.status == "running"


def test_oa_text_view_artifact_requires_exact_view_identity(tmp_path: Path) -> None:
    execution = ProjectContext.from_project_root(tmp_path).artifacts.oa_text_view_attempt(
        "lib", "dut", "veriloga", "9" * 32
    )
    record = ArtifactRecord.begin(
        execution,
        entities={"library": "lib", "cell": "dut", "view": "veriloga"},
        operation="sync-oa-text-view",
        backend="virtuoso-oa",
        source_fingerprint="8" * 64,
    )

    assert record.manifest["artifact_kind"] == "oa_text_view"
    assert record.manifest["entities"] == {
        "library": "lib",
        "cell": "dut",
        "view": "veriloga",
    }


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
    del missing_entity["entities"]["testbench"]
    with pytest.raises(ArtifactManifestError, match="entities do not match"):
        validate_manifest(missing_entity)

    forged_running = copy.deepcopy(record.manifest)
    forged_running["uncertain_reason"] = "not actually running"
    with pytest.raises(ArtifactManifestError, match="terminal provenance"):
        validate_manifest(forged_running)


def test_artifact_references_reject_symlinks_even_when_target_stays_inside_role(
    tmp_path: Path,
) -> None:
    record = _record(tmp_path)
    target = record.path("results", "proof.txt")
    target.write_text("confirmed\n", encoding="utf-8")
    alias = record.path("results", "proof-alias.txt")
    alias.symlink_to(target.name)

    with pytest.raises(RuntimeError, match="cannot traverse a symlink"):
        record.add_file("results", alias)

    real_directory = record.path("results", "real-directory")
    real_directory.mkdir()
    directory_alias = record.path("results", "directory-alias")
    directory_alias.symlink_to(real_directory.name, target_is_directory=True)
    with pytest.raises(RuntimeError, match="cannot traverse a symlink"):
        record.directory("results", "directory-alias")


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
        proof = record.path("results", "proof.txt")
        proof.write_text(identity, encoding="utf-8")
        record.add_file("results", proof)
        record.succeed(completion_evidence=(proof,))
        return record.paths.root

    with ThreadPoolExecutor(max_workers=8) as executor:
        roots = list(executor.map(create, identities))

    assert len(set(roots)) == len(identities)
    assert {load_manifest(root / "manifest.json")["run_id"] for root in roots} == set(
        identities
    )


def test_setup_fingerprint_namespace_and_attempt_identity_are_separate(tmp_path: Path) -> None:
    ade = ProjectContext.from_project_root(tmp_path).artifacts.ade("lib", "tb")
    execution = ade.setup_attempt("a" * 64, "b" * 32)
    assert execution.identity == "b" * 32
    assert execution.root.parts.count("b" * 32) == 1
    assert execution.root.parts.count("a" * 64) == 1
    assert execution.root.parent.name == "attempts"
