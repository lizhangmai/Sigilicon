"""Safe, bounded execution helpers for external EDA tools."""

from __future__ import annotations

import os
import select
import ast
import ctypes
import fcntl
import inspect
import json
import shutil
import signal
import stat
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterator


SYNOPSYS_LICENSE_ENV = "LM_LICENSE_FILE"
PROCESS_TERM_GRACE_SECONDS = 10
PROCESS_KILL_GRACE_SECONDS = 5
_SUBPROCESS_GUARD_LOCK = threading.RLock()

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


def run_readonly_capture(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: int = 30,
) -> bytes:
    """Run one bounded read-only helper and return its stdout bytes."""

    completed = subprocess.run(
        tuple(command),
        cwd=Path(cwd),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_seconds,
    )
    return completed.stdout


def owned_process_fd_path(descriptor: int) -> str:
    """Address a held fd through this still-live owner process for descendants."""

    if descriptor < 0:
        raise ValueError("owned descriptor must be non-negative")
    os.fstat(descriptor)
    return f"/proc/{os.getpid()}/fd/{descriptor}"


def _validate_guarded_dependency_process_shape(
    owner: Callable[..., Any],
    module: ModuleType,
) -> None:
    """Accept only one module-level ``subprocess.run`` launch shape.

    This is deliberately structural rather than version-pinned: tracking the
    bridge main branch is safe only while its external launch remains reachable
    through the module binding replaced by our proxy.
    """

    try:
        tree = ast.parse(inspect.getsource(module), filename=owner.__module__)
    except (OSError, TypeError, SyntaxError) as exc:
        raise RuntimeError(
            "cannot audit dependency process-launch implementation; refusing call"
        ) from exc
    imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
    allowed_imports = {
        "__future__",
        "json",
        "os",
        "re",
        "shlex",
        "shutil",
        "subprocess",
        "tempfile",
        "uuid",
        "dataclasses",
        "pathlib",
        "typing",
        "virtuoso_bridge.models",
        "virtuoso_bridge.virtuoso.ops",
        "virtuoso_bridge.virtuoso.skill_output",
    }
    imported_modules = {
        alias.name
        for node in imports
        if isinstance(node, ast.Import)
        for alias in node.names
    }.union(
        node.module or ""
        for node in imports
        if isinstance(node, ast.ImportFrom)
    )
    unexpected_imports = sorted(imported_modules.difference(allowed_imports))
    if unexpected_imports:
        raise RuntimeError(
            "dependency added unaudited modules reachable from its launch path: "
            + ", ".join(unexpected_imports)
        )
    if any(
        isinstance(node, ast.ImportFrom) and node.module == "subprocess"
        for node in imports
    ):
        raise RuntimeError("dependency imports subprocess callables directly")
    subprocess_imports = [
        alias
        for node in imports
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == "subprocess"
    ]
    if len(subprocess_imports) != 1 or subprocess_imports[0].asname is not None:
        raise RuntimeError(
            "dependency must use exactly one unaliased module-level subprocess import"
        )
    launch_names = {
        "Popen",
        "call",
        "check_call",
        "check_output",
        "getoutput",
        "getstatusoutput",
        "run",
        "system",
    }
    os_launch_prefixes = ("exec", "spawn")
    os_launch_names = {"fork", "forkpty", "popen", "posix_spawn", "system"}
    allowed_run_calls = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if (
            isinstance(function, ast.Name)
            and function.id in {"eval", "exec", "compile", "__import__"}
        ):
            raise RuntimeError(
                f"dependency uses dynamic execution primitive {function.id}"
            )
        if (
            isinstance(function, ast.Name)
            and function.id == "getattr"
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id in {"os", "subprocess"}
        ):
            raise RuntimeError("dependency dynamically resolves a process launcher")
        if isinstance(function, ast.Name) and function.id in launch_names:
            raise RuntimeError(
                f"dependency uses unguarded process launcher {function.id}"
            )
        if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
            if function.value.id == "os" and (
                function.attr in os_launch_names
                or function.attr.startswith(os_launch_prefixes)
            ):
                raise RuntimeError(
                    f"dependency uses unsupported os.{function.attr} launcher"
                )
        if not isinstance(function, ast.Attribute) or function.attr not in launch_names:
            continue
        base = function.value
        if (
            function.attr == "run"
            and isinstance(base, ast.Name)
            and base.id == "subprocess"
        ):
            allowed_run_calls += 1
            continue
        raise RuntimeError(
            f"dependency uses unsupported process launcher attribute {function.attr}"
        )
    if allowed_run_calls != 1:
        raise RuntimeError(
            "dependency process-launch shape changed; expected exactly one subprocess.run"
        )


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


@dataclass(frozen=True)
class OwnedSealedInputDescriptor:
    """Immutable anonymous input inherited by an exact child process."""

    fd: int

    @property
    def child_path(self) -> str:
        return owned_process_fd_path(self.fd)

    def require_sealed(self) -> None:
        if fcntl.fcntl(self.fd, _F_GET_SEALS) != _REQUIRED_MEMFD_SEALS:
            raise RuntimeError("sealed child input lost its immutable seals")
        os.lseek(self.fd, 0, os.SEEK_SET)


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

    directory_fd: int
    path: Path
    name: str

    @property
    def child_path(self) -> str:
        return f"{owned_process_fd_path(self.directory_fd)}/{self.name}"

    def _open_visible(self) -> int:
        try:
            descriptor = os.open(
                self.name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
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

    def require_visible(self) -> None:
        descriptor = self._open_visible()
        os.close(descriptor)

    def read_bytes(self) -> bytes:
        descriptor = self._open_visible()
        try:
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 1024 * 1024):
                chunks.append(chunk)
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
                    f"owned atomic output changed while reading: {self.path}"
                )
            return b"".join(chunks)
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


def _watch_owned_directory(descriptor: int) -> tuple[int, int]:
    """Start a nonblocking mutation audit on an already-held exact directory."""

    libc = ctypes.CDLL(None, use_errno=True)
    libc.inotify_init1.argtypes = (ctypes.c_int,)
    libc.inotify_init1.restype = ctypes.c_int
    libc.inotify_add_watch.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint32,
    )
    libc.inotify_add_watch.restype = ctypes.c_int
    watch_fd = libc.inotify_init1(os.O_CLOEXEC | os.O_NONBLOCK)
    if watch_fd < 0:
        error = ctypes.get_errno()
        raise RuntimeError(f"cannot create external input watch: {os.strerror(error)}")
    watch = libc.inotify_add_watch(
        watch_fd,
        os.fsencode(owned_process_fd_path(descriptor)),
        _INPUT_WATCH_MASK,
    )
    if watch < 0:
        error = ctypes.get_errno()
        os.close(watch_fd)
        raise RuntimeError(f"cannot watch external input directory: {os.strerror(error)}")
    return watch_fd, watch


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


@contextmanager
def owned_sealed_input(
    payload: bytes,
    *,
    name: str,
) -> Iterator[OwnedSealedInputDescriptor]:
    """Create a sealed memfd whose bytes cannot change during child execution."""

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
        try:
            yield owned
        finally:
            owned.require_sealed()
    finally:
        os.close(descriptor)


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
    """Reserve one absent child pathname for a trusted atomic writer.

    Cadence exporters commonly write a sibling temporary file and rename it
    over the requested result.  The held parent directory and post-write
    nofollow open preserve ownership without incorrectly pinning the original
    inode.
    """

    if not name or Path(name).name != name:
        raise ValueError(f"owned atomic output must be one filename: {name!r}")
    try:
        os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise RuntimeError(
            f"owned atomic output path already exists: {directory.path / name}"
        )
    yield OwnedAtomicOutputDescriptor(
        directory_fd=directory.fd,
        path=directory.path / name,
        name=name,
    )


def cadence_subprocess_env(
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a Cadence child environment without conflicting license state."""

    env = dict(os.environ if base is None else base)
    env.pop(SYNOPSYS_LICENSE_ENV, None)
    return env


def find_xrun(explicit: Path | None = None) -> Path:
    """Resolve the Xcelium launcher from an explicit path or installation root."""

    if explicit is not None:
        if explicit.is_file():
            return explicit.resolve()
        raise FileNotFoundError(f"xrun does not exist: {explicit}")
    discovered = shutil.which("xrun")
    if discovered:
        return Path(discovered).resolve()
    for variable in ("XCELIUM_HOME", "IUS_HOME"):
        value = os.environ.get(variable)
        if not value:
            continue
        installation = Path(value)
        for candidate in (
            installation / "tools" / "bin" / "xrun",
            installation / "bin" / "xrun",
        ):
            if candidate.is_file():
                return candidate.resolve()
    raise FileNotFoundError(
        "xrun was not found; load Xcelium, set XCELIUM_HOME, or pass --xrun"
    )


def _xcelium_home(xrun: Path) -> Path:
    resolved = xrun.resolve()
    configured = os.environ.get("XCELIUM_HOME") or os.environ.get("IUS_HOME")
    candidates = ([Path(configured)] if configured else []) + list(resolved.parents)
    for installation in candidates:
        for launcher in (
            installation / "tools" / "bin" / "xrun",
            installation / "bin" / "xrun",
        ):
            if launcher.is_file() and launcher.resolve() == resolved:
                return installation
    raise RuntimeError(f"cannot determine Xcelium installation root from {xrun}")


def xrun_env(xrun: Path) -> dict[str, str]:
    """Build the bounded child environment for one resolved Xcelium install."""

    env = cadence_subprocess_env()
    installation = _xcelium_home(xrun)
    path_entries = (installation / "tools" / "bin", installation / "bin")
    lib_entries = (
        installation / "tools" / "inca" / "lib",
        installation / "tools" / "tbsc" / "lib",
        installation / "tools" / "vic" / "lib" / "gnu",
        installation / "tools" / "systemc" / "lib",
        installation / "tools" / "lib",
        installation / "lib",
    )
    env["PATH"] = (
        os.pathsep.join(map(str, path_entries))
        + os.pathsep
        + env.get("PATH", "")
    )
    env["LD_LIBRARY_PATH"] = (
        os.pathsep.join(map(str, lib_entries))
        + os.pathsep
        + env.get("LD_LIBRARY_PATH", "")
    )
    env.setdefault("XCELIUM_HOME", str(installation))
    env.setdefault("IUS_HOME", str(installation))
    env.setdefault("CDS_INST_DIR", str(installation))
    return env


def _spawn_process_supervisor(
    command: Sequence[str],
    *,
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
                    "-m",
                    "sigilicon.process_supervisor",
                    str(owned_control.fd),
                ),
                cwd=cwd,
                env=dict(os.environ),
                text=True,
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


def _drain_process_stream(
    stream: Any,
    chunks: list[str],
    errors: list[BaseException],
) -> None:
    try:
        while value := stream.read(64 * 1024):
            chunks.append(value)
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


def _run_process_group_owned(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: int,
    combine_output: bool,
    before_spawn: Callable[[], None] | None = None,
    pass_fds: Sequence[int] = (),
) -> subprocess.CompletedProcess[str]:
    if before_spawn is not None:
        before_spawn()
    supervisor = _spawn_process_supervisor(
        command,
        cwd=cwd,
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if combine_output else subprocess.PIPE,
        pass_fds=pass_fds,
    )
    process = supervisor.process
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    drain_errors: list[BaseException] = []
    drain_threads: list[threading.Thread] = []
    status: _SupervisorStatus | None = None
    try:
        for stream, chunks in (
            (process.stdout, stdout_chunks),
            (process.stderr, stderr_chunks),
        ):
            if stream is None:
                continue
            thread = threading.Thread(
                target=_drain_process_stream,
                args=(stream, chunks, drain_errors),
                daemon=True,
            )
            thread.start()
            drain_threads.append(thread)
        deadline = time.monotonic() + timeout
        while not _leader_exited_unreaped(process):
            now = time.monotonic()
            if drain_errors:
                raise drain_errors[0]
            if now >= deadline:
                supervisor.terminate()
                _finish_process_drains(drain_threads, drain_errors)
                raise RuntimeError(
                    f"process group timed out after {timeout}s: {command[0]}"
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
                    f"{command[0]}: {cleanup_exc}"
                ) from exc
        _finish_process_drains(drain_threads, ())
        raise
    assert status is not None
    return subprocess.CompletedProcess(
        command,
        status.actual_returncode,
        "".join(stdout_chunks),
        None if combine_output else "".join(stderr_chunks),
    )


def _run_process_group(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: int,
    combine_output: bool,
    before_spawn: Callable[[], None] | None = None,
    pass_fds: Sequence[int] = (),
) -> subprocess.CompletedProcess[str]:
    return _run_process_group_owned(
        command,
        cwd=cwd,
        env=env,
        timeout=timeout,
        combine_output=combine_output,
        before_spawn=before_spawn,
        pass_fds=pass_fds,
    )


def run_process_group(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: int,
    before_spawn: Callable[[], None] | None = None,
    pass_fds: Sequence[int] = (),
) -> subprocess.CompletedProcess[str]:
    """Run a command and terminate its complete process group on timeout."""

    return _run_process_group(
        command,
        cwd=cwd,
        env=env,
        timeout=timeout,
        combine_output=True,
        before_spawn=before_spawn,
        pass_fds=pass_fds,
    )


def _run_process_group_until_confirmed_owned(
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
    """Wait for process exit, cleaning an owned descendant tree after completion.

    Some Cadence headless wrappers keep services and nested-session watchdogs
    alive after the simulator result is complete.  Output is sent to a
    caller-owned descriptor so polling cannot deadlock on a pipe.  A tree is
    treated as successfully confirmed only when the caller's exact-inode probe
    remains true through the grace period; timeout before confirmation remains
    an error.
    """

    if timeout <= 0:
        raise ValueError("process group timeout must be positive")
    if confirmation_grace_seconds < 0:
        raise ValueError("confirmation grace must not be negative")
    if stdout_fd < 0:
        raise ValueError("process group stdout descriptor must be valid")
    if before_spawn is not None:
        before_spawn()
    supervisor = _spawn_process_supervisor(
        command,
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


def run_process_group_capture(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: int,
    before_spawn: Callable[[], None] | None = None,
    pass_fds: Sequence[int] = (),
) -> subprocess.CompletedProcess[str]:
    """Run a child group while preserving separate stdout/stderr streams."""

    return _run_process_group(
        command,
        cwd=cwd,
        env=env,
        timeout=timeout,
        combine_output=False,
        before_spawn=before_spawn,
        pass_fds=pass_fds,
    )


@dataclass(frozen=True)
class OwnedExternalInvocation:
    """Exact command/cwd plus descriptors held through process-group completion."""

    command: tuple[str, ...]
    cwd: Path
    pass_fds: tuple[int, ...] = ()


@contextmanager
def passthrough_external_invocation(
    command: Sequence[str],
    cwd: Path,
) -> Iterator[OwnedExternalInvocation]:
    """Explicit test-only/basic ownership for invocations without file resources."""

    yield OwnedExternalInvocation(tuple(str(item) for item in command), cwd)


class _ProcessGroupSubprocessProxy:
    """Fail-closed ``subprocess.run`` subset for a guarded dependency call."""

    _SAFE_ATTRIBUTES = {
        "PIPE",
        "STDOUT",
        "DEVNULL",
        "CalledProcessError",
        "CompletedProcess",
        "TimeoutExpired",
    }

    def __init__(
        self,
        original: ModuleType,
        *,
        before_spawn: Callable[[], None] | None,
        validate_spawn: Callable[[Sequence[str], Path], None],
        own_invocation: Callable[
            [Sequence[str], Path],
            Any,
        ],
    ) -> None:
        self._original = original
        self._before_spawn = before_spawn
        self._validate_spawn = validate_spawn
        self._own_invocation = own_invocation
        self.run_calls = 0
        self.last_result: subprocess.CompletedProcess[str] | None = None

    def __getattr__(self, name: str) -> Any:
        if name not in self._SAFE_ATTRIBUTES:
            raise RuntimeError(
                f"unsupported subprocess.{name} in process-group guarded dependency"
            )
        return getattr(self._original, name)

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int | None = None,
        capture_output: bool = False,
        text: bool = False,
        check: bool = False,
        input: str | None = None,
        encoding: str | None = None,
        errors: str | None = None,
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        if (
            cwd is None
            or env is None
            or timeout is None
            or not capture_output
            or text is not True
            or input is not None
            or encoding is not None
            or errors is not None
            or kwargs
        ):
            raise RuntimeError(
                "unsupported subprocess.run shape in process-group guarded dependency"
            )
        if self.run_calls:
            raise RuntimeError("guarded dependency attempted multiple process launches")
        resolved_cwd = Path(cwd)
        self._validate_spawn(command, resolved_cwd)
        with self._own_invocation(command, resolved_cwd) as invocation:
            if not isinstance(invocation, OwnedExternalInvocation):
                raise RuntimeError("owned invocation factory returned an invalid value")
            if self._before_spawn is not None:
                self._before_spawn()
            self.run_calls += 1
            result = run_process_group_capture(
                invocation.command,
                cwd=invocation.cwd,
                env=env,
                timeout=timeout,
                pass_fds=invocation.pass_fds,
            )
            self.last_result = result
        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode,
                command,
                output=result.stdout,
                stderr=result.stderr,
            )
        return result


@contextmanager
def enforce_process_group_subprocess_run(
    owner: Callable[..., Any],
    *,
    validate_spawn: Callable[[Sequence[str], Path], None],
    own_invocation: Callable[[Sequence[str], Path], Any],
    before_spawn: Callable[[], None] | None = None,
) -> Iterator[_ProcessGroupSubprocessProxy]:
    """Guard one dependency call whose public API internally uses run().

    The current virtuoso-bridge public netlist importer does not expose an
    external-process runner hook.  Bind a strict module-local proxy for the
    duration of the synchronous call and fail closed if that implementation
    shape changes.  The standard ``subprocess`` module itself is never patched.
    """

    module = sys.modules.get(owner.__module__)
    if module is None:
        raise RuntimeError(f"dependency module is not loaded: {owner.__module__}")
    _validate_guarded_dependency_process_shape(owner, module)
    with _SUBPROCESS_GUARD_LOCK:
        original = getattr(module, "subprocess", None)
        if original is not subprocess:
            raise RuntimeError(
                "dependency subprocess implementation changed; refusing ungrouped tool"
            )
        forbidden_aliases = {
            subprocess.Popen,
            subprocess.call,
            subprocess.check_call,
            subprocess.check_output,
            subprocess.getoutput,
            subprocess.getstatusoutput,
            subprocess.run,
            os.system,
        }
        aliases = [
            name
            for name, value in vars(module).items()
            if any(value is forbidden for forbidden in forbidden_aliases)
        ]
        if aliases:
            raise RuntimeError(
                "dependency caches an unguarded process-launch alias: "
                + ", ".join(sorted(aliases))
            )
        proxy = _ProcessGroupSubprocessProxy(
            subprocess,
            before_spawn=before_spawn,
            validate_spawn=validate_spawn,
            own_invocation=own_invocation,
        )
        setattr(module, "subprocess", proxy)
        changed = False
        try:
            yield proxy
        except BaseException:
            raise
        else:
            if proxy.run_calls != 1:
                raise RuntimeError(
                    "guarded dependency returned without exactly one owned process launch"
                )
        finally:
            changed = getattr(module, "subprocess", None) is not proxy
            setattr(module, "subprocess", original)
            if changed:
                raise RuntimeError(
                    "dependency replaced its subprocess binding during guarded call"
                )
