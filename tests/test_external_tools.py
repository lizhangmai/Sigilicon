from __future__ import annotations

import subprocess
import importlib
import os
import sys
from pathlib import Path

import pytest

from sigilicon.external_tools import (
    OwnedExternalInvocation,
    ProcessGroupCleanupUncertainError,
    cadence_subprocess_env,
    enforce_process_group_subprocess_run,
    owned_atomic_output_file,
    owned_directory,
    owned_input_file,
    owned_output_file,
    owned_process_fd_path,
    owned_sealed_input,
    passthrough_external_invocation,
    process_group_cleanup_uncertainty,
    run_process_group,
    run_process_group_until_confirmed,
)


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
    completed = run_process_group(
        ["/bin/sh", "-c", "printf failed-output; exit 7"],
        cwd=tmp_path,
        env={},
        timeout=5,
    )

    assert completed.returncode == 7
    assert completed.stdout == "failed-output"


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


def test_stubborn_wrapper_does_not_preempt_its_reparent_cleanup_watchdog(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("sigilicon.external_tools.PROCESS_TERM_GRACE_SECONDS", 0.2)
    monkeypatch.setattr("sigilicon.external_tools.PROCESS_KILL_GRACE_SECONDS", 1)
    ready = tmp_path / "watchdog.ready"
    cleaned = tmp_path / "watchdog.cleaned"
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
    time.sleep(0.02)
time.sleep(0.3)
pathlib.Path(os.environ["WATCHDOG_CLEANED"]).write_text("cleaned")
'''
signal.signal(signal.SIGTERM, signal.SIG_IGN)
subprocess.Popen([sys.executable, "-c", watchdog, str(os.getpid())], start_new_session=True)
pathlib.Path(os.environ["WATCHDOG_READY"]).write_text("ready")
signal.pause()
"""
    with (
        owned_directory(tmp_path) as owned_root,
        owned_output_file(owned_root, "stubborn.stdout") as owned_stdout,
    ):
        result = run_process_group_until_confirmed(
            [sys.executable, "-c", parent_code],
            cwd=tmp_path,
            env={
                "WATCHDOG_READY": str(ready),
                "WATCHDOG_CLEANED": str(cleaned),
            },
            timeout=5,
            stdout_fd=owned_stdout.fd,
            confirmation_probe=ready.is_file,
            confirmation_grace_seconds=0,
            pass_fds=(owned_stdout.fd,),
        )

    assert result.leader_terminated_after_confirmation
    assert cleaned.read_text(encoding="utf-8") == "cleaned"


def test_escaped_daemon_that_ignores_term_reaches_final_kill_before_parent_deadline(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("sigilicon.external_tools.PROCESS_TERM_GRACE_SECONDS", 0.2)
    monkeypatch.setattr("sigilicon.external_tools.PROCESS_KILL_GRACE_SECONDS", 0.5)
    daemon_pid_file = tmp_path / "escaped-stubborn.pid"
    parent_code = """
import os
import pathlib
import subprocess
import sys
import time

daemon = '''
import os
import pathlib
import signal
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
pathlib.Path(os.environ["DAEMON_PID_FILE"]).write_text(str(os.getpid()))
while True:
    time.sleep(1)
'''
subprocess.Popen([sys.executable, "-c", daemon], start_new_session=True)
while not pathlib.Path(os.environ["DAEMON_PID_FILE"]).is_file():
    time.sleep(0.01)
time.sleep(30)
"""
    with (
        owned_directory(tmp_path) as owned_root,
        owned_output_file(owned_root, "escaped-stubborn.stdout") as owned_stdout,
    ):
        result = run_process_group_until_confirmed(
            [sys.executable, "-c", parent_code],
            cwd=tmp_path,
            env={"DAEMON_PID_FILE": str(daemon_pid_file)},
            timeout=5,
            stdout_fd=owned_stdout.fd,
            confirmation_probe=daemon_pid_file.is_file,
            confirmation_grace_seconds=0,
            pass_fds=(owned_stdout.fd,),
        )

    daemon_pid = int(daemon_pid_file.read_text(encoding="utf-8"))
    assert result.leader_terminated_after_confirmation
    assert not Path(f"/proc/{daemon_pid}").exists()


def test_subreaper_captures_double_fork_that_reparents_between_scans(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("sigilicon.external_tools.PROCESS_TERM_GRACE_SECONDS", 0.2)
    monkeypatch.setattr("sigilicon.external_tools.PROCESS_KILL_GRACE_SECONDS", 1)
    daemon_pid_file = tmp_path / "double-fork.pid"
    launcher_code = """
import os
import pathlib
import time

child = os.fork()
if child == 0:
    os.setsid()
    daemon = os.fork()
    if daemon == 0:
        pathlib.Path(os.environ["OWNED_DAEMON_PID_FILE"]).write_text(str(os.getpid()))
        time.sleep(30)
        os._exit(0)
    os._exit(0)
os.waitpid(child, 0)
while not pathlib.Path(os.environ["OWNED_DAEMON_PID_FILE"]).is_file():
    time.sleep(0.01)
time.sleep(0.5)
"""
    with (
        owned_directory(tmp_path) as owned_root,
        owned_output_file(owned_root, "double-fork.stdout") as owned_stdout,
    ):
        result = run_process_group_until_confirmed(
            [sys.executable, "-c", launcher_code],
            cwd=tmp_path,
            env={"OWNED_DAEMON_PID_FILE": str(daemon_pid_file)},
            timeout=5,
            stdout_fd=owned_stdout.fd,
            confirmation_probe=daemon_pid_file.is_file,
            confirmation_grace_seconds=0.1,
            pass_fds=(owned_stdout.fd,),
        )

    daemon_pid = int(daemon_pid_file.read_text(encoding="utf-8"))
    assert result.leader_terminated_after_confirmation
    assert not Path(f"/proc/{daemon_pid}").exists()


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
        run_process_group(
            ["/bin/sh", "-c", "trap '' TERM; while :; do :; done"],
            cwd=tmp_path,
            env={},
            timeout=0.05,
        )


def test_cleanup_uncertainty_survives_context_exit_exception_wrapping() -> None:
    wrapped: RuntimeError | None = None
    try:
        try:
            raise ProcessGroupCleanupUncertainError("owned group unknown")
        finally:
            raise RuntimeError("input identity also changed")
    except RuntimeError as error:
        wrapped = error

    assert wrapped is not None
    assert process_group_cleanup_uncertainty(wrapped) == "owned group unknown"


def test_bridge_subprocess_run_is_group_guarded_and_restored(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from sigilicon.virtuoso.bridge import load_import_netlist_schematic

    import_netlist_schematic = load_import_netlist_schematic()
    module = importlib.import_module(import_netlist_schematic.__module__)
    original = module.subprocess
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        "sigilicon.external_tools.run_process_group_capture",
        lambda command, **kwargs: calls.append((tuple(command), kwargs))
        or subprocess.CompletedProcess(command, 0, "stdout", "stderr"),
    )

    with enforce_process_group_subprocess_run(
        import_netlist_schematic,
        validate_spawn=lambda *_args: None,
        own_invocation=passthrough_external_invocation,
    ):
        assert module.subprocess is not original
        result = module.subprocess.run(
            ["spiceIn", "-param", "spiceIn.il"],
            cwd=tmp_path,
            env={"PATH": "/tools"},
            timeout=17,
            capture_output=True,
            text=True,
        )

    assert module.subprocess is original
    assert result.stdout == "stdout"
    assert result.stderr == "stderr"
    assert calls == [
        (
            ("spiceIn", "-param", "spiceIn.il"),
            {
                "cwd": tmp_path,
                "env": {"PATH": "/tools"},
                "timeout": 17,
                "pass_fds": (),
            },
        )
    ]


def test_explicit_empty_child_environment_stays_empty() -> None:
    assert cadence_subprocess_env({}) == {}


def test_owned_input_detects_in_place_mutation_during_invocation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "owned-input"
    source.write_text("before", encoding="utf-8")

    with pytest.raises(RuntimeError, match="changed during invocation"):
        with owned_input_file(source):
            source.write_text("after!", encoding="utf-8")


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
    completed = run_process_group(
        [
            "/bin/sh",
            "-c",
            f"sleep 30 & printf %s $! > {child_pid_file}",
        ],
        cwd=tmp_path,
        env={},
        timeout=5,
    )

    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
    assert completed.returncode == 0
    assert not Path(f"/proc/{child_pid}").exists()


def test_dependency_guard_fails_when_launch_is_skipped_or_bypassed(monkeypatch) -> None:
    from sigilicon.virtuoso.bridge import load_import_netlist_schematic

    import_netlist_schematic = load_import_netlist_schematic()
    module = importlib.import_module(import_netlist_schematic.__module__)
    with pytest.raises(RuntimeError, match="exactly one"):
        with enforce_process_group_subprocess_run(
            import_netlist_schematic,
            validate_spawn=lambda *_args: None,
            own_invocation=passthrough_external_invocation,
        ):
            pass

    setattr(module, "cached_unguarded_popen", subprocess.Popen)
    try:
        with pytest.raises(RuntimeError, match="unguarded process-launch alias"):
            with enforce_process_group_subprocess_run(
                import_netlist_schematic,
                validate_spawn=lambda *_args: None,
                own_invocation=passthrough_external_invocation,
            ):
                pass
    finally:
        delattr(module, "cached_unguarded_popen")


def test_dependency_guard_runs_pre_spawn_revalidation(monkeypatch, tmp_path: Path) -> None:
    from sigilicon.virtuoso.bridge import load_import_netlist_schematic

    import_netlist_schematic = load_import_netlist_schematic()
    module = importlib.import_module(import_netlist_schematic.__module__)
    events: list[str] = []
    monkeypatch.setattr(
        "sigilicon.external_tools.run_process_group_capture",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )

    with enforce_process_group_subprocess_run(
        import_netlist_schematic,
        validate_spawn=lambda *_args: None,
        own_invocation=passthrough_external_invocation,
        before_spawn=lambda: events.append("revalidated"),
    ):
        module.subprocess.run(
            ["spiceIn"],
            cwd=tmp_path,
            env={},
            timeout=1,
            capture_output=True,
            text=True,
        )

    assert events == ["revalidated"]


def test_dependency_guard_rejects_wrong_launch_before_process_call(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from sigilicon.virtuoso.bridge import load_import_netlist_schematic

    import_netlist_schematic = load_import_netlist_schematic()
    module = importlib.import_module(import_netlist_schematic.__module__)
    launches: list[object] = []
    monkeypatch.setattr(
        "sigilicon.external_tools.run_process_group_capture",
        lambda *args, **kwargs: launches.append((args, kwargs)),
    )

    with pytest.raises(RuntimeError, match="wrong guarded launch"):
        with enforce_process_group_subprocess_run(
            import_netlist_schematic,
            validate_spawn=lambda *_args: (_ for _ in ()).throw(
                RuntimeError("wrong guarded launch")
            ),
            own_invocation=passthrough_external_invocation,
        ):
            module.subprocess.run(
                ["unrelated-tool"],
                cwd=tmp_path,
                env={},
                timeout=1,
                capture_output=True,
                text=True,
            )

    assert launches == []


def test_dependency_guard_holds_owned_fds_through_process_completion(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from contextlib import contextmanager
    import os

    from sigilicon.virtuoso.bridge import load_import_netlist_schematic

    import_netlist_schematic = load_import_netlist_schematic()
    module = importlib.import_module(import_netlist_schematic.__module__)
    source = tmp_path / "source.scs"
    source.write_text("subckt cell a b\nends cell\n", encoding="utf-8")
    observed: list[str] = []

    @contextmanager
    def own(command, cwd):
        descriptor = os.open(source, os.O_RDONLY | os.O_CLOEXEC)
        try:
            yield OwnedExternalInvocation(
                command=(str(command[0]), owned_process_fd_path(descriptor)),
                cwd=cwd,
                pass_fds=(descriptor,),
            )
        finally:
            os.close(descriptor)

    def run(command, **kwargs):
        descriptor = kwargs["pass_fds"][0]
        os.fstat(descriptor)
        observed.append(Path(command[1]).read_text(encoding="utf-8"))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("sigilicon.external_tools.run_process_group_capture", run)
    with enforce_process_group_subprocess_run(
        import_netlist_schematic,
        validate_spawn=lambda *_args: None,
        own_invocation=own,
    ):
        module.subprocess.run(
            ["spiceIn", "ignored"],
            cwd=tmp_path,
            env={},
            timeout=1,
            capture_output=True,
            text=True,
        )

    assert observed == ["subckt cell a b\nends cell\n"]
