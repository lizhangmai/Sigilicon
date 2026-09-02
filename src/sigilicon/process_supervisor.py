"""Dedicated Linux child-subreaper for one external EDA invocation."""

from __future__ import annotations

import ctypes
import errno
import fcntl
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time
from typing import Any


_PR_SET_CHILD_SUBREAPER = 36
_F_GET_SEALS = getattr(fcntl, "F_GET_SEALS", 1034)
_REQUIRED_CONTROL_SEALS = 0x0001 | 0x0002 | 0x0004 | 0x0008


def _set_subreaper() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise RuntimeError(f"cannot enable child subreaper: {os.strerror(error)}")


def _read_control(descriptor: int) -> dict[str, Any]:
    if descriptor < 0:
        raise RuntimeError("invalid supervisor control descriptor")
    if fcntl.fcntl(descriptor, _F_GET_SEALS) != _REQUIRED_CONTROL_SEALS:
        raise RuntimeError("supervisor control descriptor is not fully sealed")
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 8 * 1024 * 1024:
        raise RuntimeError("invalid supervisor control payload")
    payload = os.pread(descriptor, metadata.st_size, 0)
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("supervisor control must be a JSON object")
    command = value.get("command")
    executable = value.get("executable")
    environment = value.get("environment")
    pass_fds = value.get("pass_fds")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(item, str) and item for item in command)
        or not (
            executable is None
            or isinstance(executable, str) and Path(executable).is_absolute()
        )
        or not isinstance(value.get("cwd"), str)
        or not value["cwd"]
        or not isinstance(environment, dict)
        or not all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in environment.items()
        )
        or not isinstance(pass_fds, list)
        or not all(isinstance(item, int) and item >= 0 for item in pass_fds)
        or not isinstance(value.get("status_fd"), int)
        or value["status_fd"] < 0
        or not isinstance(value.get("ready_fd"), int)
        or value["ready_fd"] < 0
        or not all(
            isinstance(value.get(name), (int, float)) and value[name] >= 0
            for name in (
                "term_grace_seconds",
                "nested_term_grace_seconds",
                "kill_grace_seconds",
            )
        )
    ):
        raise RuntimeError("invalid supervisor control fields")
    return value


def _proc_snapshot() -> dict[int, tuple[int, int, str]]:
    """Return PID -> (PPID, SID, state) for exact cleanup decisions."""

    result: dict[int, tuple[int, int, str]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            value = (entry / "stat").read_text(encoding="utf-8")
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ESRCH}:
                continue
            raise
        close = value.rfind(")")
        fields = value[close + 2 :].split() if close >= 0 else []
        if len(fields) < 4:
            raise RuntimeError(f"cannot audit /proc/{entry.name}/stat")
        result[int(entry.name)] = (int(fields[1]), int(fields[3]), fields[0])
    return result


def _reap_orphan_zombies(actual_pid: int) -> None:
    owner_pid = os.getpid()
    for pid, (parent_pid, _session_id, state) in _proc_snapshot().items():
        if pid == actual_pid or parent_pid != owner_pid or state != "Z":
            continue
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError as exc:
            raise RuntimeError(f"cannot reap owned orphan {pid}") from exc


def _owned_members(actual: subprocess.Popen[Any]) -> tuple[tuple[int, str], ...]:
    actual.poll()
    _reap_orphan_zombies(actual.pid)
    owner_pid = os.getpid()
    snapshot = _proc_snapshot()
    members = {
        pid: state
        for pid, (parent_pid, session_id, state) in snapshot.items()
        if pid != owner_pid
        and (parent_pid == owner_pid or session_id == owner_pid)
    }
    return tuple(sorted(members.items()))


def _signal_primary_process_group(signal_number: int) -> None:
    """Signal the private original process group without enumerating PIDs."""

    # This supervisor remains the live session/process-group leader, so its
    # PGID cannot be reused during cleanup.  SIGTERM is handled locally while
    # the actual command and its original-group descendants receive it.
    try:
        os.killpg(os.getpid(), signal_number)
    except ProcessLookupError:
        pass


def _signal_direct_children(
    actual: subprocess.Popen[Any],
    signal_number: int,
    *,
    primary_session_only: bool = False,
) -> None:
    owner_pid = os.getpid()
    snapshot = _proc_snapshot()
    for pid, (parent_pid, session_id, state) in snapshot.items():
        if (
            parent_pid != owner_pid
            or state == "Z"
            or (primary_session_only and session_id != owner_pid)
        ):
            continue
        # Direct children cannot be reaped or have their PID reused without
        # this supervisor calling waitpid, so this signal has an exact owner.
        try:
            os.kill(pid, signal_number)
        except ProcessLookupError:
            continue
    actual.poll()


def _wait_empty(actual: subprocess.Popen[Any], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    empty_since: float | None = None
    while True:
        members = _owned_members(actual)
        now = time.monotonic()
        if not members:
            if empty_since is None:
                empty_since = now
            if now - empty_since >= 0.1:
                return True
        else:
            empty_since = None
        if now >= deadline:
            return False
        time.sleep(min(0.05, max(0.0, deadline - now)))


def _cleanup(
    actual: subprocess.Popen[Any],
    *,
    term_grace_seconds: float,
    nested_term_grace_seconds: float,
    kill_grace_seconds: float,
) -> tuple[int, bool]:
    """Clean the primary session, then exact direct orphan children."""

    initial_returncode = actual.poll()
    residual = initial_returncode is not None and bool(_owned_members(actual))
    _signal_primary_process_group(signal.SIGTERM)
    if not _wait_empty(actual, term_grace_seconds):
        # A stubborn wrapper can keep its separate-session cleanup watchdogs
        # parented and asleep.  Kill only the exact direct command leader,
        # then repeatedly TERM newly adopted *primary-session* children while
        # leaving escaped watchdog sessions time to observe reparenting and
        # remove their own Xvfb state.
        if actual.poll() is None:
            actual.kill()
        adoption_deadline = time.monotonic() + nested_term_grace_seconds
        while True:
            _signal_direct_children(
                actual,
                signal.SIGTERM,
                primary_session_only=True,
            )
            remaining = max(0.0, adoption_deadline - time.monotonic())
            if _wait_empty(actual, min(0.2, remaining)):
                break
            if time.monotonic() >= adoption_deadline:
                break
        if _owned_members(actual):
            _signal_direct_children(actual, signal.SIGTERM)
        if not _wait_empty(actual, kill_grace_seconds):
            deadline = time.monotonic() + kill_grace_seconds
            while True:
                # Newly killed parents can reveal another generation of
                # daemonized direct children.  Re-snapshot and signal every
                # wave until the dedicated subreaper is stably empty.
                _signal_direct_children(actual, signal.SIGKILL)
                if _wait_empty(actual, min(0.2, kill_grace_seconds)):
                    break
                if time.monotonic() >= deadline:
                    remaining = _owned_members(actual)
                    raise RuntimeError(
                        "supervisor could not clean owned descendants: "
                        + ", ".join(str(pid) for pid, _state in remaining)
                    )
            if _owned_members(actual):
                remaining = _owned_members(actual)
                raise RuntimeError(
                    "supervisor could not clean owned descendants: "
                    + ", ".join(str(pid) for pid, _state in remaining)
                )
    returncode = actual.poll()
    if returncode is None:
        raise RuntimeError("supervisor cleanup left the command leader running")
    return returncode, residual


def _write_status(status_fd: int, value: dict[str, Any]) -> None:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(payload) > 4096:
        raise RuntimeError("supervisor status exceeds atomic pipe payload")
    view = memoryview(payload)
    while view:
        written = os.write(status_fd, view)
        if written <= 0:
            raise RuntimeError("cannot write supervisor status")
        view = view[written:]


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1 or not arguments[0].isdigit():
        print("process supervisor requires one control fd", file=sys.stderr)
        return 125
    control_fd = int(arguments[0])
    status_fd = -1
    ready_fd = -1
    actual: subprocess.Popen[Any] | None = None
    requested_signal: int | None = None
    actual_returncode: int | None = None
    residual_cleanup = False
    cleanup_succeeded = False
    cleanup_attempted = False

    def request_cleanup(signal_number: int, _frame: Any) -> None:
        nonlocal requested_signal
        requested_signal = signal_number

    try:
        control = _read_control(control_fd)
        status_fd = control["status_fd"]
        ready_fd = control["ready_fd"]
        cleanup_options = {
            "term_grace_seconds": float(control["term_grace_seconds"]),
            "nested_term_grace_seconds": float(
                control["nested_term_grace_seconds"]
            ),
            "kill_grace_seconds": float(control["kill_grace_seconds"]),
        }
        _set_subreaper()
        signal.signal(signal.SIGTERM, request_cleanup)
        signal.signal(signal.SIGINT, request_cleanup)
        actual = subprocess.Popen(
            control["command"],
            executable=control["executable"],
            cwd=control["cwd"],
            env=control["environment"],
            pass_fds=tuple(control["pass_fds"]),
        )
        os.write(ready_fd, b"R")
        os.close(ready_fd)
        ready_fd = -1
        while requested_signal is None:
            actual_returncode = actual.poll()
            if actual_returncode is not None:
                break
            time.sleep(0.05)
        cleanup_attempted = True
        actual_returncode, residual_cleanup = _cleanup(
            actual,
            **cleanup_options,
        )
        cleanup_succeeded = True
        _write_status(
            status_fd,
            {
                "actual_returncode": actual_returncode,
                "cleanup_requested": requested_signal is not None,
                "cleanup_succeeded": True,
                "residual_cleanup": residual_cleanup,
            },
        )
        return 0
    except BaseException as exc:
        if actual is not None and not cleanup_attempted:
            cleanup_attempted = True
            try:
                actual_returncode, residual_cleanup = _cleanup(
                    actual,
                    **cleanup_options,
                )
                cleanup_succeeded = True
            except BaseException as cleanup_error:
                print(
                    f"process supervisor cleanup failed: {cleanup_error}",
                    file=sys.stderr,
                )
        if status_fd >= 0:
            try:
                _write_status(
                    status_fd,
                    {
                        "actual_returncode": actual_returncode,
                        "cleanup_requested": requested_signal is not None,
                        "cleanup_succeeded": cleanup_succeeded,
                        "error": f"{type(exc).__name__}: {exc}",
                        "residual_cleanup": residual_cleanup,
                    },
                )
            except BaseException:
                pass
        print(f"process supervisor failed: {exc}", file=sys.stderr)
        return 125
    finally:
        if ready_fd >= 0:
            try:
                os.close(ready_fd)
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise
        if status_fd >= 0:
            try:
                os.close(status_fd)
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise


if __name__ == "__main__":
    raise SystemExit(main())
