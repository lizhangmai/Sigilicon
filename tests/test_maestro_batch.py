from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess

import pytest

from sigilicon.external_tools import ConfirmedProcessGroupResult
from sigilicon.execution.model import Resources
from sigilicon.virtuoso.maestro_batch import (
    render_isolated_maestro_run_skill,
    run_isolated_maestro,
)
from sigilicon.virtuoso.workspace import OperationPolicy


NONCE = "a" * 32
PROC_FD_PREFIX = f"/proc/{os.getpid()}/fd/"


def test_rendered_native_worker_reads_official_rdb_without_result_path_fallbacks(
    tmp_path: Path,
) -> None:
    source = render_isolated_maestro_run_skill(
        "lib",
        "tb",
        variables={"z": 'quoted"value', "a": "1"},
        simulation_root=tmp_path / "run" / "simulation",
        nonce=NONCE,
        rdb_export=tmp_path / "work" / "maestro-rdb.tsv",
    )

    assert "maeOpenSetup" in source
    assert "maeOpenResults(?history history ?session session)" in source
    assert "maeReadResDB(?historyName history ?session session)" in source
    assert "resultDb->points()" in source
    assert "resultPoint->outputs(?type 'expr ?sortBy 'corner)" in source
    assert "maeGetSpecStatus(" in source
    assert "resultPoint->params()" in source
    assert '"PARAM\\t%d\\t%s\\t%L\\n"' in source
    assert "maeGetOverallSpecStatus(?verbose nil)" in source
    assert "maeExportOutputView" not in source
    assert "axlGetPointPsfDir" not in source
    assert "runObjFile" not in source
    assert "FLOW_ISOLATED_MAESTRO_RDB" in source
    assert source.index('maeSetVar("a" "1"') < source.index('maeSetVar("z"')
    assert 'maeSetVar("z" "quoted\\"value"' in source


@pytest.mark.parametrize(
    ("library", "cell", "nonce"),
    (("../lib", "tb", NONCE), ("lib", "/tb", NONCE), ("lib", "tb", "../bad")),
)
def test_rendered_worker_rejects_unsafe_identity(
    tmp_path: Path, library: str, cell: str, nonce: str
) -> None:
    with pytest.raises(ValueError, match="invalid artifact"):
        render_isolated_maestro_run_skill(
            library,
            cell,
            variables={},
            simulation_root=tmp_path / "simulation",
            nonce=nonce,
            rdb_export=tmp_path / "maestro-rdb.tsv",
        )


def _run_with_fake_process(
    monkeypatch,
    workspace_factory,
    tmp_path: Path,
    *,
    log_text: str,
    returncode: int = 0,
    terminated_after_confirmation: bool = False,
    residual_group_cleaned_after_exit: bool = False,
    result_confirmed: bool = True,
):
    client = object()
    work = tmp_path / "artifacts" / "run" / "work"
    work.mkdir(parents=True)
    worker_log = work / "virtuoso.log"
    rdb_path = work / "maestro-rdb.tsv"
    executable = tmp_path / "virtuoso"
    executable.write_text("tool\n", encoding="utf-8")
    executable.chmod(0o755)
    runtime = Resources(
        environment={
            "SIGILICON_CADENCE_VIRTUOSO": str(executable),
            "XCELIUM_HOME": "/mock/xcelium",
        }
    )
    captured: dict[str, object] = {}

    def fake_process(command, **kwargs):
        kwargs["before_spawn"]()
        for descriptor in kwargs["pass_fds"]:
            assert Path(f"{PROC_FD_PREFIX}{descriptor}").exists()
        Path(command[command.index("-log") + 1]).write_text(
            log_text,
            encoding="utf-8",
        )
        Path(f'{PROC_FD_PREFIX}{kwargs["stdout_fd"]}').write_text(
            "worker stdout\n",
            encoding="utf-8",
        )
        rdb_path.write_text(
            "RDB_SCHEMA\t1\nSUMMARY\t1\t1\nOVERALL_SPEC\tt\n",
            encoding="utf-8",
        )
        assert kwargs["confirmation_probe"]() == (
            f"FLOW_ISOLATED_MAESTRO_STARTED {NONCE} " in log_text
            and f"FLOW_ISOLATED_MAESTRO_FAILED {NONCE}" not in log_text
            and log_text.count(
                f"FLOW_ISOLATED_MAESTRO_STARTED {NONCE} "
            ) == 1
            and log_text.count(f"FLOW_ISOLATED_MAESTRO_DONE {NONCE}") <= 1
            and result_confirmed
        )
        captured.update(command=tuple(command), **kwargs)
        return ConfirmedProcessGroupResult(
            completed=subprocess.CompletedProcess(command, returncode, "", None),
            leader_terminated_after_confirmation=terminated_after_confirmation,
            residual_group_cleaned_after_exit=residual_group_cleaned_after_exit,
        )

    monkeypatch.setattr(
        "sigilicon.virtuoso.maestro_batch.cadence_subprocess_env",
        lambda base: dict(base),
    )
    monkeypatch.setattr(
        "sigilicon.virtuoso.maestro_batch.run_process_group_until_confirmed",
        fake_process,
    )
    with workspace_factory(
        client,
        library="lib",
        policy=OperationPolicy.MAESTRO_RUN,
    ) as operation:
        (operation.root / "cds.lib").write_text("# test\n", encoding="utf-8")
        result = run_isolated_maestro(
            client,
            library="lib",
            cell="tb",
            variables={"z": "2", "a": "1"},
            work_dir=work,
            worker_log=worker_log,
            nonce=NONCE,
            timeout=30,
            operation=operation,
            resources=runtime,
            result_completion_probe=lambda _history: result_confirmed,
            rdb_export=rdb_path,
        )
    return result, captured, worker_log, rdb_path


def test_isolated_runner_exports_native_rdb_and_keeps_exact_resources(
    monkeypatch, workspace_factory, tmp_path: Path
) -> None:
    result, captured, worker_log, rdb_path = _run_with_fake_process(
        monkeypatch,
        workspace_factory,
        tmp_path,
        log_text=(
            f"\\o FLOW_ISOLATED_MAESTRO_STARTED {NONCE} ExplorerRORun.0.RO\n"
            f"\\o FLOW_ISOLATED_MAESTRO_DONE {NONCE}\n"
        ),
    )

    command = captured["command"]
    assert any(part.endswith("/virtuoso") for part in command)
    nograph = command.index("-nograph")
    assert command[nograph : nograph + 2] == ("-nograph", "-nocdsinit")
    assert command[command.index("-cdslib") + 1].startswith(PROC_FD_PREFIX)
    assert command[command.index("-log") + 1].startswith(PROC_FD_PREFIX)
    assert command[command.index("-restore") + 1].startswith(PROC_FD_PREFIX)
    assert str(captured["cwd"]).startswith(PROC_FD_PREFIX)
    assert captured["env"]["IUS_HOME"] == "/mock/xcelium"
    assert len(captured["pass_fds"]) == 9
    assert result.history == "ExplorerRORun.0.RO"
    assert result.rdb_export == rdb_path
    assert result.status == "isolated-worker-complete"
    assert result.stdout == "worker stdout\n"
    assert "maeReadResDB" in result.control_script
    assert "runObjFile" not in result.control_script
    assert "axlGetPointPsfDir" not in result.control_script
    assert (worker_log.parent / "maestro-worker.il").read_text(
        encoding="utf-8"
    ) == result.control_script
    assert rdb_path.read_text(encoding="utf-8").startswith("RDB_SCHEMA")


@pytest.mark.parametrize(
    ("log_text", "returncode"),
    (
        ("no start or completion marker\n", 0),
        (f"\\o FLOW_ISOLATED_MAESTRO_FAILED {NONCE}\n", 0),
        (
            f"\\o FLOW_ISOLATED_MAESTRO_STARTED {NONCE} Run.1\n"
            f"\\o FLOW_ISOLATED_MAESTRO_STARTED {NONCE} Run.1\n"
            f"\\o FLOW_ISOLATED_MAESTRO_DONE {NONCE}\n",
            0,
        ),
        (
            f"\\o FLOW_ISOLATED_MAESTRO_STARTED {NONCE} Run.1\n"
            f"\\o FLOW_ISOLATED_MAESTRO_DONE {NONCE}\n",
            2,
        ),
    ),
)
def test_isolated_runner_rejects_unproven_completion(
    monkeypatch,
    workspace_factory,
    tmp_path: Path,
    log_text: str,
    returncode: int,
) -> None:
    with pytest.raises(
        RuntimeError,
        match="without one started history and confirmed simulator result",
    ):
        _run_with_fake_process(
            monkeypatch,
            workspace_factory,
            tmp_path,
            log_text=log_text,
            returncode=returncode,
        )


def test_isolated_runner_rejects_unconfirmed_result(
    monkeypatch, workspace_factory, tmp_path: Path
) -> None:
    with pytest.raises(RuntimeError, match="confirmed simulator result"):
        _run_with_fake_process(
            monkeypatch,
            workspace_factory,
            tmp_path,
            log_text=f"\\o FLOW_ISOLATED_MAESTRO_STARTED {NONCE} Run.1\n",
            result_confirmed=False,
        )


def test_isolated_runner_accepts_owned_cleanup_after_exact_completion(
    monkeypatch, workspace_factory, tmp_path: Path
) -> None:
    result, _captured, _worker_log, _rdb_path = _run_with_fake_process(
        monkeypatch,
        workspace_factory,
        tmp_path,
        log_text=(
            f"\\o FLOW_ISOLATED_MAESTRO_STARTED {NONCE} Run.1\n"
            f"\\o FLOW_ISOLATED_MAESTRO_DONE {NONCE}\n"
        ),
        returncode=-signal.SIGTERM,
        terminated_after_confirmation=True,
    )

    assert result.history == "Run.1"
    assert result.terminated_after_completion


def test_isolated_runner_remembers_start_marker_beyond_log_tail(
    monkeypatch, workspace_factory, tmp_path: Path
) -> None:
    result, _captured, _worker_log, _rdb_path = _run_with_fake_process(
        monkeypatch,
        workspace_factory,
        tmp_path,
        log_text=(
            f"\\o FLOW_ISOLATED_MAESTRO_STARTED {NONCE} Run.1\n"
            + ("Save image: diagnostic.png written\n" * 4096)
            + f"\\o FLOW_ISOLATED_MAESTRO_DONE {NONCE}\n"
        ),
        residual_group_cleaned_after_exit=True,
    )

    assert result.history == "Run.1"
    assert not result.terminated_after_completion


def test_isolated_runner_rejects_log_outside_direct_work(
    workspace_factory, tmp_path: Path
) -> None:
    client = object()
    work = tmp_path / "work"
    work.mkdir()
    executable = tmp_path / "virtuoso"
    executable.write_text("tool\n", encoding="utf-8")
    executable.chmod(0o755)
    runtime = Resources(
        environment={"SIGILICON_CADENCE_VIRTUOSO": str(executable)}
    )
    with workspace_factory(
        client,
        library="lib",
        policy=OperationPolicy.MAESTRO_RUN,
    ) as operation:
        with pytest.raises(RuntimeError, match="must be a direct work file"):
            run_isolated_maestro(
                client,
                library="lib",
                cell="tb",
                variables={},
                work_dir=work,
                worker_log=tmp_path / "outside.log",
                nonce=NONCE,
                timeout=30,
                operation=operation,
                resources=runtime,
                result_completion_probe=lambda _history: True,
                rdb_export=work / "maestro-rdb.tsv",
            )
