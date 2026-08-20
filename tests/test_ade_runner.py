from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from sigilicon.ams.provenance import ams_fingerprint
from sigilicon.external_tools import ProcessGroupCleanupUncertainError
from sigilicon.workflows.ams_ade_run import (
    find_ade_xrun_log,
    run_ade,
    validate_ade_simulator_success,
)
from sigilicon.ams.spec import load_ams_spec
from sigilicon.virtuoso.maestro_batch import IsolatedMaestroRunResult


def _pass_log(spec) -> str:
    return f"""
FLOW_FINGERPRINT {ams_fingerprint(spec)}
TRUTH vector=0 inputs=0 expected=1 observed=1 pass=1
TRUTH vector=1 inputs=1 expected=0 observed=0 pass=1
SUMMARY vectors=2 failed=0
spectre completes with 0 errors, 4 warnings, 0 notices
TOOL: xrun(64) 25.03: Exiting on test timestamp
"""


def _history_log(workdir: Path, spec, text: str) -> Path:
    path = (
        workdir
        / "simulation"
        / spec.design.library
        / spec.testbench
        / "maestro"
        / "results"
        / "maestro"
        / "Interactive.7"
        / "psf"
        / "TRAN"
        / "psf"
        / "xrun.log"
    )
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return path


@contextmanager
def _workspace(client, _root, name, **_kwargs):
    deferred = []

    @contextmanager
    def lease(*_args, **_kwargs):
        yield

    operation = SimpleNamespace(
        client=client,
        name=name,
        view_lease=lease,
        uncertain_reason=None,
    )
    operation.mark_uncertain = lambda reason: setattr(
        operation, "uncertain_reason", reason
    )
    operation.defer_commit = lambda callback, *, on_failure=None: deferred.append(
        SimpleNamespace(callback=callback, on_failure=on_failure)
    )
    operation.register_artifact = lambda record: record.bind_operation("a" * 32)
    try:
        yield operation
    except BaseException as error:
        for commit in deferred:
            if commit.on_failure is not None:
                commit.on_failure(error)
        raise
    else:
        for commit in deferred:
            commit.callback()


def _patch_workspace(monkeypatch, workspace=_workspace) -> None:
    monkeypatch.setattr("sigilicon.workflows.ams_ade_run.workspace_operation", workspace)
    monkeypatch.setattr(
        "sigilicon.workflows.ams_ade_run.validate_cell_fingerprint",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "sigilicon.workflows.ams_ade_run.validate_ade_component_views",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "sigilicon.workflows.ams_ade_run.read_oa_load_instances",
        lambda *_args, **_kwargs: (
            {
                "instance": "CLOAD0",
                "library": "analogLib",
                "cell": "cap",
                "value": "2f",
                "nets": ("OUT", "0"),
            },
        ),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.ams_ade_run.load_committed_ade_setup",
        lambda namespace, *_args, **_kwargs: SimpleNamespace(
            components={
                "load": {
                    "oa_load_attestation": {
                        "load_cap": "2f",
                        "load_cap_farads": "2E-15",
                        "instances": [
                            {
                                "instance": "CLOAD0",
                                "device_library": "analogLib",
                                "device_cell": "cap",
                                "output": "OUT",
                                "ground": "0",
                                "capacitance_farads": "2E-15",
                            }
                        ],
                    }
                }
            },
            setup_dir=namespace.root / "mock-setup",
            manifest_path=namespace.root / "mock-setup" / "manifest.json",
        ),
    )


def _patch_isolated_success(monkeypatch, source_log: Path) -> None:
    def run(*_args, worker_log: Path, **_kwargs):
        assert _kwargs["result_completion_probe"]("Interactive.7")
        worker_log.write_text("isolated worker complete\n", encoding="utf-8")
        (worker_log.parent / "maestro-worker.il").write_text(
            "canonical worker\n",
            encoding="utf-8",
        )
        (worker_log.parent / "maestro-worker.cds.lib").write_text(
            "DEFINE designLib /owned/library\n",
            encoding="utf-8",
        )
        return IsolatedMaestroRunResult(
            history="Interactive.7",
            status="isolated-worker-complete",
            stdout="worker stdout\n",
            worker_log=worker_log,
            worker_log_text="isolated worker complete\n",
            control_script="canonical worker\n",
        )

    monkeypatch.setattr("sigilicon.workflows.ams_ade_run.run_isolated_maestro", run)
    monkeypatch.setattr(
        "sigilicon.workflows.ams_ade_run.find_ade_xrun_log",
        lambda *_args, **_kwargs: source_log,
    )


def test_find_ade_xrun_log_is_scoped_to_history(project_factory, tmp_path) -> None:
    root, path = project_factory()
    spec = load_ams_spec(path, project_root=root)
    expected = _history_log(tmp_path, spec, _pass_log(spec))

    assert find_ade_xrun_log(tmp_path, spec, "Interactive.7") == expected


@pytest.mark.parametrize("history", ("", ".", "..", "a/b", "a\\b", "/absolute"))
def test_find_ade_xrun_log_rejects_unsafe_history_components(
    history,
    project_factory,
    tmp_path,
) -> None:
    root, path = project_factory()
    spec = load_ams_spec(path, project_root=root)
    with pytest.raises(ValueError, match="invalid artifact Maestro history"):
        find_ade_xrun_log(tmp_path, spec, history)


def test_find_ade_xrun_log_fails_closed_on_ambiguous_history(
    project_factory,
    tmp_path,
) -> None:
    root, path = project_factory()
    spec = load_ams_spec(path, project_root=root)
    _history_log(tmp_path, spec, _pass_log(spec))
    duplicate = (
        tmp_path
        / "simulation"
        / spec.design.library
        / spec.testbench
        / "maestro/results/maestro/Interactive.7/another/xrun.log"
    )
    duplicate.parent.mkdir(parents=True)
    duplicate.write_text(_pass_log(spec), encoding="utf-8")

    with pytest.raises(RuntimeError, match="ambiguous or unsupported xrun.log provenance"):
        find_ade_xrun_log(tmp_path, spec, "Interactive.7")


def test_find_ade_xrun_log_selects_mapped_concrete_design_point(
    project_factory,
    tmp_path,
) -> None:
    root, path = project_factory()
    spec = load_ams_spec(path, project_root=root)
    history_root = (
        tmp_path
        / "simulation"
        / spec.design.library
        / spec.testbench
        / "maestro/results/maestro/ExplorerRORun.0.RO"
    )
    concrete = history_root / "1/TRAN/psf/xrun.log"
    concrete.parent.mkdir(parents=True)
    concrete.write_text(_pass_log(spec), encoding="utf-8")
    point_id = history_root / "1/TRAN/netlist/pointID.txt"
    point_id.parent.mkdir(parents=True)
    point_id.write_text("1\n", encoding="utf-8")
    aggregate = history_root / "psf/TRAN/psf/xrun.log"
    aggregate.parent.mkdir(parents=True)
    aggregate.write_text(_pass_log(spec) + "LOG ENDS\n", encoding="utf-8")

    assert find_ade_xrun_log(tmp_path, spec, "ExplorerRORun.0.RO") == concrete


def test_find_ade_xrun_log_rejects_unmapped_concrete_and_aggregate_pair(
    project_factory,
    tmp_path,
) -> None:
    root, path = project_factory()
    spec = load_ams_spec(path, project_root=root)
    history_root = (
        tmp_path
        / "simulation"
        / spec.design.library
        / spec.testbench
        / "maestro/results/maestro/ExplorerRORun.0.RO"
    )
    concrete = history_root / "1/TRAN/psf/xrun.log"
    concrete.parent.mkdir(parents=True)
    concrete.write_text(_pass_log(spec), encoding="utf-8")
    point_id = history_root / "1/TRAN/netlist/pointID.txt"
    point_id.parent.mkdir(parents=True)
    point_id.write_text("1\n", encoding="utf-8")
    aggregate = history_root / "psf/OTHER/psf/xrun.log"
    aggregate.parent.mkdir(parents=True)
    aggregate.write_text(_pass_log(spec), encoding="utf-8")

    with pytest.raises(RuntimeError, match="ambiguous or unsupported"):
        find_ade_xrun_log(tmp_path, spec, "ExplorerRORun.0.RO")


def test_find_ade_xrun_log_rejects_multiple_concrete_points(
    project_factory,
    tmp_path,
) -> None:
    root, path = project_factory()
    spec = load_ams_spec(path, project_root=root)
    history_root = (
        tmp_path
        / "simulation"
        / spec.design.library
        / spec.testbench
        / "maestro/results/maestro/ExplorerRORun.0.RO"
    )
    concrete = history_root / "1/TRAN/psf/xrun.log"
    concrete.parent.mkdir(parents=True)
    concrete.write_text(_pass_log(spec), encoding="utf-8")
    point_id = history_root / "1/TRAN/netlist/pointID.txt"
    point_id.parent.mkdir(parents=True)
    point_id.write_text("1\n", encoding="utf-8")
    second = history_root / "2/TRAN/psf/xrun.log"
    second.parent.mkdir(parents=True)
    second.write_text(_pass_log(spec), encoding="utf-8")
    second_point_id = history_root / "2/TRAN/netlist/pointID.txt"
    second_point_id.parent.mkdir(parents=True)
    second_point_id.write_text("2\n", encoding="utf-8")
    aggregate = history_root / "psf/TRAN/psf/xrun.log"
    aggregate.parent.mkdir(parents=True)
    aggregate.write_text(_pass_log(spec), encoding="utf-8")

    with pytest.raises(RuntimeError, match="ambiguous or unsupported"):
        find_ade_xrun_log(tmp_path, spec, "ExplorerRORun.0.RO")


def test_run_ade_archives_and_enforces_truth(monkeypatch, project_factory, tmp_path) -> None:
    root, path = project_factory()
    spec = load_ams_spec(path, project_root=root)
    source_log = _history_log(tmp_path, spec, _pass_log(spec))
    _patch_workspace(monkeypatch)
    _patch_isolated_success(monkeypatch, source_log)
    result = run_ade(spec, object(), artifact_root=tmp_path / "artifacts")

    assert result.vectors == 2
    assert result.copied_log.is_file()
    assert result.truth_table.is_file()
    assert result.namespace_dir == (
        tmp_path / "artifacts/verification/designLib/tb_inv/ade"
    )
    assert result.run_dir.parent == result.namespace_dir / "runs"
    assert result.setup_dir == result.namespace_dir / "mock-setup"
    state = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert state["status"] == "succeeded"
    assert state["details"]["variables"] == {}
    assert state["fingerprints"]["run"] == result.run_fingerprint
    assert state["fingerprints"]["setup"] == ams_fingerprint(spec)


def test_run_ade_rejects_failed_truth(monkeypatch, project_factory, tmp_path) -> None:
    root, path = project_factory()
    spec = load_ams_spec(path, project_root=root)
    _patch_workspace(monkeypatch)
    failed = _pass_log(spec).replace("observed=0 pass=1", "observed=1 pass=0").replace(
        "failed=0", "failed=1"
    )
    source_log = _history_log(tmp_path, spec, failed)
    _patch_isolated_success(monkeypatch, source_log)
    monkeypatch.setattr(
        "sigilicon.workflows.ams_ade_run.new_identity",
        lambda: "1" * 32,
    )

    with pytest.raises(RuntimeError, match="ADE run directory retained"):
        run_ade(spec, object(), artifact_root=tmp_path / "artifacts")
    failed_run = (
        tmp_path / "artifacts/verification/designLib/tb_inv/ade/runs" / ("1" * 32)
    )
    assert (failed_run / "logs/xrun.log").read_text(encoding="utf-8") == failed
    assert (failed_run / "results/truth_table.csv").is_file()
    state = json.loads((failed_run / "manifest.json").read_text(encoding="utf-8"))
    assert state["status"] == "failed"
    assert state["backend"] == "ade"


@pytest.mark.parametrize(
    "failure",
    (
        "xrun(64): *F,ELBERR: elaboration failed\n",
        "spectre completes with 2 errors, 0 warnings, 0 notices\n",
        "ERROR: simulator failed after producing output\n",
    ),
)
def test_ade_simulator_success_rejects_fatal_or_nonzero_error_summary(
    failure, project_factory
) -> None:
    root, path = project_factory()
    spec = load_ams_spec(path, project_root=root)

    with pytest.raises(RuntimeError, match="fatal or error"):
        validate_ade_simulator_success(_pass_log(spec) + failure)


def test_run_ade_does_not_accept_truth_rows_after_simulator_failure(
    monkeypatch, project_factory, tmp_path
) -> None:
    root, path = project_factory()
    spec = load_ams_spec(path, project_root=root)
    _patch_workspace(monkeypatch)
    failed = _pass_log(spec).replace(
        "TOOL: xrun(64)",
        "xrun(64): *F,ELBERR: fatal after vectors\nTOOL: xrun(64)",
    )
    source_log = _history_log(tmp_path, spec, failed)
    _patch_isolated_success(monkeypatch, source_log)
    monkeypatch.setattr(
        "sigilicon.workflows.ams_ade_run.new_identity",
        lambda: "9" * 32,
    )

    with pytest.raises(RuntimeError, match="fatal or error"):
        run_ade(spec, object(), artifact_root=tmp_path / "artifacts")

    state = json.loads(
        (
            tmp_path
            / "artifacts/verification/designLib/tb_inv/ade/runs"
            / ("9" * 32)
            / "manifest.json"
        ).read_text(encoding="utf-8")
    )
    assert state["status"] == "failed"
    assert state["completion_evidence"] == []


def test_run_ade_start_failure_keeps_uuid_attempt_without_fake_log(
    monkeypatch, project_factory, tmp_path
) -> None:
    root, path = project_factory()
    spec = load_ams_spec(path, project_root=root)
    _patch_workspace(monkeypatch)
    monkeypatch.setattr(
        "sigilicon.workflows.ams_ade_run.run_isolated_maestro",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("start failed")),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.ams_ade_run.new_identity",
        lambda: "2" * 32,
    )

    with pytest.raises(RuntimeError, match="ADE run directory retained"):
        run_ade(spec, object(), artifact_root=tmp_path / "artifacts")

    run_dir = tmp_path / "artifacts/verification/designLib/tb_inv/ade/runs" / ("2" * 32)
    state = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert state["status"] == "failed"
    assert state["details"]["error"] == "start failed"
    assert not (run_dir / "logs/xrun.log").exists()


def test_run_ade_commits_only_after_workspace_exit_audit(
    monkeypatch, project_factory, tmp_path
) -> None:
    root, path = project_factory()
    spec = load_ams_spec(path, project_root=root)
    source_log = _history_log(tmp_path, spec, _pass_log(spec))
    observed_statuses: list[str] = []

    @contextmanager
    def audited_workspace(client, _root, name, **_kwargs):
        deferred = []

        @contextmanager
        def lease(*_args, **_kwargs):
            yield

        operation = SimpleNamespace(
            client=client,
            name=name,
            view_lease=lease,
            uncertain_reason=None,
        )
        operation.defer_commit = lambda callback, *, on_failure=None: deferred.append(
            SimpleNamespace(callback=callback, on_failure=on_failure)
        )
        operation.register_artifact = lambda record: record.bind_operation("a" * 32)
        yield operation
        run_manifest = next((tmp_path / "artifacts").rglob("manifest.json"))
        observed_statuses.append(json.loads(run_manifest.read_text())["status"])
        for commit in deferred:
            commit.callback()
        observed_statuses.append(json.loads(run_manifest.read_text())["status"])

    _patch_workspace(monkeypatch, audited_workspace)
    _patch_isolated_success(monkeypatch, source_log)

    run_ade(spec, object(), artifact_root=tmp_path / "artifacts")

    assert observed_statuses == ["running", "succeeded"]


def test_run_ade_late_workspace_uncertainty_cannot_commit(
    monkeypatch, project_factory, tmp_path
) -> None:
    root, path = project_factory()
    spec = load_ams_spec(path, project_root=root)
    source_log = _history_log(tmp_path, spec, _pass_log(spec))

    @contextmanager
    def uncertain_workspace(client, _root, name, **_kwargs):
        deferred = []

        @contextmanager
        def lease(*_args, **_kwargs):
            yield

        operation = SimpleNamespace(
            client=client,
            name=name,
            view_lease=lease,
            uncertain_reason=None,
        )
        operation.defer_commit = lambda callback, *, on_failure=None: deferred.append(
            SimpleNamespace(callback=callback, on_failure=on_failure)
        )
        operation.register_artifact = lambda record: record.bind_operation("a" * 32)
        yield operation
        operation.uncertain_reason = "workspace exit audit failed"
        error = RuntimeError(operation.uncertain_reason)
        for commit in deferred:
            if commit.on_failure is not None:
                commit.on_failure(error)
        raise error

    _patch_workspace(monkeypatch, uncertain_workspace)
    _patch_isolated_success(monkeypatch, source_log)
    monkeypatch.setattr(
        "sigilicon.workflows.ams_ade_run.new_identity",
        lambda: "3" * 32,
    )

    with pytest.raises(RuntimeError, match="workspace exit audit failed"):
        run_ade(spec, object(), artifact_root=tmp_path / "artifacts")

    run_dir = (
        tmp_path
        / "artifacts/verification/designLib/tb_inv/ade/runs"
        / ("3" * 32)
    )
    state = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert state["status"] == "uncertain"
    assert state["uncertain_reason"] == "workspace exit audit failed"


def test_run_ade_isolated_worker_failure_is_failed_not_uncertain(
    monkeypatch, project_factory, tmp_path
) -> None:
    root, path = project_factory()
    spec = load_ams_spec(path, project_root=root)
    _patch_workspace(monkeypatch)

    def failed_worker(*_args, **_kwargs):
        raise RuntimeError("isolated worker process group timed out and was terminated")

    monkeypatch.setattr(
        "sigilicon.workflows.ams_ade_run.run_isolated_maestro", failed_worker
    )
    monkeypatch.setattr(
        "sigilicon.workflows.ams_ade_run.new_identity",
        lambda: "4" * 32,
    )

    with pytest.raises(RuntimeError, match="ADE run directory retained"):
        run_ade(
            spec,
            object(),
            artifact_root=tmp_path / "artifacts",
            variables={"z": "2", "a": "1"},
        )

    run_dir = tmp_path / "artifacts/verification/designLib/tb_inv/ade/runs" / ("4" * 32)
    state = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert state["status"] == "failed"
    assert state["uncertain_reason"] is None
    assert "process group timed out" in state["details"]["error"]
    assert list(state["details"]["variables"]) == ["a", "z"]


def test_run_ade_unproven_worker_cleanup_is_uncertain_with_incident(
    monkeypatch, project_factory, tmp_path
) -> None:
    root, path = project_factory()
    spec = load_ams_spec(path, project_root=root)
    _patch_workspace(monkeypatch)

    def uncertain_worker(*_args, operation, **_kwargs):
        reason = "isolated worker cleanup could not be proven"
        operation.mark_uncertain(reason)
        raise ProcessGroupCleanupUncertainError(reason)

    monkeypatch.setattr(
        "sigilicon.workflows.ams_ade_run.run_isolated_maestro", uncertain_worker
    )
    monkeypatch.setattr(
        "sigilicon.workflows.ams_ade_run.new_identity",
        lambda: "5" * 32,
    )

    with pytest.raises(RuntimeError, match="ADE run directory retained"):
        run_ade(spec, object(), artifact_root=tmp_path / "artifacts")

    run_dir = tmp_path / "artifacts/verification/designLib/tb_inv/ade/runs" / ("5" * 32)
    state = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert state["status"] == "uncertain"
    assert "cleanup could not be proven" in state["uncertain_reason"]
