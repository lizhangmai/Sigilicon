from __future__ import annotations

import math
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from sigilicon.external_tools import (
    cadence_subprocess_env,
    find_xrun,
    owned_atomic_output_file,
    owned_directory,
    owned_input_file,
    owned_output_file,
    owned_scratch_directory,
    owned_sealed_input,
    ProcessRequest,
    ProcessResult,
    ProcessGroupCleanupUncertainError,
    managed_process,
    run_process_group_until_confirmed,
    xrun_env,
)
from sigilicon.execution.step_files import StepFiles
from sigilicon.workflows.spectre import find_spectre, run_spectre_deck


def test_cadence_child_environment_removes_conflicting_license_variable() -> None:
    source = {
        "LM_LICENSE_FILE": "mentor-or-synopsys-license",
        "CDS_LIC_FILE": "cadence-license",
        "PATH": "/tools/bin",
    }

    result = cadence_subprocess_env(source)

    assert "LM_LICENSE_FILE" not in result
    assert result["CDS_LIC_FILE"] == "cadence-license"
    assert result["PATH"] == "/tools/bin"
    assert source["LM_LICENSE_FILE"] == "mentor-or-synopsys-license"


def test_xrun_resolution_and_environment_use_one_installation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    installation = tmp_path / "xcelium"
    launcher = installation / "tools/bin/xrun"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("launcher\n", encoding="utf-8")
    monkeypatch.setenv("XCELIUM_HOME", str(installation))
    monkeypatch.delenv("IUS_HOME", raising=False)
    monkeypatch.setenv("PATH", "")

    resolved = find_xrun(
        environment={
            "XCELIUM_HOME": str(installation),
            "PATH": "",
        }
    )
    environment = xrun_env(
        resolved,
        {"XCELIUM_HOME": str(installation)},
    )

    assert resolved == launcher.resolve()
    assert environment["XCELIUM_HOME"] == str(installation)
    assert environment["IUS_HOME"] == str(installation)
    assert environment["CDS_INST_DIR"] == str(installation)
    assert environment["PATH"].split(os.pathsep)[:2] == [
        str(installation / "tools/bin"),
        str(installation / "bin"),
    ]
    assert not environment["PATH"].endswith(os.pathsep)
    assert not environment["LD_LIBRARY_PATH"].endswith(os.pathsep)


def test_xrun_resolution_ignores_ambient_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    installation = tmp_path / "ambient-xcelium"
    launcher = installation / "tools/bin/xrun"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("launcher\n", encoding="utf-8")
    monkeypatch.setenv("XCELIUM_HOME", str(installation))

    with pytest.raises(FileNotFoundError):
        find_xrun()


def test_spectre_resolution_uses_only_explicit_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    launcher = tmp_path / "spectre"
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(0o755)
    monkeypatch.setenv("VB_SPECTRE_BIN", str(launcher))

    with pytest.raises(FileNotFoundError):
        find_spectre()
    assert find_spectre(
        environment={"VB_SPECTRE_BIN": str(launcher)}
    ) == launcher.absolute()


def test_xrun_environment_uses_the_supplied_resource_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    installation = tmp_path / "selected-xcelium"
    launcher = installation / "tools/bin/xrun"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("launcher\n", encoding="utf-8")
    monkeypatch.setenv("CDS_LIC_FILE", "global-license")

    environment = xrun_env(
        launcher,
        {
            "PATH": "/snapshot/bin",
            "CDS_LIC_FILE": "snapshot-license",
            "XCELIUM_HOME": str(installation),
        },
    )

    assert environment["CDS_LIC_FILE"] == "snapshot-license"
    assert environment["PATH"].endswith(":/snapshot/bin")


def test_owned_scratch_removes_links_without_following_them(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_text("preserved\n", encoding="utf-8")

    with owned_scratch_directory(prefix="sigilicon-test-") as scratch:
        root = scratch.path
        (root / "cache").mkdir()
        (root / "cache/link").symlink_to(outside)

    assert not root.exists()
    assert outside.read_text(encoding="utf-8") == "preserved\n"


def test_owned_scratch_is_retained_when_process_cleanup_is_uncertain() -> None:
    with pytest.raises(ProcessGroupCleanupUncertainError):
        with owned_scratch_directory(prefix="sigilicon-uncertain-") as scratch:
            root = scratch.path
            (root / "live-tool-state").write_text("unknown\n", encoding="utf-8")
            raise ProcessGroupCleanupUncertainError("process state unknown")

    assert (root / "live-tool-state").is_file()
    (root / "live-tool-state").unlink()
    root.rmdir()


def test_owned_scratch_is_removed_after_an_ordinary_failure() -> None:
    with pytest.raises(RuntimeError, match="tool failed"):
        with owned_scratch_directory(prefix="sigilicon-failed-") as scratch:
            root = scratch.path
            (root / "tool.log").write_text("failed\n", encoding="utf-8")
            raise RuntimeError("tool failed")

    assert not root.exists()


def test_sealed_child_input_is_immutable_and_exact() -> None:
    with owned_sealed_input(b"canonical\n", name="control.il") as owned:
        assert Path(owned.child_path).read_bytes() == b"canonical\n"
        with pytest.raises(PermissionError):
            os.write(owned.fd, b"mutated")
        owned.require_sealed()
        assert os.lseek(owned.fd, 0, os.SEEK_CUR) == 0
        assert Path(owned.child_path).read_bytes() == b"canonical\n"


def test_nonzero_external_exit_is_reported_without_leaving_a_live_process(
    tmp_path: Path,
) -> None:
    completed = managed_process.run(ProcessRequest(
        argv=("/bin/sh", "-c", "printf failed-output; exit 7"),
        cwd=tmp_path,
        environment={},
        timeout_seconds=5,
    ))

    assert completed.returncode == 7
    assert completed.stdout == "failed-output"
    assert completed.stderr == ""


def test_managed_process_keeps_output_streams_and_environment_separate(
    tmp_path: Path,
) -> None:
    environment = {"SELECTED_VALUE": "explicit"}
    request = ProcessRequest(
        argv=(
            "/bin/sh",
            "-c",
            "printf %s \"$SELECTED_VALUE\"; printf diagnostic >&2",
        ),
        cwd=tmp_path,
        environment=environment,
        timeout_seconds=5,
    )
    environment["SELECTED_VALUE"] = "changed-after-request"

    completed = managed_process.run(request)

    assert completed.returncode == 0
    assert completed.stdout == "explicit"
    assert completed.stderr == "diagnostic"
    assert request.environment == {"SELECTED_VALUE": "explicit"}


@pytest.mark.parametrize("timeout", (math.nan, math.inf, -math.inf))
def test_process_boundaries_reject_nonfinite_timeouts(timeout: float) -> None:
    with pytest.raises(ValueError, match="timeout must be positive"):
        ProcessRequest(
            argv=("/bin/true",),
            cwd=Path("/"),
            environment={},
            timeout_seconds=timeout,
        )

    with pytest.raises(ValueError, match="timeout must be positive"):
        run_process_group_until_confirmed(
            ["/bin/true"],
            cwd=Path("/"),
            env={},
            timeout=timeout,
            stdout_fd=1,
            confirmation_probe=lambda: False,
        )


def test_spectre_completion_preserves_all_three_log_sources(tmp_path: Path) -> None:
    run = tmp_path / "run"
    record = StepFiles(
        run_id="spectre-proof",
        root=run,
        input_root=run / "inputs",
        work_root=run / "work",
        output_root=run / "outputs",
        log_root=run / "logs",
        source={},
    )
    model = record.write_text("inputs", ("model.scs",), "// model\n")

    def execute(request: ProcessRequest) -> ProcessResult:
        assert request.before_spawn is not None
        request.before_spawn()
        (request.cwd / "spectre.out").write_text(
            "native log without completion marker\n",
            encoding="utf-8",
        )
        (request.cwd / "result.prn").write_text("time value\n0 1\n", encoding="utf-8")
        return ProcessResult(
            returncode=0,
            stdout="",
            stderr="spectre completes with 0 errors\n",
        )

    result = run_spectre_deck(
        record,
        render_deck=lambda paths: f'include "{paths["model"]}"\n',
        inputs={"model": model},
        output_names=("result.prn",),
        timeout=5,
        spectre=Path("/bin/true"),
        environment={},
        process=SimpleNamespace(run=execute),
    )

    assert result.stderr_log.read_text(encoding="utf-8").endswith("0 errors\n")
    assert result.native_log is not None


def test_confirmed_process_group_can_clean_its_own_lingering_worker(
    tmp_path: Path,
) -> None:
    with (
        owned_directory(tmp_path) as owned_root,
        owned_output_file(owned_root, "worker.stdout") as owned_stdout,
    ):
        result = run_process_group_until_confirmed(
            ["/bin/sh", "-c", "sleep 30"],
            cwd=tmp_path,
            env={},
            timeout=5,
            stdout_fd=owned_stdout.fd,
            confirmation_probe=lambda: True,
            confirmation_grace_seconds=0,
            pass_fds=(owned_stdout.fd,),
        )

    assert result.leader_terminated_after_confirmation
    assert not result.residual_group_cleaned_after_exit
    assert result.completed.returncode is not None


def test_confirmed_cleanup_tracks_descendant_that_creates_new_session(
    tmp_path: Path,
) -> None:
    child_pid_file = tmp_path / "nested-session.pid"
    cleanup_file = tmp_path / "nested-session.cleaned"
    parent_code = """
import os
import pathlib
import signal
import subprocess
import sys

watchdog = '''
import os
import pathlib
import sys
import time

parent = int(sys.argv[1])
while os.getppid() == parent:
    time.sleep(0.05)
pathlib.Path(os.environ["OWNED_CLEANUP_FILE"]).write_text("cleaned")
'''
child = subprocess.Popen(
    [sys.executable, "-c", watchdog, str(os.getpid())],
    start_new_session=True,
)
pathlib.Path(os.environ["OWNED_CHILD_PID_FILE"]).write_text(str(child.pid))
signal.pause()
"""
    with (
        owned_directory(tmp_path) as owned_root,
        owned_output_file(owned_root, "nested.stdout") as owned_stdout,
    ):
        result = run_process_group_until_confirmed(
            [sys.executable, "-c", parent_code],
            cwd=tmp_path,
            env={
                "OWNED_CHILD_PID_FILE": str(child_pid_file),
                "OWNED_CLEANUP_FILE": str(cleanup_file),
            },
            timeout=5,
            stdout_fd=owned_stdout.fd,
            confirmation_probe=child_pid_file.is_file,
            confirmation_grace_seconds=0.5,
            pass_fds=(owned_stdout.fd,),
        )

    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
    assert result.leader_terminated_after_confirmation
    assert cleanup_file.read_text(encoding="utf-8") == "cleaned"
    assert not Path(f"/proc/{child_pid}").exists()


def test_unconfirmed_process_group_timeout_remains_an_error(tmp_path: Path) -> None:
    with (
        owned_directory(tmp_path) as owned_root,
        owned_output_file(owned_root, "unconfirmed.stdout") as owned_stdout,
    ):
        with pytest.raises(RuntimeError, match="before confirmed completion"):
            run_process_group_until_confirmed(
                ["/bin/sh", "-c", "sleep 30"],
                cwd=tmp_path,
                env={},
                timeout=0.1,
                stdout_fd=owned_stdout.fd,
                confirmation_probe=lambda: False,
                pass_fds=(owned_stdout.fd,),
            )


def test_timeout_escalates_from_term_to_kill_when_group_does_not_exit(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("sigilicon.external_tools.PROCESS_TERM_GRACE_SECONDS", 0.05)
    monkeypatch.setattr("sigilicon.external_tools.PROCESS_KILL_GRACE_SECONDS", 1)

    with pytest.raises(RuntimeError, match="timed out"):
        managed_process.run(ProcessRequest(
            argv=("/bin/sh", "-c", "trap '' TERM; while :; do :; done"),
            cwd=tmp_path,
            environment={},
            timeout_seconds=0.05,
        ))


def test_owned_input_detects_a_path_swap_even_when_original_is_restored(
    tmp_path: Path,
) -> None:
    source = tmp_path / "owned-input.sv"
    original = tmp_path / "original.sv"
    replacement = tmp_path / "replacement.sv"
    source.write_text("original", encoding="utf-8")
    replacement.write_text("replacement", encoding="utf-8")

    with pytest.raises(RuntimeError, match="pathname changed during invocation"):
        with owned_input_file(source):
            source.rename(original)
            replacement.rename(source)
            source.unlink()
            original.rename(source)


def test_owned_output_never_follows_a_replaced_directory_path(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    moved = tmp_path / "owned-run"
    run_dir.mkdir()

    with pytest.raises(RuntimeError, match="owned directory identity changed"):
        with owned_directory(run_dir) as owned_run:
            run_dir.rename(moved)
            run_dir.mkdir()
            with owned_output_file(owned_run, "control.il") as output:
                output.write_bytes(b"owned\n")

    assert not (run_dir / "control.il").exists()
    assert (moved / "control.il").read_bytes() == b"owned\n"


def test_owned_atomic_output_accepts_a_regular_rename_commit(tmp_path: Path) -> None:
    with owned_directory(tmp_path) as owned_root:
        with owned_atomic_output_file(owned_root, "detail.csv") as output:
            temporary = tmp_path / "detail.csv.tmp"
            temporary.write_bytes(b"canonical result\n")
            os.replace(temporary, tmp_path / "detail.csv")

            assert output.read_bytes() == b"canonical result\n"


def test_owned_atomic_output_rejects_a_symlink_result(tmp_path: Path) -> None:
    outside = tmp_path / "outside.csv"
    outside.write_bytes(b"not owned\n")
    with owned_directory(tmp_path) as owned_root:
        with owned_atomic_output_file(owned_root, "detail.csv") as output:
            (tmp_path / "detail.csv").symlink_to(outside)

            with pytest.raises(RuntimeError, match="readable regular file"):
                output.read_bytes()


def test_normal_leader_exit_succeeds_only_after_residual_descendants_are_cleaned(
    tmp_path: Path,
) -> None:
    child_pid_file = tmp_path / "residual.pid"
    completed = managed_process.run(ProcessRequest(
        argv=(
            "/bin/sh",
            "-c",
            f"sleep 30 & printf %s $! > {child_pid_file}",
        ),
        cwd=tmp_path,
        environment={},
        timeout_seconds=5,
    ))

    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
    assert completed.returncode == 0
    assert not Path(f"/proc/{child_pid}").exists()
