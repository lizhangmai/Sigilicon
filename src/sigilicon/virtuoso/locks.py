"""Conservative OpenAccess lock detection and non-destructive evidence capture."""

from __future__ import annotations

import os
import re
import socket
import stat
import time
import fcntl
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Iterator


@dataclass(frozen=True)
class OaLock:
    path: Path
    host: str | None
    pid: int | None
    device: int
    inode: int
    size: int
    mtime_ns: int


def _open_nofollow_directory(path: Path, *, create_missing: bool) -> int:
    """Open an absolute directory chain without following symlink components."""

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


def _read_lock_snapshot(path: Path) -> tuple[str, os.stat_result]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        if path.is_symlink():
            raise RuntimeError(f"refusing symbolic-link OA lock: {path}") from exc
        raise
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise RuntimeError(f"refusing non-regular OA lock: {path}")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        visible = path.stat(follow_symlinks=False)
    finally:
        os.close(descriptor)
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if identity_after != identity_before or (
        visible.st_dev,
        visible.st_ino,
    ) != (before.st_dev, before.st_ino):
        raise RuntimeError(f"OA lock changed while it was being inspected: {path}")
    return b"".join(chunks).decode("utf-8", errors="replace"), after


def _matches_snapshot(lock: OaLock) -> bool:
    try:
        _, current = _read_lock_snapshot(lock.path)
    except (FileNotFoundError, RuntimeError):
        return False
    return (
        current.st_dev,
        current.st_ino,
        current.st_size,
        current.st_mtime_ns,
    ) == (lock.device, lock.inode, lock.size, lock.mtime_ns)


def discover_oa_locks(view_dir: Path) -> tuple[OaLock, ...]:
    """Discover OA and non-OA Cadence edit-lock stakes in one view."""

    locks: list[OaLock] = []
    seen: set[Path] = set()
    for path in sorted(view_dir.glob("*.cdslck*")):
        absolute = path.absolute()
        if absolute in seen:
            continue
        seen.add(absolute)
        text, metadata = _read_lock_snapshot(path)
        host_match = re.search(r"^HostName\s+(\S+)", text, re.MULTILINE)
        pid_match = re.search(r"^ProcessIdentifier\s+(\d+)", text, re.MULTILINE)
        locks.append(
            OaLock(
                path=path,
                host=host_match.group(1) if host_match else None,
                pid=int(pid_match.group(1)) if pid_match else None,
                device=metadata.st_dev,
                inode=metadata.st_ino,
                size=metadata.st_size,
                mtime_ns=metadata.st_mtime_ns,
            )
        )
    return tuple(locks)


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def inspect_flow_operation_lock(virtuoso_root: Path) -> dict[str, object]:
    """Inspect the persistent marker without treating its pathname as a lease.

    ``exclusive_flow_operation`` intentionally leaves the last-operation
    marker in place after releasing its advisory flock.  A doctor therefore
    needs to inspect the kernel's flock table, not merely check whether the
    marker exists.  Reading ``/proc/locks`` does not acquire or release the
    lock and fails closed if the marker is replaced, linked, or otherwise
    unsafe to inspect.
    """

    path = virtuoso_root / ".flow-operation.lock"
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except FileNotFoundError:
        return {
            "path": str(path),
            "exists": False,
            "held": False,
            "inspected_without_write": True,
        }
    try:
        before = os.fstat(descriptor)
        visible = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise RuntimeError("flow operation lock is not a regular file")
        if before.st_nlink != 1:
            raise RuntimeError("flow operation lock must have one link")
        if (before.st_dev, before.st_ino) != (visible.st_dev, visible.st_ino):
            raise RuntimeError("flow operation lock identity changed while inspecting")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        visible_after = path.stat(follow_symlinks=False)
        if (
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            or (visible_after.st_dev, visible_after.st_ino)
            != (before.st_dev, before.st_ino)
        ):
            raise RuntimeError("flow operation lock changed while inspecting")
        text = b"".join(chunks).decode("utf-8", errors="replace").strip()
        pid_match = re.search(r"(?:^|\s)pid=(\d+)(?:\s|$)", text)
        host_match = re.search(r"(?:^|\s)host=(\S+)(?:\s|$)", text)
        operation_match = re.search(r"(?:^|\s)operation=(.+)$", text)
        pid = int(pid_match.group(1)) if pid_match else None
        device_token = f"{os.major(before.st_dev):02x}:{os.minor(before.st_dev):02x}"
        lock_identity = f"{device_token}:{before.st_ino}"
        lock_pids: list[int] = []
        try:
            lock_lines = Path("/proc/locks").read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise RuntimeError("cannot inspect kernel flock table /proc/locks") from exc
        for line in lock_lines:
            fields = line.split()
            if len(fields) < 6 or fields[1] != "FLOCK":
                continue
            if fields[5] != lock_identity:
                continue
            try:
                lock_pids.append(int(fields[4]))
            except ValueError:
                continue
        held = bool(lock_pids)
        return {
            "path": str(path),
            "exists": True,
            "held": held,
            "owner_pid": pid,
            "owner_alive": _pid_is_alive(pid) if pid is not None else None,
            "owner_host": host_match.group(1) if host_match else None,
            "operation": operation_match.group(1) if operation_match else None,
            "advisory_lock_pids": lock_pids,
            "inspected_without_write": True,
        }
    finally:
        os.close(descriptor)


def require_clean_oa_view(
    view_dir: Path,
    *,
    quarantine_root: Path | None = None,
    allowed_config_owner_pid: int | None = None,
) -> tuple[Path, ...]:
    """Refuse locks, except an in-place config lock owned by this VTS.

    A requested quarantine captures an exact hard-link evidence record but
    deliberately leaves the live pathname untouched and still refuses the
    operation: POSIX has no atomic "rename this path only if it is this inode"
    primitive, so automatic removal cannot be proven race-free.
    """

    locks = discover_oa_locks(view_dir)
    local_names = {socket.gethostname(), socket.getfqdn(), "localhost"}
    if allowed_config_owner_pid is not None:
        locks = tuple(
            lock
            for lock in locks
            if not (
                lock.path.name.startswith("expand.cfg.cdslck")
                and lock.host in local_names
                and lock.pid == allowed_config_owner_pid
            )
        )
    if not locks:
        return ()
    details = ", ".join(
        f"{lock.path.name}(host={lock.host or '?'},pid={lock.pid or '?'})" for lock in locks
    )
    if quarantine_root is None:
        raise RuntimeError(f"OpenAccess lock files exist for {view_dir}: {details}")
    for lock in locks:
        if lock.host not in local_names or lock.pid is None:
            raise RuntimeError(f"cannot prove OA lock is local and stale: {details}")
        if _pid_is_alive(lock.pid):
            raise RuntimeError(f"OA lock owner is still alive: {details}")
    quarantine_fd = _open_nofollow_directory(
        quarantine_root,
        create_missing=True,
    )
    destination_name = (
        f"{view_dir.parent.name}_{view_dir.name}_{time.time_ns()}"
    )
    os.mkdir(destination_name, mode=0o755, dir_fd=quarantine_fd)
    destination = quarantine_root / destination_name
    destination_fd = os.open(
        destination_name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        dir_fd=quarantine_fd,
    )
    captured: list[Path] = []
    try:
        for lock in locks:
            if not _matches_snapshot(lock):
                raise RuntimeError(
                    f"OA lock changed after stale validation; nothing was moved: "
                    f"{lock.path}"
                )
            assert lock.pid is not None
            if _pid_is_alive(lock.pid):
                raise RuntimeError(
                    f"OA lock owner became live during quarantine: {lock.path}"
                )
            target = destination / lock.path.name
            os.link(
                lock.path,
                lock.path.name,
                dst_dir_fd=destination_fd,
                follow_symlinks=False,
            )
            captured_metadata = os.stat(
                lock.path.name,
                dir_fd=destination_fd,
                follow_symlinks=False,
            )
            captured_identity = (
                captured_metadata.st_dev,
                captured_metadata.st_ino,
                captured_metadata.st_size,
                captured_metadata.st_mtime_ns,
            )
            expected_identity = (lock.device, lock.inode, lock.size, lock.mtime_ns)
            if captured_identity != expected_identity:
                os.unlink(lock.path.name, dir_fd=destination_fd)
                raise RuntimeError(
                    "OA lock was replaced while evidence was captured; the live "
                    f"path was preserved: {lock.path}"
                )
            captured.append(target)
            if not _matches_snapshot(lock):
                raise RuntimeError(
                    f"OA lock changed after evidence capture; live path was "
                    f"preserved: {lock.path}"
                )
    finally:
        os.close(destination_fd)
        os.close(quarantine_fd)
    evidence = ", ".join(str(path) for path in captured)
    raise RuntimeError(
        "stale OA lock evidence was captured without removing the live pathname "
        f"({evidence}); atomic inode-conditional quarantine is unavailable, so "
        "automation failed closed"
    )


def require_clean_oa_cell(
    cell_dir: Path,
    *,
    quarantine_root: Path | None = None,
    allowed_config_owner_pid: int | None = None,
) -> tuple[Path, ...]:
    """Apply the lock policy to every existing view of one OA cell."""

    moved: list[Path] = []
    if cell_dir.is_symlink():
        raise RuntimeError(f"refusing symbolic-link OA cell path: {cell_dir}")
    if not cell_dir.is_dir():
        return ()
    for view_dir in sorted(cell_dir.iterdir()):
        if view_dir.is_symlink():
            raise RuntimeError(f"refusing symbolic-link OA view path: {view_dir}")
        if not view_dir.is_dir():
            continue
        moved.extend(
            require_clean_oa_view(
                view_dir,
                quarantine_root=quarantine_root,
                allowed_config_owner_pid=allowed_config_owner_pid,
            )
        )
    return tuple(moved)


@contextmanager
def exclusive_flow_operation(virtuoso_root: Path, operation: str) -> Iterator[None]:
    """Serialize automation through one nofollow, single-link regular file."""

    root_fd = os.open(
        virtuoso_root,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    lock_fd: int | None = None
    handle = None
    try:
        root_metadata = os.fstat(root_fd)
        visible_root = virtuoso_root.stat(follow_symlinks=False)
        if not stat.S_ISDIR(root_metadata.st_mode) or (
            root_metadata.st_dev,
            root_metadata.st_ino,
        ) != (visible_root.st_dev, visible_root.st_ino):
            raise RuntimeError("Virtuoso workspace directory identity changed")
        try:
            lock_fd = os.open(
                ".flow-operation.lock",
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | os.O_CLOEXEC
                | os.O_NOFOLLOW,
                0o600,
                dir_fd=root_fd,
            )
        except FileExistsError:
            lock_fd = os.open(
                ".flow-operation.lock",
                os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=root_fd,
            )
        metadata = os.fstat(lock_fd)
        visible = os.stat(
            ".flow-operation.lock",
            dir_fd=root_fd,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("flow operation lock is not a regular file")
        if metadata.st_uid != os.geteuid() or metadata.st_nlink != 1:
            raise RuntimeError(
                "flow operation lock must be owned by this user and have one link"
            )
        if (metadata.st_dev, metadata.st_ino) != (visible.st_dev, visible.st_ino):
            raise RuntimeError("flow operation lock identity changed while opening")
        handle = os.fdopen(lock_fd, "r+", encoding="utf-8")
        lock_fd = None
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.seek(0)
            owner = handle.read().strip() or "unknown owner"
            raise RuntimeError(f"another Virtuoso flow operation is active: {owner}") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(
            f"pid={os.getpid()} host={socket.gethostname()} operation={operation}\n"
        )
        handle.flush()
        yield
    finally:
        if handle is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        elif lock_fd is not None:
            os.close(lock_fd)
        os.close(root_fd)
