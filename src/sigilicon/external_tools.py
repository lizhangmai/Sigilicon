"""Safe, bounded execution helpers for external EDA tools."""

from __future__ import annotations

import os
import select
import ctypes
import fcntl
import json
import math
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterator, Protocol


SYNOPSYS_LICENSE_ENV = "LM_LICENSE_FILE"
CADENCE_VIRTUOSO_TOOL = "cadence.virtuoso"
CADENCE_SPICEIN_TOOL = "cadence.spice-in"
CADENCE_TEXT_IMPORT_TOOL = "cadence.cds-text-to-5x"
CADENCE_SPECTRE_TOOL = "cadence.spectre"
PROCESS_TERM_GRACE_SECONDS = 10
PROCESS_KILL_GRACE_SECONDS = 5
PROCESS_OUTPUT_LIMIT_BYTES = 8 * 1024 * 1024
# Some Python builds omit these Linux memfd constants even though libc and the
# kernel support memfd_create(2).  Use the documented Linux ABI values as the
# fallback so immutable child inputs do not depend on optional Python names.
_F_ADD_SEALS = getattr(fcntl, "F_ADD_SEALS", 1033)
_F_GET_SEALS = getattr(fcntl, "F_GET_SEALS", 1034)
_F_SEAL_SEAL = getattr(fcntl, "F_SEAL_SEAL", 0x0001)
_F_SEAL_SHRINK = getattr(fcntl, "F_SEAL_SHRINK", 0x0002)
_F_SEAL_GROW = getattr(fcntl, "F_SEAL_GROW", 0x0004)
_F_SEAL_WRITE = getattr(fcntl, "F_SEAL_WRITE", 0x0008)
_REQUIRED_MEMFD_SEALS = (
    _F_SEAL_SEAL | _F_SEAL_SHRINK | _F_SEAL_GROW | _F_SEAL_WRITE
)
_IN_ATTRIB = 0x00000004
_IN_CLOSE_WRITE = 0x00000008
_IN_MODIFY = 0x00000002
_IN_MOVED_FROM = 0x00000040
_IN_MOVED_TO = 0x00000080
_IN_CREATE = 0x00000100
_IN_DELETE = 0x00000200
_IN_DELETE_SELF = 0x00000400
_IN_MOVE_SELF = 0x00000800
_IN_UNMOUNT = 0x00002000
_IN_Q_OVERFLOW = 0x00004000
_IN_IGNORED = 0x00008000
_INPUT_WATCH_MASK = (
    _IN_ATTRIB
    | _IN_CLOSE_WRITE
    | _IN_MODIFY
    | _IN_MOVED_FROM
    | _IN_MOVED_TO
    | _IN_CREATE
    | _IN_DELETE
    | _IN_DELETE_SELF
    | _IN_MOVE_SELF
    | _IN_UNMOUNT
)
_INOTIFY_EVENT = struct.Struct("iIII")
_EMPTY_ENVIRONMENT: Mapping[str, str] = MappingProxyType({})
_CADENCE_LOCATION_ENVIRONMENT = frozenset(
    {
        "VB_SPECTRE_BIN",
        "MMSIM",
        "SPECTRE_HOME",
        "XCELIUM_HOME",
        "IUS_HOME",
        "GCC_HOME",
        "CDSHOME",
        "CDSROOT",
        "CDS_INST_DIR",
        "OA_HOME",
    }
)


@dataclass(frozen=True, slots=True)
class ProcessRequest:
    """Complete, explicit input to the package-owned process boundary."""

    argv: tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str]
    timeout_seconds: float
    pass_fds: tuple[int, ...] = ()
    executable: str | None = None
    output_limit_bytes: int = PROCESS_OUTPUT_LIMIT_BYTES
    before_spawn: Callable[[], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        argv = tuple(self.argv)
        if not argv or any(not isinstance(value, str) or not value for value in argv):
            raise ValueError("process argv must contain non-empty strings")
        if not isinstance(self.cwd, Path) or not self.cwd.is_absolute():
            raise ValueError("process cwd must be an absolute path")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("process timeout must be positive")
        environment = dict(self.environment)
        if any(
            not isinstance(key, str)
            or not key
            or not isinstance(value, str)
            for key, value in environment.items()
        ):
            raise ValueError("process environment must map non-empty strings to strings")
        pass_fds = tuple(self.pass_fds)
        if any(type(descriptor) is not int or descriptor < 0 for descriptor in pass_fds):
            raise ValueError("process descriptors must be non-negative integers")
        if len(set(pass_fds)) != len(pass_fds):
            raise ValueError("process descriptors must be unique")
        if self.executable is not None and (
            not isinstance(self.executable, str)
            or not self.executable
            or not Path(self.executable).is_absolute()
        ):
            raise ValueError("process executable must be an absolute path")
        if self.before_spawn is not None and not callable(self.before_spawn):
            raise ValueError("process before_spawn hook must be callable")
        if (
            type(self.output_limit_bytes) is not int
            or self.output_limit_bytes <= 0
        ):
            raise ValueError("process output limit must be a positive integer")
        object.__setattr__(self, "argv", argv)
        object.__setattr__(self, "environment", MappingProxyType(environment))
        object.__setattr__(self, "pass_fds", pass_fds)


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Captured result of one fully cleaned process-tree execution."""

    returncode: int
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False


class ProcessPort(Protocol):
    """Replaceable execution seam for package workflows and tests."""

    def run(self, request: ProcessRequest) -> ProcessResult: ...


def owned_process_fd_path(descriptor: int) -> str:
    """Address a held fd through this still-live owner process for descendants."""

    if descriptor < 0:
        raise ValueError("owned descriptor must be non-negative")
    os.fstat(descriptor)
    return f"/proc/{os.getpid()}/fd/{descriptor}"
class ProcessGroupCleanupUncertainError(RuntimeError):
    """Exact process-group cleanup could not be proven."""


def process_group_cleanup_uncertainty(error: BaseException) -> str | None:
    """Find cleanup uncertainty even when a context-exit audit wrapped it."""

    pending: list[BaseException] = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ProcessGroupCleanupUncertainError):
            return str(current)
        for linked in (current.__cause__, current.__context__):
            if linked is not None:
                pending.append(linked)
    return None


def _leader_exited_unreaped(process: subprocess.Popen[str]) -> bool:
    """Observe the helper exit while keeping its PID reserved against reuse."""

    if process.returncode is not None:
        raise ProcessGroupCleanupUncertainError(
            f"dedicated supervisor {process.pid} was reaped before ownership audit"
        )
    try:
        info = os.waitid(
            os.P_PID,
            process.pid,
            os.WEXITED | os.WNOHANG | os.WNOWAIT,
        )
    except ChildProcessError as exc:
        raise ProcessGroupCleanupUncertainError(
            f"dedicated supervisor {process.pid} is no longer an unreaped child"
        ) from exc
    return info is not None


@dataclass(frozen=True)
class ConfirmedProcessGroupResult:
    """A process result plus proof that only a confirmed group needed cleanup."""

    completed: subprocess.CompletedProcess[str]
    leader_terminated_after_confirmation: bool
    residual_group_cleaned_after_exit: bool


@dataclass(frozen=True)
class _SupervisorStatus:
    actual_returncode: int
    cleanup_requested: bool
    residual_cleanup: bool


@dataclass
class _OwnedProcessSupervisor:
    """One dedicated subreaper process and its private status channel."""

    process: subprocess.Popen[str]
    status_fd: int
    command: tuple[str, ...]
    _status: _SupervisorStatus | None = field(default=None, init=False, repr=False)

    def _read_status(self) -> _SupervisorStatus:
        if self._status is not None:
            return self._status
        chunks: list[bytes] = []
        try:
            while chunk := os.read(self.status_fd, 4096):
                chunks.append(chunk)
        finally:
            os.close(self.status_fd)
            self.status_fd = -1
        try:
            value = json.loads(b"".join(chunks).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProcessGroupCleanupUncertainError(
                "dedicated process supervisor produced no valid cleanup status"
            ) from exc
        if not isinstance(value, dict) or not value.get("cleanup_succeeded"):
            raise ProcessGroupCleanupUncertainError(
                "dedicated process supervisor could not prove descendant cleanup: "
                + str(value.get("error", "unknown supervisor error"))
            )
        returncode = value.get("actual_returncode")
        if not isinstance(returncode, int):
            raise ProcessGroupCleanupUncertainError(
                "dedicated process supervisor omitted command return code"
            )
        self._status = _SupervisorStatus(
            actual_returncode=returncode,
            cleanup_requested=bool(value.get("cleanup_requested")),
            residual_cleanup=bool(value.get("residual_cleanup")),
        )
        return self._status

    def finish(self) -> _SupervisorStatus:
        if not _leader_exited_unreaped(self.process):
            raise ProcessGroupCleanupUncertainError(
                f"dedicated supervisor {self.process.pid} has not exited"
            )
        supervisor_returncode = self.process.wait()
        status = self._read_status()
        if supervisor_returncode != 0:
            raise ProcessGroupCleanupUncertainError(
                f"dedicated supervisor exited {supervisor_returncode} despite status"
            )
        return status

    def terminate(self) -> _SupervisorStatus:
        """Ask the exact supervisor to clean its tree; never kill the supervisor."""

        if _leader_exited_unreaped(self.process):
            return self.finish()
        try:
            self.process.terminate()
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + (
            PROCESS_TERM_GRACE_SECONDS + 3 * PROCESS_KILL_GRACE_SECONDS + 5
        )
        while not _leader_exited_unreaped(self.process):
            if time.monotonic() >= deadline:
                raise ProcessGroupCleanupUncertainError(
                    f"dedicated supervisor {self.process.pid} did not finish cleanup"
                )
            time.sleep(0.05)
        status = self.finish()
        return status


@dataclass(frozen=True)
class OwnedFileDescriptor:
    """One nofollow regular file held open for an external invocation."""

    fd: int
    directory_fd: int
    path: Path
    watch_fd: int
    watch_descriptor: int

    @property
    def child_path(self) -> str:
        return owned_process_fd_path(self.fd)

    @property
    def child_named_path(self) -> str:
        """Exact held parent plus the original basename, preserving extension."""

        return f"{owned_process_fd_path(self.directory_fd)}/{self.path.name}"

    def require_visible(self) -> None:
        self._require_unmodified_path()
        held = os.fstat(self.fd)
        visible = os.stat(
            self.path.name,
            dir_fd=self.directory_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(held.st_mode)
            or (visible.st_dev, visible.st_ino) != (held.st_dev, held.st_ino)
        ):
            raise RuntimeError(f"external input pathname changed: {self.path}")

    def _require_unmodified_path(self) -> None:
        """Reject every pathname mutation, including a swap that was restored."""

        while True:
            try:
                payload = os.read(self.watch_fd, 64 * 1024)
            except BlockingIOError:
                return
            if not payload:
                raise RuntimeError(f"external input watch ended: {self.path}")
            offset = 0
            while offset < len(payload):
                if len(payload) - offset < _INOTIFY_EVENT.size:
                    raise RuntimeError(f"invalid external input watch event: {self.path}")
                watch, mask, _cookie, name_length = _INOTIFY_EVENT.unpack_from(
                    payload, offset
                )
                offset += _INOTIFY_EVENT.size
                end = offset + name_length
                if end > len(payload):
                    raise RuntimeError(f"invalid external input watch event: {self.path}")
                name = payload[offset:end].split(b"\0", 1)[0]
                offset = end
                if mask & (_IN_Q_OVERFLOW | _IN_IGNORED | _IN_UNMOUNT):
                    raise RuntimeError(f"external input watch became uncertain: {self.path}")
                if mask & (_IN_DELETE_SELF | _IN_MOVE_SELF):
                    raise RuntimeError(
                        f"external input directory changed during invocation: {self.path}"
                    )
                if watch == self.watch_descriptor and name == os.fsencode(self.path.name):
                    raise RuntimeError(
                        f"external input pathname changed during invocation: {self.path}"
                    )


@dataclass
class OwnedSealedInputDescriptor:
    """Immutable anonymous input with an explicit or object lifetime."""

    fd: int | None

    @property
    def child_path(self) -> str:
        if self.fd is None:
            raise RuntimeError("sealed child input is closed")
        return owned_process_fd_path(self.fd)

    def require_sealed(self) -> None:
        if self.fd is None:
            raise RuntimeError("sealed child input is closed")
        if fcntl.fcntl(self.fd, _F_GET_SEALS) != _REQUIRED_MEMFD_SEALS:
            raise RuntimeError("sealed child input lost its immutable seals")
        os.lseek(self.fd, 0, os.SEEK_SET)

    def close(self) -> None:
        descriptor, self.fd = self.fd, None
        if descriptor is not None:
            os.close(descriptor)

    def __del__(self) -> None:
        try:
            self.close()
        except OSError:
            pass


@dataclass(frozen=True)
class OwnedDirectoryDescriptor:
    """One nofollow directory held open for an external invocation."""

    fd: int
    path: Path

    @property
    def child_path(self) -> str:
        return owned_process_fd_path(self.fd)

    def child_file(self, name: str) -> str:
        if not name or Path(name).name != name:
            raise ValueError(f"owned directory child must be one filename: {name!r}")
        return f"{self.child_path}/{name}"

    def require_visible(self) -> None:
        metadata = os.fstat(self.fd)
        visible = os.stat(self.path, follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or (visible.st_dev, visible.st_ino)
            != (metadata.st_dev, metadata.st_ino)
        ):
            raise RuntimeError(f"owned directory identity changed: {self.path}")

    def read_child_bytes(
        self,
        relative: Path | str,
        *,
        missing_ok: bool = False,
    ) -> bytes | None:
        """Capture one stable regular child through the held directory."""

        value = Path(relative)
        if (
            value.is_absolute()
            or not value.parts
            or "\\" in str(relative)
            or any(part in {"", ".", ".."} for part in value.parts)
        ):
            raise ValueError(f"owned directory child must be relative: {relative!r}")
        current = self.fd
        held_directories: list[tuple[int, str, int, os.stat_result]] = []
        descriptor: int | None = None
        try:
            try:
                for component in value.parts[:-1]:
                    visible = os.stat(
                        component,
                        dir_fd=current,
                        follow_symlinks=False,
                    )
                    child = os.open(
                        component,
                        os.O_RDONLY
                        | os.O_DIRECTORY
                        | os.O_CLOEXEC
                        | os.O_NOFOLLOW,
                        dir_fd=current,
                    )
                    held = os.fstat(child)
                    if (
                        not stat.S_ISDIR(held.st_mode)
                        or (visible.st_dev, visible.st_ino)
                        != (held.st_dev, held.st_ino)
                    ):
                        os.close(child)
                        raise RuntimeError(
                            f"owned directory child changed: {self.path / value}"
                        )
                    held_directories.append((current, component, child, held))
                    current = child
                descriptor = os.open(
                    value.name,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=current,
                )
            except FileNotFoundError:
                if missing_ok:
                    return None
                raise RuntimeError(
                    f"owned directory child is missing: {self.path / value}"
                ) from None
            before = os.fstat(descriptor)
            visible = os.stat(
                value.name,
                dir_fd=current,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or (visible.st_dev, visible.st_ino)
                != (before.st_dev, before.st_ino)
            ):
                raise RuntimeError(
                    f"owned directory child is not a stable regular file: "
                    f"{self.path / value}"
                )
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 1024 * 1024):
                chunks.append(chunk)
            after = os.fstat(descriptor)
            visible_after = os.stat(
                value.name,
                dir_fd=current,
                follow_symlinks=False,
            )
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_nlink,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_nlink,
            ) or (visible_after.st_dev, visible_after.st_ino) != (
                after.st_dev,
                after.st_ino,
            ):
                raise RuntimeError(
                    f"owned directory child changed while reading: {self.path / value}"
                )
            return b"".join(chunks)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            for parent, name, child, held in reversed(held_directories):
                try:
                    visible = os.stat(
                        name,
                        dir_fd=parent,
                        follow_symlinks=False,
                    )
                    if (visible.st_dev, visible.st_ino) != (
                        held.st_dev,
                        held.st_ino,
                    ):
                        raise RuntimeError(
                            f"owned directory child changed: {self.path / value}"
                        )
                finally:
                    os.close(child)


@dataclass(frozen=True)
class OwnedInputClosureDescriptor:
    """One held root for a mutation-monitored file and directory closure."""

    fd: int
    root: Path

    @property
    def child_path(self) -> str:
        return owned_process_fd_path(self.fd)

    def child(self, relative: Path | str) -> str:
        value = Path(relative)
        if (
            value.is_absolute()
            or not value.parts
            or any(part in {"", ".", ".."} for part in value.parts)
        ):
            raise ValueError(f"input closure child must be relative: {relative!r}")
        return f"{self.child_path}/{value.as_posix()}"

    def require_visible(self) -> None:
        metadata = os.fstat(self.fd)
        visible = self.root.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or (visible.st_dev, visible.st_ino)
            != (metadata.st_dev, metadata.st_ino)
        ):
            raise RuntimeError(f"owned input root changed: {self.root}")


@dataclass(frozen=True)
class OwnedExecutable:
    """Held executable target and any exact shebang interpreter."""

    command: tuple[str, ...]
    path: Path
    target: OwnedFileDescriptor
    interpreter: OwnedFileDescriptor | None = None
    executable: str | None = None

    def require_visible(self) -> None:
        self.target.require_visible()
        if self.interpreter is not None:
            self.interpreter.require_visible()


@dataclass(frozen=True)
class OwnedOutputDescriptor:
    """One exclusively created output inode owned through child completion."""

    fd: int
    directory_fd: int
    path: Path
    name: str

    @property
    def child_path(self) -> str:
        return owned_process_fd_path(self.fd)

    def require_visible(self) -> None:
        metadata = os.fstat(self.fd)
        visible = os.stat(
            self.name,
            dir_fd=self.directory_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or (visible.st_dev, visible.st_ino)
            != (metadata.st_dev, metadata.st_ino)
        ):
            raise RuntimeError(f"owned output identity changed: {self.path}")

    def write_bytes(self, payload: bytes) -> None:
        os.lseek(self.fd, 0, os.SEEK_SET)
        os.ftruncate(self.fd, 0)
        remaining = memoryview(payload)
        while remaining:
            written = os.write(self.fd, remaining)
            if written <= 0:
                raise RuntimeError(f"could not persist owned output: {self.path}")
            remaining = remaining[written:]
        os.fsync(self.fd)
        self.require_visible()

    def read_bytes(self) -> bytes:
        os.lseek(self.fd, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while chunk := os.read(self.fd, 1024 * 1024):
            chunks.append(chunk)
        self.require_visible()
        return b"".join(chunks)


@dataclass(frozen=True)
class OwnedAtomicOutputDescriptor:
    """A child-created output that may be committed by atomic rename."""

    reservation_fd: int
    directory_fd: int
    path: Path
    name: str

    @property
    def child_path(self) -> str:
        return f"{owned_process_fd_path(self.directory_fd)}/{self.name}"

    def _open_visible(self, *, writable: bool = False) -> int:
        try:
            descriptor = os.open(
                self.name,
                (os.O_RDWR if writable else os.O_RDONLY)
                | os.O_CLOEXEC
                | os.O_NOFOLLOW,
                dir_fd=self.directory_fd,
            )
        except OSError as exc:
            raise RuntimeError(
                f"owned atomic output is not a readable regular file: {self.path}"
            ) from exc
        metadata = os.fstat(descriptor)
        visible = os.stat(
            self.name,
            dir_fd=self.directory_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or (visible.st_dev, visible.st_ino)
            != (metadata.st_dev, metadata.st_ino)
        ):
            os.close(descriptor)
            raise RuntimeError(
                f"owned atomic output is not a stable regular file: {self.path}"
            )
        return descriptor

    def require_reserved(self) -> None:
        """Prove the safe placeholder still owns the pathname before spawn."""

        reserved = os.fstat(self.reservation_fd)
        visible = os.stat(
            self.name,
            dir_fd=self.directory_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(reserved.st_mode)
            or reserved.st_nlink != 1
            or (visible.st_dev, visible.st_ino)
            != (reserved.st_dev, reserved.st_ino)
        ):
            raise RuntimeError(
                f"owned atomic output reservation changed: {self.path}"
            )

    def require_visible(self) -> None:
        descriptor = self._open_visible()
        os.close(descriptor)

    def read_bytes(self) -> bytes:
        descriptor = self._open_visible()
        try:
            before = os.fstat(descriptor)
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 1024 * 1024):
                chunks.append(chunk)
            after = os.fstat(descriptor)
            visible = os.stat(
                self.name,
                dir_fd=self.directory_fd,
                follow_symlinks=False,
            )
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_nlink,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_nlink,
            ) or (visible.st_dev, visible.st_ino) != (
                after.st_dev,
                after.st_ino,
            ):
                raise RuntimeError(
                    f"owned atomic output changed while reading: {self.path}"
                )
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    def write_bytes(self, payload: bytes) -> None:
        """Replace the contents of the exact visible output inode."""

        descriptor = self._open_visible(writable=True)
        try:
            os.ftruncate(descriptor, 0)
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise RuntimeError(
                        f"could not persist owned atomic output: {self.path}"
                    )
                remaining = remaining[written:]
            os.fsync(descriptor)
            metadata = os.fstat(descriptor)
            visible = os.stat(
                self.name,
                dir_fd=self.directory_fd,
                follow_symlinks=False,
            )
            if (visible.st_dev, visible.st_ino) != (
                metadata.st_dev,
                metadata.st_ino,
            ):
                raise RuntimeError(
                    f"owned atomic output changed while writing: {self.path}"
                )
            os.fsync(self.directory_fd)
        finally:
            os.close(descriptor)


def _open_nofollow_directory(path: Path, *, create_missing: bool) -> int:
    absolute = Path(os.path.abspath(path))
    descriptor = os.open(
        "/",
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    try:
        for component in absolute.parts[1:]:
            try:
                next_descriptor = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                if not create_missing:
                    raise
                os.mkdir(component, mode=0o755, dir_fd=descriptor)
                next_descriptor = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _new_input_watch() -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.inotify_init1.argtypes = (ctypes.c_int,)
    libc.inotify_init1.restype = ctypes.c_int
    watch_fd = libc.inotify_init1(os.O_CLOEXEC | os.O_NONBLOCK)
    if watch_fd < 0:
        error = ctypes.get_errno()
        raise RuntimeError(f"cannot create external input watch: {os.strerror(error)}")
    return watch_fd


def _add_input_watch(watch_fd: int, descriptor: int) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.inotify_add_watch.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint32,
    )
    libc.inotify_add_watch.restype = ctypes.c_int
    watch = libc.inotify_add_watch(
        watch_fd,
        os.fsencode(owned_process_fd_path(descriptor)),
        _INPUT_WATCH_MASK,
    )
    if watch < 0:
        error = ctypes.get_errno()
        raise RuntimeError(f"cannot watch external input directory: {os.strerror(error)}")
    return watch


def _watch_owned_directory(descriptor: int) -> tuple[int, int]:
    """Start a nonblocking mutation audit on an already-held exact directory."""

    watch_fd = _new_input_watch()
    try:
        watch = _add_input_watch(watch_fd, descriptor)
    except BaseException:
        os.close(watch_fd)
        raise
    return watch_fd, watch


def _closure_relative(root: Path, path: Path, label: str) -> Path:
    absolute = Path(os.path.abspath(path))
    try:
        relative = absolute.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"{label} is outside its owned root: {absolute}") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise RuntimeError(f"{label} is not a canonical child: {absolute}")
    return relative


def _open_closure_directory(root_fd: int, relative: Path) -> int:
    descriptor = os.dup(root_fd)
    try:
        for component in relative.parts:
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _closure_metadata(root_fd: int, relative: Path) -> os.stat_result:
    parent = _open_closure_directory(root_fd, relative.parent)
    try:
        return os.stat(relative.name, dir_fd=parent, follow_symlinks=False)
    finally:
        os.close(parent)


def _metadata_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_mode,
        value.st_nlink,
    )


@contextmanager
def owned_input_closure(
    root: Path,
    *,
    files: Sequence[Path],
    directories: Sequence[Path] = (),
) -> Iterator[OwnedInputClosureDescriptor]:
    """Hold one root and monitor an exact input closure without per-file FDs."""

    absolute_root = Path(os.path.abspath(root))
    root_fd = _open_nofollow_directory(absolute_root, create_missing=False)
    watch_fd: int | None = None
    try:
        root_metadata = os.fstat(root_fd)
        watch_fd = _new_input_watch()
        file_relatives = tuple(
            dict.fromkeys(
                _closure_relative(absolute_root, Path(path), "external input file")
                for path in files
            )
        )
        directory_relatives = tuple(
            dict.fromkeys(
                _closure_relative(
                    absolute_root,
                    Path(path),
                    "external input directory",
                )
                for path in directories
                if Path(os.path.abspath(path)) != absolute_root
            )
        )
    except BaseException:
        if watch_fd is not None:
            os.close(watch_fd)
        os.close(root_fd)
        raise
    assert watch_fd is not None
    parent_watches: dict[Path, int] = {}
    watched_names: dict[int, set[bytes]] = {}
    closed_watches: set[int] = set()
    captured_files: dict[Path, tuple[int, ...]] = {}
    captured_directories: dict[Path, tuple[int, ...]] = {}

    def watch_directory(relative: Path) -> int:
        if relative in parent_watches:
            return parent_watches[relative]
        descriptor = _open_closure_directory(root_fd, relative)
        try:
            watch = _add_input_watch(watch_fd, descriptor)
        finally:
            os.close(descriptor)
        parent_watches[relative] = watch
        return watch

    try:
        for relative in file_relatives:
            metadata = _closure_metadata(root_fd, relative)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise RuntimeError(
                    "external input is not an owned regular file: "
                    f"{absolute_root / relative}"
                )
            captured_files[relative] = _metadata_identity(metadata)
            watch = watch_directory(relative.parent)
            watched_names.setdefault(watch, set()).add(os.fsencode(relative.name))
        for relative in directory_relatives:
            descriptor = _open_closure_directory(root_fd, relative)
            try:
                metadata = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            captured_directories[relative] = _metadata_identity(metadata)
            closed_watches.add(watch_directory(relative))
        try:
            yield OwnedInputClosureDescriptor(root_fd, absolute_root)
        finally:
            while True:
                try:
                    payload = os.read(watch_fd, 64 * 1024)
                except BlockingIOError:
                    break
                if not payload:
                    raise RuntimeError("external input watch ended")
                offset = 0
                while offset < len(payload):
                    if len(payload) - offset < _INOTIFY_EVENT.size:
                        raise RuntimeError("invalid external input watch event")
                    watch, mask, _cookie, name_length = _INOTIFY_EVENT.unpack_from(
                        payload, offset
                    )
                    offset += _INOTIFY_EVENT.size
                    end = offset + name_length
                    if end > len(payload):
                        raise RuntimeError("invalid external input watch event")
                    name = payload[offset:end].split(b"\0", 1)[0]
                    offset = end
                    if mask & (_IN_Q_OVERFLOW | _IN_IGNORED | _IN_UNMOUNT):
                        raise RuntimeError("external input watch became uncertain")
                    if (
                        mask & (_IN_DELETE_SELF | _IN_MOVE_SELF)
                        or watch in closed_watches
                        or name in watched_names.get(watch, set())
                    ):
                        raise RuntimeError(
                            "external input pathname changed during invocation"
                        )
            for relative, expected in captured_files.items():
                current = _closure_metadata(root_fd, relative)
                if _metadata_identity(current) != expected:
                    raise RuntimeError(
                        "external input identity changed during invocation: "
                        f"{absolute_root / relative}"
                    )
            for relative, expected in captured_directories.items():
                descriptor = _open_closure_directory(root_fd, relative)
                try:
                    current = os.fstat(descriptor)
                finally:
                    os.close(descriptor)
                if _metadata_identity(current) != expected:
                    raise RuntimeError(
                        "external input directory changed during invocation: "
                        f"{absolute_root / relative}"
                    )
            visible_root = absolute_root.stat(follow_symlinks=False)
            if (
                visible_root.st_dev,
                visible_root.st_ino,
                visible_root.st_mode,
            ) != (
                root_metadata.st_dev,
                root_metadata.st_ino,
                root_metadata.st_mode,
            ):
                raise RuntimeError(
                    f"external input root changed during invocation: {absolute_root}"
                )
    finally:
        os.close(watch_fd)
        os.close(root_fd)


@contextmanager
def owned_input_file(
    path: Path,
    *,
    require_single_link: bool = True,
) -> Iterator[OwnedFileDescriptor]:
    """Hold the exact input inode open until an external invocation completes."""

    absolute = Path(os.path.abspath(path))
    parent_fd = _open_nofollow_directory(absolute.parent, create_missing=False)
    descriptor: int | None = None
    watch_fd: int | None = None
    try:
        descriptor = os.open(
            absolute.name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or (
            require_single_link and metadata.st_nlink != 1
        ):
            raise RuntimeError(f"external input is not an owned regular file: {absolute}")
        after = os.fstat(descriptor)
        if (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise RuntimeError(f"external input changed while attesting it: {absolute}")
        visible = os.stat(
            absolute.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (visible.st_dev, visible.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise RuntimeError(f"external input pathname changed: {absolute}")
        os.lseek(descriptor, 0, os.SEEK_SET)
        watch_fd, watch_descriptor = _watch_owned_directory(parent_fd)
        owned = OwnedFileDescriptor(
            descriptor,
            parent_fd,
            absolute,
            watch_fd,
            watch_descriptor,
        )
        try:
            yield owned
        finally:
            owned._require_unmodified_path()
            after_use = os.fstat(descriptor)
            if (
                not stat.S_ISREG(after_use.st_mode)
                or (require_single_link and after_use.st_nlink != 1)
                or (
                    after_use.st_dev,
                    after_use.st_ino,
                    after_use.st_size,
                    after_use.st_mtime_ns,
                    after_use.st_mode,
                )
                != (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    metadata.st_mode,
                )
            ):
                raise RuntimeError(
                    f"external input identity changed during invocation: {absolute}"
                )
            final_visible = os.stat(
                absolute.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if (
                (final_visible.st_dev, final_visible.st_ino)
                != (metadata.st_dev, metadata.st_ino)
            ):
                raise RuntimeError(
                    f"external input changed during invocation: {absolute}"
                )
    finally:
        if watch_fd is not None:
            os.close(watch_fd)
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def retain_sealed_input(
    payload: bytes,
    *,
    name: str,
) -> OwnedSealedInputDescriptor:
    """Create an immutable memfd retained until its owner closes or releases it."""

    if not name or Path(name).name != name:
        raise ValueError(f"sealed input name must be one filename: {name!r}")
    flags = getattr(os, "MFD_CLOEXEC", 0x0001) | getattr(
        os, "MFD_ALLOW_SEALING", 0x0002
    )
    creator = getattr(os, "memfd_create", None)
    if creator is not None:
        descriptor = creator(name, flags)
    else:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.memfd_create.argtypes = (ctypes.c_char_p, ctypes.c_uint)
        libc.memfd_create.restype = ctypes.c_int
        descriptor = libc.memfd_create(name.encode("utf-8"), flags)
        if descriptor < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), name)
    try:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise RuntimeError("could not populate sealed child input")
            remaining = remaining[written:]
        fcntl.fcntl(descriptor, _F_ADD_SEALS, _REQUIRED_MEMFD_SEALS)
        owned = OwnedSealedInputDescriptor(descriptor)
        owned.require_sealed()
        return owned
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def owned_sealed_input(
    payload: bytes,
    *,
    name: str,
) -> Iterator[OwnedSealedInputDescriptor]:
    """Hold a sealed memfd across one exact child invocation."""

    owned = retain_sealed_input(payload, name=name)
    try:
        yield owned
    finally:
        try:
            owned.require_sealed()
        finally:
            owned.close()


@contextmanager
def owned_directory(
    path: Path,
    *,
    create_missing: bool = False,
) -> Iterator[OwnedDirectoryDescriptor]:
    """Hold one exact nofollow directory open across a child lifecycle."""

    absolute = Path(os.path.abspath(path))
    descriptor = _open_nofollow_directory(
        absolute,
        create_missing=create_missing,
    )
    metadata = os.fstat(descriptor)
    try:
        yield OwnedDirectoryDescriptor(descriptor, absolute)
    finally:
        try:
            if absolute != Path("/"):
                parent_fd = _open_nofollow_directory(
                    absolute.parent,
                    create_missing=False,
                )
                try:
                    visible = os.stat(
                        absolute.name,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                    if (
                        not stat.S_ISDIR(visible.st_mode)
                        or (visible.st_dev, visible.st_ino)
                        != (metadata.st_dev, metadata.st_ino)
                    ):
                        raise RuntimeError(
                            f"owned directory identity changed: {absolute}"
                        )
                finally:
                    os.close(parent_fd)
        finally:
            os.close(descriptor)


@contextmanager
def owned_executable(path: Path) -> Iterator[OwnedExecutable]:
    """Hold one exact executable while preserving a configured multicall name."""

    absolute = Path(os.path.abspath(path))
    resolved = absolute.resolve(strict=True)
    with owned_input_file(resolved, require_single_link=False) as target:
        metadata = os.fstat(target.fd)
        if metadata.st_mode & 0o111 == 0:
            raise RuntimeError(f"external executable is not executable: {absolute}")
        header = os.pread(target.fd, 128, 0).splitlines()[0]
        if not header.startswith(b"#!"):
            held = OwnedExecutable(
                (str(absolute),),
                absolute,
                target,
                executable=target.child_path,
            )
            held.require_visible()
            try:
                yield held
            finally:
                held.require_visible()
            return
        try:
            shebang = header[2:].decode("utf-8").strip().split()
        except UnicodeDecodeError as exc:
            raise RuntimeError("external executable has an invalid shebang") from exc
        if not shebang or len(shebang) > 2 or not Path(shebang[0]).is_absolute():
            raise RuntimeError("external executable requires one absolute interpreter")
        if Path(shebang[0]).name == "env":
            raise RuntimeError("external executable cannot select an ambient interpreter")
        interpreter_path = Path(shebang[0]).resolve(strict=True)
        with owned_input_file(
            interpreter_path,
            require_single_link=False,
        ) as interpreter:
            interpreter_name = Path(shebang[0]).name
            if interpreter_name in {"ash", "bash", "dash", "ksh", "sh", "zsh"}:
                command = (
                    interpreter.child_path,
                    *shebang[1:],
                    "-c",
                    'launcher=$1; shift; . "$launcher"',
                    str(absolute),
                    target.child_named_path,
                )
            else:
                command = (
                    interpreter.child_path,
                    *shebang[1:],
                    target.child_named_path,
                )
            held = OwnedExecutable(
                command,
                absolute,
                target,
                interpreter,
            )
            held.require_visible()
            try:
                yield held
            finally:
                held.require_visible()


def _clear_directory_at(descriptor: int) -> None:
    """Remove children of one held directory without following links."""

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    for name in os.listdir(descriptor):
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            child = os.open(name, flags, dir_fd=descriptor)
            try:
                os.fchmod(child, stat.S_IRWXU)
                _clear_directory_at(child)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=descriptor)
        else:
            os.unlink(name, dir_fd=descriptor)


@contextmanager
def owned_scratch_directory(
    *,
    prefix: str,
    retain_on_error: Callable[[BaseException], bool] | None = None,
) -> Iterator[OwnedDirectoryDescriptor]:
    """Create an fd-owned tool scratch tree and remove it only when safe.

    Process cleanup uncertainty retains the scratch outside the managed run so
    a possibly live tool never races artifact finalization or recursive cleanup.
    """

    if not prefix or Path(prefix).name != prefix:
        raise ValueError("scratch prefix must be one non-empty path component")
    root = Path(tempfile.mkdtemp(prefix=prefix)).absolute()
    parent_fd = _open_nofollow_directory(root.parent, create_missing=False)
    expected = os.stat(root.name, dir_fd=parent_fd, follow_symlinks=False)
    retained = False
    cleared = False
    try:
        with owned_directory(root) as directory:
            try:
                yield directory
            except BaseException as exc:
                retained = (
                    retain_on_error(exc)
                    if retain_on_error is not None
                    else process_group_cleanup_uncertainty(exc) is not None
                )
                if not retained:
                    _clear_directory_at(directory.fd)
                    cleared = True
                raise
            else:
                _clear_directory_at(directory.fd)
                cleared = True
    finally:
        try:
            if cleared:
                visible = os.stat(root.name, dir_fd=parent_fd, follow_symlinks=False)
                if (
                    not stat.S_ISDIR(visible.st_mode)
                    or (visible.st_dev, visible.st_ino)
                    != (expected.st_dev, expected.st_ino)
                ):
                    raise RuntimeError(f"owned scratch identity changed: {root}")
                os.rmdir(root.name, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)


@contextmanager
def owned_output_file(
    directory: OwnedDirectoryDescriptor,
    name: str,
    *,
    mode: int = 0o644,
) -> Iterator[OwnedOutputDescriptor]:
    """Exclusively create and hold one exact output inode."""

    if not name or Path(name).name != name:
        raise ValueError(f"owned output must be one filename: {name!r}")
    descriptor = os.open(
        name,
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | os.O_CLOEXEC
        | os.O_NOFOLLOW,
        mode,
        dir_fd=directory.fd,
    )
    output = OwnedOutputDescriptor(
        fd=descriptor,
        directory_fd=directory.fd,
        path=directory.path / name,
        name=name,
    )
    try:
        output.require_visible()
        try:
            yield output
        finally:
            output.require_visible()
    finally:
        os.close(descriptor)


@contextmanager
def owned_atomic_output_file(
    directory: OwnedDirectoryDescriptor,
    name: str,
) -> Iterator[OwnedAtomicOutputDescriptor]:
    """Reserve one safe regular pathname for a trusted atomic writer.

    Cadence exporters commonly write a sibling temporary file and rename it
    over the requested result.  A held placeholder prevents the initial path
    from being a symlink; post-write reads bind whichever regular inode the
    exporter committed through the held parent directory.
    """

    if not name or Path(name).name != name:
        raise ValueError(f"owned atomic output must be one filename: {name!r}")
    reservation = os.open(
        name,
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | os.O_CLOEXEC
        | os.O_NOFOLLOW,
        0o644,
        dir_fd=directory.fd,
    )
    output = OwnedAtomicOutputDescriptor(
        reservation_fd=reservation,
        directory_fd=directory.fd,
        path=directory.path / name,
        name=name,
    )
    try:
        output.require_reserved()
        yield output
    finally:
        os.close(reservation)


def cadence_subprocess_env(
    base: Mapping[str, str],
) -> dict[str, str]:
    """Return a Cadence child environment without external tool selection."""

    env = dict(base)
    env.pop(SYNOPSYS_LICENSE_ENV, None)
    for name in _CADENCE_LOCATION_ENVIRONMENT:
        env.pop(name, None)
    return env


def _xcelium_home(
    xrun: Path,
) -> Path:
    absolute = Path(os.path.abspath(xrun))
    if absolute.name != "xrun" or absolute.parent.name != "bin":
        raise RuntimeError(f"cannot determine Xcelium installation root from {xrun}")
    if absolute.parent.parent.name == "tools":
        return absolute.parents[2]
    if (
        absolute.parent.parent.name == "inca"
        and absolute.parent.parent.parent.name == "tools.lnx86"
    ):
        return absolute.parents[3]
    return absolute.parents[1]


def _prepend_environment_paths(
    env: dict[str, str],
    name: str,
    entries: Sequence[Path],
) -> None:
    existing = env.get(name)
    env[name] = os.pathsep.join(
        (*map(str, entries), *((existing,) if existing else ()))
    )


def xrun_env(
    xrun: Path,
    base: Mapping[str, str] = _EMPTY_ENVIRONMENT,
) -> dict[str, str]:
    """Build the bounded child environment for one resolved Xcelium install."""

    env = cadence_subprocess_env(base)
    _add_xrun_environment(env, xrun)
    return env


def _add_xrun_environment(env: dict[str, str], xrun: Path) -> None:
    installation = _xcelium_home(xrun)
    path_entries = (installation / "tools" / "bin", installation / "bin")
    lib_entries = (
        installation / "tools" / "inca" / "lib",
        installation / "tools" / "tbsc" / "lib",
        installation / "tools" / "vic" / "lib" / "gnu",
        installation / "tools" / "systemc" / "lib",
        installation / "tools" / "systemc" / "lib" / "gnu",
        installation / "tools" / "systemc" / "gcc" / "install" / "lib64",
        installation / "tools" / "lib",
        installation / "lib",
    )
    _prepend_environment_paths(env, "PATH", path_entries)
    _prepend_environment_paths(env, "LD_LIBRARY_PATH", lib_entries)
    env["XCELIUM_HOME"] = str(installation)
    env["IUS_HOME"] = str(installation)
    env["CDS_INST_DIR"] = str(installation)
    env["GCC_HOME"] = str(installation / "tools" / "systemc" / "gcc" / "install")


def _cadence_ic_home(executable: Path) -> Path | None:
    absolute = Path(os.path.abspath(executable))
    if absolute.name not in {"virtuoso", "spiceIn", "cdsTextTo5x", "strmout"}:
        return None
    if absolute.parent.name != "bin":
        return None
    if (
        absolute.parent.parent.name == "dfII"
        and absolute.parent.parent.parent.name in {"tools", "tools.lnx86"}
    ):
        return absolute.parents[3]
    if absolute.parent.parent.name in {"tools", "tools.lnx86"}:
        return absolute.parents[2]
    return absolute.parents[1]


def cadence_ic_env(
    executable: Path,
    base: Mapping[str, str] = _EMPTY_ENVIRONMENT,
    *,
    xrun: Path | None = None,
) -> dict[str, str]:
    """Derive Cadence IC and optional Xcelium homes from configured tools."""

    env = cadence_subprocess_env(base)
    installation = _cadence_ic_home(executable)
    if installation is not None:
        env["CDSHOME"] = str(installation)
        env["CDSROOT"] = str(installation)
        env["CDS_INST_DIR"] = str(installation)
        oa_installations = tuple(
            path for path in sorted(installation.glob("oa_*")) if path.is_dir()
        )
        if len(oa_installations) == 1:
            env["OA_HOME"] = str(oa_installations[0])
        _prepend_environment_paths(
            env,
            "LD_LIBRARY_PATH",
            (installation / "tools.lnx86" / "lib",),
        )
    if xrun is not None:
        _add_xrun_environment(env, xrun)
    return env


def spectre_env(
    spectre: Path,
    base: Mapping[str, str] = _EMPTY_ENVIRONMENT,
) -> dict[str, str]:
    """Build a child environment from one configured Spectre launcher."""

    absolute = Path(os.path.abspath(spectre))
    env = cadence_subprocess_env(base)
    if absolute.name != "spectre" or absolute.parent.name != "bin":
        _prepend_environment_paths(env, "PATH", (absolute.parent,))
        return env
    installation = (
        absolute.parents[2]
        if absolute.parent.parent.name in {"tools", "tools.lnx86"}
        else absolute.parents[1]
    )
    _prepend_environment_paths(
        env,
        "PATH",
        (
            installation / "tools" / "bin",
            installation / "tools.lnx86" / "bin",
            installation / "bin",
        ),
    )
    _prepend_environment_paths(
        env,
        "LD_LIBRARY_PATH",
        (installation / "tools.lnx86" / "lib",),
    )
    env["SPECTRE_HOME"] = str(installation)
    env["MMSIM"] = str(installation)
    return env


def _spawn_process_supervisor(
    command: Sequence[str],
    *,
    executable: str | None,
    cwd: Path,
    env: Mapping[str, str],
    stdout: Any,
    stderr: Any,
    pass_fds: Sequence[int],
) -> _OwnedProcessSupervisor:
    """Launch one invocation-scoped helper; no ownership is inferred globally."""

    status_read = status_write = ready_read = ready_write = -1
    try:
        status_read, status_write = os.pipe2(os.O_CLOEXEC)
        ready_read, ready_write = os.pipe2(os.O_CLOEXEC)
    except BaseException:
        for descriptor in (status_read, status_write, ready_read, ready_write):
            if descriptor >= 0:
                os.close(descriptor)
        raise
    control = {
        "command": list(command),
        "executable": executable,
        "cwd": os.fspath(cwd),
        "environment": dict(env),
        "pass_fds": list(pass_fds),
        "status_fd": status_write,
        "ready_fd": ready_write,
        "term_grace_seconds": PROCESS_TERM_GRACE_SECONDS,
        "nested_term_grace_seconds": PROCESS_KILL_GRACE_SECONDS,
        "kill_grace_seconds": PROCESS_KILL_GRACE_SECONDS,
    }
    process: subprocess.Popen[str] | None = None
    try:
        with owned_sealed_input(
            json.dumps(
                control,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
            name="external-process-supervisor.json",
        ) as owned_control:
            process = subprocess.Popen(
                (
                    sys.executable,
                    str(Path(__file__).with_name("process_supervisor.py")),
                    str(owned_control.fd),
                ),
                cwd=cwd,
                env={"PYTHONNOUSERSITE": "1"},
                text=True,
                encoding="utf-8",
                errors="backslashreplace",
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
                pass_fds=(
                    owned_control.fd,
                    status_write,
                    ready_write,
                    *tuple(pass_fds),
                ),
            )
    except BaseException as exc:
        for descriptor in (status_write, ready_read, ready_write):
            if descriptor >= 0:
                os.close(descriptor)
        if process is None:
            os.close(status_read)
        else:
            supervisor = _OwnedProcessSupervisor(
                process,
                status_read,
                tuple(command),
            )
            try:
                supervisor.terminate()
            except BaseException as cleanup_exc:
                raise ProcessGroupCleanupUncertainError(
                    "supervisor launch failed after spawn and cleanup was uncertain: "
                    f"{cleanup_exc}"
                ) from exc
        raise
    os.close(status_write)
    status_write = -1
    os.close(ready_write)
    ready_write = -1
    assert process is not None
    supervisor = _OwnedProcessSupervisor(process, status_read, tuple(command))
    try:
        readable, _writable, _exceptional = select.select(
            (ready_read,),
            (),
            (),
            10.0,
        )
        ready = os.read(ready_read, 1) if readable else b""
        if ready != b"R":
            raise RuntimeError("dedicated process supervisor did not become ready")
    except BaseException as exc:
        try:
            if _leader_exited_unreaped(process):
                supervisor.finish()
            else:
                supervisor.terminate()
        except BaseException as cleanup_exc:
            raise ProcessGroupCleanupUncertainError(
                f"dedicated supervisor failed before readiness: {cleanup_exc}"
            ) from exc
        raise
    finally:
        os.close(ready_read)
    return supervisor


@dataclass
class _BoundedTextCapture:
    limit: int
    payload: bytearray = field(default_factory=bytearray)
    truncated: bool = False

    def append(self, value: str) -> None:
        encoded = value.encode("utf-8", errors="backslashreplace")
        excess = len(self.payload) + len(encoded) - self.limit
        if excess > 0:
            self.truncated = True
            if excess >= len(self.payload):
                self.payload.clear()
                encoded = encoded[-self.limit :]
            else:
                del self.payload[:excess]
        self.payload.extend(encoded)

    @property
    def text(self) -> str:
        body = self.payload.decode("utf-8", errors="replace")
        if not self.truncated:
            return body
        return f"[output truncated; retained last {self.limit} bytes]\n{body}"


def _drain_process_stream(
    stream: Any,
    capture: _BoundedTextCapture,
    errors: list[BaseException],
) -> None:
    try:
        while value := stream.read(64 * 1024):
            capture.append(value)
    except BaseException as exc:
        errors.append(exc)


def _finish_process_drains(
    threads: Sequence[threading.Thread],
    errors: Sequence[BaseException],
) -> None:
    for thread in threads:
        thread.join(PROCESS_KILL_GRACE_SECONDS)
        if thread.is_alive():
            raise ProcessGroupCleanupUncertainError(
                "external process output drain remained active after group cleanup"
            )
    if errors:
        raise errors[0]


def _run_process_group_owned(request: ProcessRequest) -> ProcessResult:
    if request.before_spawn is not None:
        request.before_spawn()
    supervisor = _spawn_process_supervisor(
        request.argv,
        executable=request.executable,
        cwd=request.cwd,
        env=dict(request.environment),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=request.pass_fds,
    )
    process = supervisor.process
    stdout_capture = _BoundedTextCapture(request.output_limit_bytes)
    stderr_capture = _BoundedTextCapture(request.output_limit_bytes)
    drain_errors: list[BaseException] = []
    drain_threads: list[threading.Thread] = []
    status: _SupervisorStatus | None = None
    try:
        for stream, capture in (
            (process.stdout, stdout_capture),
            (process.stderr, stderr_capture),
        ):
            if stream is None:
                continue
            thread = threading.Thread(
                target=_drain_process_stream,
                args=(stream, capture, drain_errors),
                daemon=True,
            )
            thread.start()
            drain_threads.append(thread)
        deadline = time.monotonic() + request.timeout_seconds
        while not _leader_exited_unreaped(process):
            now = time.monotonic()
            if drain_errors:
                raise drain_errors[0]
            if now >= deadline:
                supervisor.terminate()
                _finish_process_drains(drain_threads, drain_errors)
                raise RuntimeError(
                    f"process group timed out after {request.timeout_seconds}s: "
                    f"{request.argv[0]}"
                )
            time.sleep(0.05)
        status = supervisor.finish()
        _finish_process_drains(drain_threads, drain_errors)
    except BaseException as exc:
        if process.returncode is None:
            try:
                supervisor.terminate()
            except BaseException as cleanup_exc:
                raise ProcessGroupCleanupUncertainError(
                    f"external process failed and cleanup could not be proven: "
                    f"{request.argv[0]}: {cleanup_exc}"
                ) from exc
        _finish_process_drains(drain_threads, ())
        raise
    assert status is not None
    return ProcessResult(
        returncode=status.actual_returncode,
        stdout=stdout_capture.text,
        stderr=stderr_capture.text,
        stdout_truncated=stdout_capture.truncated,
        stderr_truncated=stderr_capture.truncated,
    )


class ManagedProcessPort:
    """Linux process-tree implementation used by trusted package code."""

    def run(self, request: ProcessRequest) -> ProcessResult:
        return _run_process_group_owned(request)


managed_process = ManagedProcessPort()


def _run_process_group_until_confirmed_owned(
    command: Sequence[str],
    *,
    executable: str | None = None,
    cwd: Path,
    env: Mapping[str, str],
    timeout: int,
    stdout_fd: int,
    confirmation_probe: Callable[[], bool],
    confirmation_grace_seconds: float = 5.0,
    before_spawn: Callable[[], None] | None = None,
    pass_fds: Sequence[int] = (),
) -> ConfirmedProcessGroupResult:
    """Wait for process exit, cleaning an owned descendant tree after completion.

    Some Cadence headless wrappers keep services and nested-session watchdogs
    alive after the simulator result is complete.  Output is sent to a
    caller-owned descriptor so polling cannot deadlock on a pipe.  A tree is
    treated as successfully confirmed only when the caller's exact-inode probe
    remains true through the grace period; timeout before confirmation remains
    an error.
    """

    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("process group timeout must be positive")
    if (
        isinstance(confirmation_grace_seconds, bool)
        or not isinstance(confirmation_grace_seconds, (int, float))
        or not math.isfinite(confirmation_grace_seconds)
        or confirmation_grace_seconds < 0
    ):
        raise ValueError("confirmation grace must not be negative")
    if stdout_fd < 0:
        raise ValueError("process group stdout descriptor must be valid")
    if before_spawn is not None:
        before_spawn()
    supervisor = _spawn_process_supervisor(
        command,
        executable=executable,
        cwd=cwd,
        env=dict(env),
        stdout=stdout_fd,
        stderr=subprocess.STDOUT,
        pass_fds=pass_fds,
    )
    process = supervisor.process
    deadline = time.monotonic() + timeout
    confirmed_at: float | None = None
    try:
        while True:
            now = time.monotonic()
            confirmed = confirmation_probe()
            if confirmed:
                if confirmed_at is None:
                    confirmed_at = now
            else:
                confirmed_at = None

            if _leader_exited_unreaped(process):
                status = supervisor.finish()
                if status.residual_cleanup and not confirmed:
                    raise RuntimeError(
                        f"{command[0]} leader exited while it still had descendants; "
                        "the dedicated supervisor cleaned them"
                    )
                return ConfirmedProcessGroupResult(
                    completed=subprocess.CompletedProcess(
                        command,
                        status.actual_returncode,
                        "",
                        None,
                    ),
                    leader_terminated_after_confirmation=False,
                    residual_group_cleaned_after_exit=status.residual_cleanup,
                )
            if (
                confirmed_at is not None
                and now - confirmed_at >= confirmation_grace_seconds
            ):
                status = supervisor.terminate()
                return ConfirmedProcessGroupResult(
                    completed=subprocess.CompletedProcess(
                        command,
                        status.actual_returncode,
                        "",
                        None,
                    ),
                    leader_terminated_after_confirmation=status.cleanup_requested,
                    residual_group_cleaned_after_exit=False,
                )
            if now >= deadline:
                supervisor.terminate()
                raise RuntimeError(
                    f"process group timed out after {timeout}s before confirmed "
                    f"completion: {command[0]}"
                )
            time.sleep(min(0.1, max(0.0, deadline - now)))
    except BaseException as exc:
        if process.returncode is None:
            try:
                supervisor.terminate()
            except BaseException as cleanup_exc:
                raise ProcessGroupCleanupUncertainError(
                    f"external process failed and group cleanup could not be proven: "
                    f"{command[0]}: {cleanup_exc}"
                ) from exc
        raise


def run_process_group_until_confirmed(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: int,
    stdout_fd: int,
    confirmation_probe: Callable[[], bool],
    confirmation_grace_seconds: float = 5.0,
    before_spawn: Callable[[], None] | None = None,
    pass_fds: Sequence[int] = (),
) -> ConfirmedProcessGroupResult:
    """Run an exact child-subreaper-owned tree until completion is confirmed."""

    return _run_process_group_until_confirmed_owned(
        command,
        cwd=cwd,
        env=env,
        timeout=timeout,
        stdout_fd=stdout_fd,
        confirmation_probe=confirmation_probe,
        confirmation_grace_seconds=confirmation_grace_seconds,
        before_spawn=before_spawn,
        pass_fds=pass_fds,
    )
