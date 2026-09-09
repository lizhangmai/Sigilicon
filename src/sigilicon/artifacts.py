"""Lifecycle, state machine, and atomic I/O for persistent artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping, Sequence

from sigilicon.paths import (
    RunPaths,
    operation_incident_reference,
    validate_artifact_component,
    validate_artifact_id,
)


ARTIFACT_STATUSES = frozenset(
    {"running", "succeeded", "failed", "partial", "uncertain", "cancelled"}
)
TERMINAL_STATUSES = ARTIFACT_STATUSES - {"running"}
RUN_ROLES = frozenset({"inputs", "work", "outputs", "logs"})


def is_waveform_path(path: Path) -> bool:
    """Recognize generated waveform databases and sampled signal tables."""
    suffixes = {".vcd", ".vpd", ".fsdb", ".shm", ".wdb", ".wlf", ".raw",
                ".psf", ".psfxl", ".tran", ".tr0", ".ac0", ".sw0", ".saif", ".prn"}
    names = {"psf", "psfbin", "psfascii", "psfxl", "waveforms.csv"}
    for part in path.parts:
        name = part.lower()
        if Path(name).suffix in {".gz", ".bz2", ".xz", ".zst"}:
            name = Path(name).stem
        if name in names or Path(name).suffix in suffixes:
            return True
    return False


class ArtifactManifestError(RuntimeError):
    """The sole supported artifact manifest contract was not met."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _open_nofollow_directory(path: Path, *, create_missing: bool) -> int:
    absolute = Path(os.path.abspath(path))
    descriptor = os.open(
        "/",
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    try:
        for component in absolute.parts[1:]:
            try:
                child = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                if not create_missing:
                    raise
                os.mkdir(component, mode=0o755, dir_fd=descriptor)
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


def _read_nofollow_bytes(path: Path) -> bytes:
    absolute = Path(os.path.abspath(path))
    parent_fd = _open_nofollow_directory(absolute.parent, create_missing=False)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            absolute.name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(f"artifact input is not a regular file: {absolute}")
        visible = os.stat(absolute.name, dir_fd=parent_fd, follow_symlinks=False)
        if (visible.st_dev, visible.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise RuntimeError(f"artifact input pathname changed: {absolute}")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
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
            raise RuntimeError(f"artifact input changed while reading: {absolute}")
        visible_after = os.stat(
            absolute.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (visible_after.st_dev, visible_after.st_ino) != (
            metadata.st_dev,
            metadata.st_ino,
        ):
            raise RuntimeError(f"artifact input pathname changed: {absolute}")
        return b"".join(chunks)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def _inspect_nofollow_file(
    path: Path,
    *,
    require_single_link: bool = True,
) -> tuple[os.stat_result, str]:
    """Hash one held regular inode and prove its visible pathname identity."""

    absolute = Path(os.path.abspath(path))
    parent_fd = _open_nofollow_directory(absolute.parent, create_missing=False)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            absolute.name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        before = os.fstat(descriptor)
        visible = os.stat(absolute.name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISDIR(before.st_mode):
            raise IsADirectoryError(absolute)
        if (
            not stat.S_ISREG(before.st_mode)
            or (require_single_link and before.st_nlink != 1)
            or (visible.st_dev, visible.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise RuntimeError(f"artifact file identity is unsafe: {absolute}")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
        visible_after = os.stat(
            absolute.name,
            dir_fd=parent_fd,
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
            raise RuntimeError(f"artifact file changed while hashing: {absolute}")
        return after, digest.hexdigest()
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def _write_exclusive_bytes(path: Path, payload: bytes) -> None:
    absolute = Path(os.path.abspath(path))
    parent_fd = _open_nofollow_directory(absolute.parent, create_missing=True)
    descriptor: int | None = None
    created = False
    completed = False
    try:
        descriptor = os.open(
            absolute.name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | os.O_NOFOLLOW,
            0o644,
            dir_fd=parent_fd,
        )
        created = True
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise RuntimeError(f"could not write artifact file: {absolute}")
            remaining = remaining[written:]
        os.fsync(descriptor)
        visible = os.stat(absolute.name, dir_fd=parent_fd, follow_symlinks=False)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or (visible.st_dev, visible.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            raise RuntimeError(f"artifact output identity changed: {absolute}")
        os.fsync(parent_fd)
        completed = True
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if created and not completed:
            try:
                os.unlink(absolute.name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    """Durably replace one JSON file through a held nofollow parent dirfd."""

    absolute = Path(os.path.abspath(path))
    if not absolute.name:
        raise ValueError("atomic JSON target must have a filename")
    parent_fd = _open_nofollow_directory(absolute.parent, create_missing=True)
    temporary = f".{absolute.name}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | os.O_NOFOLLOW,
            0o644,
            dir_fd=parent_fd,
        )
        payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise RuntimeError(f"could not persist atomic JSON: {absolute}")
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary,
            absolute.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)


def read_json_object(
    path: Path,
    label: str,
    *,
    sha256: str | None = None,
) -> dict[str, Any]:
    """Decode one stable JSON object, optionally requiring its byte digest."""

    try:
        payload = _read_nofollow_bytes(path)
        if sha256 is not None and hashlib.sha256(payload).hexdigest() != sha256:
            raise ArtifactManifestError(f"{label} digest disagrees with its locator")
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RuntimeError) as exc:
        raise ArtifactManifestError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ArtifactManifestError(f"invalid {label} {path}: expected a JSON object")
    return value


def read_nofollow_text(path: Path, *, errors: str = "strict") -> str:
    return _read_nofollow_bytes(path).decode("utf-8", errors=errors)


def read_nofollow_bytes(path: Path) -> bytes:
    """Read one stable regular file without following any path symlink."""

    return _read_nofollow_bytes(path)


def ensure_nofollow_directory(path: Path) -> Path:
    """Create/open one directory chain while rejecting symlink components."""

    absolute = Path(os.path.abspath(path))
    descriptor = _open_nofollow_directory(absolute, create_missing=True)
    os.close(descriptor)
    return absolute


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _clear_held_directory(descriptor: int) -> None:
    os.fchmod(descriptor, 0o700)
    for name in os.listdir(descriptor):
        visible = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISDIR(visible.st_mode):
            child = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            try:
                held = os.fstat(child)
                if not _same_inode(visible, held):
                    raise RuntimeError("directory changed while removing safe tree")
                _clear_held_directory(child)
                current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if not _same_inode(held, current):
                    raise RuntimeError("directory changed while removing safe tree")
                os.rmdir(name, dir_fd=descriptor)
            finally:
                os.close(child)
        else:
            os.unlink(name, dir_fd=descriptor)


@dataclass(frozen=True)
class SafeFile:
    """One stable, single-link regular file inside a no-follow tree."""

    relative: Path
    path: Path
    mode: int
    size: int
    sha256: str | None


@dataclass(frozen=True)
class SafeTreeInventory:
    files: Mapping[Path, SafeFile]
    directories: Mapping[Path, int]


@dataclass(frozen=True)
class SafeTree:
    """Inspect or remove one exact tree without accepting path indirection."""

    root: Path

    def __post_init__(self) -> None:
        root = Path(os.path.abspath(self.root))
        descriptor = _open_nofollow_directory(root, create_missing=False)
        os.close(descriptor)
        object.__setattr__(self, "root", root)

    def path(self, value: object, label: str = "tree member") -> Path:
        relative = _safe_manifest_relative(value, label)
        return _resolved_artifact_member(self.root, self.root / relative, label)

    def file(self, value: object, label: str = "tree file") -> SafeFile:
        path = self.path(value, label)
        metadata, digest = _inspect_nofollow_file(path)
        return SafeFile(
            path.relative_to(self.root),
            path,
            metadata.st_mode,
            metadata.st_size,
            digest,
        )

    def inventory(self, *, verify_content: bool = True) -> SafeTreeInventory:
        """Return one stable descriptor-relative inventory of the exact tree."""

        files: dict[Path, SafeFile] = {}
        directories: dict[Path, int] = {}
        root_fd = _open_nofollow_directory(self.root, create_missing=False)
        expected_root = os.fstat(root_fd)

        def stable_directory_state(value: os.stat_result) -> tuple[int, ...]:
            return (
                value.st_dev,
                value.st_ino,
                value.st_mode,
                value.st_size,
                value.st_mtime_ns,
            )

        def inspect_file(
            directory_fd: int,
            name: str,
            relative: Path,
        ) -> SafeFile:
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            try:
                before = os.fstat(descriptor)
                visible = os.stat(
                    name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_nlink != 1
                    or not _same_inode(before, visible)
                ):
                    raise RuntimeError(
                        f"safe tree contains an unsafe file: {relative}"
                    )
                digest: str | None = None
                if verify_content:
                    content = hashlib.sha256()
                    while chunk := os.read(descriptor, 1024 * 1024):
                        content.update(chunk)
                    digest = content.hexdigest()
                after = os.fstat(descriptor)
                visible_after = os.stat(
                    name,
                    dir_fd=directory_fd,
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
                ) or not _same_inode(after, visible_after):
                    raise RuntimeError(
                        f"safe tree file changed while inspecting: {relative}"
                    )
                return SafeFile(
                    relative,
                    self.root / relative,
                    after.st_mode,
                    after.st_size,
                    digest,
                )
            finally:
                os.close(descriptor)

        def walk(directory_fd: int, prefix: Path) -> None:
            before = os.fstat(directory_fd)
            names = sorted(os.listdir(directory_fd))
            for name in names:
                relative = prefix / name
                visible = os.stat(
                    name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                if stat.S_ISLNK(visible.st_mode):
                    raise RuntimeError(
                        f"safe tree cannot contain symlinks: {relative}"
                    )
                if stat.S_ISREG(visible.st_mode):
                    files[relative] = inspect_file(directory_fd, name, relative)
                    continue
                if not stat.S_ISDIR(visible.st_mode):
                    raise RuntimeError(
                        f"safe tree contains an unsupported entry: {relative}"
                    )
                child = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
                try:
                    held = os.fstat(child)
                    if not _same_inode(visible, held):
                        raise RuntimeError(
                            f"safe tree directory changed while opening: {relative}"
                        )
                    directories[relative] = held.st_mode
                    walk(child, relative)
                    visible_after = os.stat(
                        name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                    if not _same_inode(held, visible_after):
                        raise RuntimeError(
                            f"safe tree directory changed while inspecting: {relative}"
                        )
                finally:
                    os.close(child)
            after = os.fstat(directory_fd)
            if (
                names != sorted(os.listdir(directory_fd))
                or stable_directory_state(before) != stable_directory_state(after)
            ):
                label = prefix if prefix.parts else Path(".")
                raise RuntimeError(
                    f"safe tree directory changed while inspecting: {label}"
                )

        try:
            walk(root_fd, Path())
            visible_fd = _open_nofollow_directory(self.root, create_missing=False)
            try:
                if not _same_inode(expected_root, os.fstat(visible_fd)):
                    raise RuntimeError(
                        f"safe tree root changed while inspecting: {self.root}"
                    )
            finally:
                os.close(visible_fd)
        finally:
            os.close(root_fd)
        return SafeTreeInventory(
            MappingProxyType(files),
            MappingProxyType(directories),
        )

    def remove(self, expected: os.stat_result) -> None:
        """Remove the expected root through held parent/root descriptors."""

        parent = _open_nofollow_directory(self.root.parent, create_missing=False)
        descriptor: int | None = None
        try:
            descriptor = os.open(
                self.root.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=parent,
            )
            held = os.fstat(descriptor)
            visible = os.stat(
                self.root.name, dir_fd=parent, follow_symlinks=False
            )
            if not _same_inode(expected, held) or not _same_inode(held, visible):
                raise RuntimeError("safe tree root changed while removing")
            _clear_held_directory(descriptor)
            visible = os.stat(
                self.root.name, dir_fd=parent, follow_symlinks=False
            )
            if not _same_inode(held, visible):
                raise RuntimeError("safe tree root changed while removing")
            os.rmdir(self.root.name, dir_fd=parent)
            os.fsync(parent)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent)


def copy_immutable_file(
    source: Path,
    destination: Path,
    *,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
) -> Path:
    """Stream one stable, optionally identity-bound file to a nofollow path."""

    if expected_size is not None and (
        type(expected_size) is not int or expected_size < 0
    ):
        raise ValueError("expected file size must be a non-negative integer")
    if expected_sha256 is not None and (
        not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in expected_sha256
        )
    ):
        raise ValueError("expected file digest must be SHA-256")

    source_path = Path(os.path.abspath(source))
    target = Path(os.path.abspath(destination))
    source_parent = _open_nofollow_directory(
        source_path.parent,
        create_missing=False,
    )
    target_parent = _open_nofollow_directory(
        target.parent,
        create_missing=True,
    )
    source_fd: int | None = None
    target_fd: int | None = None
    created = False
    completed = False
    try:
        source_fd = os.open(
            source_path.name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=source_parent,
        )
        before = os.fstat(source_fd)
        visible = os.stat(
            source_path.name,
            dir_fd=source_parent,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(before.st_mode)
            or not _same_inode(before, visible)
        ):
            raise RuntimeError(
                f"artifact input is not a stable regular file: {source_path}"
            )
        target_fd = os.open(
            target.name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | os.O_NOFOLLOW,
            0o644,
            dir_fd=target_parent,
        )
        created = True
        digest = hashlib.sha256()
        copied_size = 0
        while chunk := os.read(source_fd, 1024 * 1024):
            digest.update(chunk)
            copied_size += len(chunk)
            remaining = memoryview(chunk)
            while remaining:
                written = os.write(target_fd, remaining)
                if written <= 0:
                    raise RuntimeError(f"could not copy artifact file: {target}")
                remaining = remaining[written:]
        after = os.fstat(source_fd)
        visible_after = os.stat(
            source_path.name,
            dir_fd=source_parent,
            follow_symlinks=False,
        )
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or not _same_inode(after, visible_after):
            raise RuntimeError(f"artifact input changed while copying: {source_path}")
        if expected_size is not None and copied_size != expected_size:
            raise RuntimeError(f"artifact input size drifted: {source_path}")
        if expected_sha256 is not None and digest.hexdigest() != expected_sha256:
            raise RuntimeError(f"artifact input content drifted: {source_path}")
        os.fsync(target_fd)
        target_visible = os.stat(
            target.name,
            dir_fd=target_parent,
            follow_symlinks=False,
        )
        target_metadata = os.fstat(target_fd)
        if (
            not stat.S_ISREG(target_metadata.st_mode)
            or target_metadata.st_nlink != 1
            or not _same_inode(target_visible, target_metadata)
        ):
            raise RuntimeError(f"artifact output identity changed: {target}")
        os.fsync(target_parent)
        completed = True
        return target
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if target_fd is not None:
            os.close(target_fd)
        if created and not completed:
            try:
                os.unlink(target.name, dir_fd=target_parent)
            except FileNotFoundError:
                pass
        os.close(source_parent)
        os.close(target_parent)


def write_immutable_text(path: Path, value: str) -> None:
    """Create one nofollow regular text artifact without replacement semantics."""

    if not isinstance(value, str):
        raise ValueError("immutable text artifact must be text")
    _write_exclusive_bytes(Path(path), value.encode("utf-8"))


def write_immutable_bytes(path: Path, value: bytes) -> None:
    """Create one nofollow regular binary artifact without replacement semantics."""

    if not isinstance(value, bytes):
        raise ValueError("immutable binary artifact must be bytes")
    _write_exclusive_bytes(Path(path), value)


def _safe_manifest_relative(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ArtifactManifestError(f"manifest {label} must be a non-empty string")
    relative = Path(value)
    if (
        relative.is_absolute()
        or "\\" in value
        or not relative.parts
        or relative.as_posix() != value
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ArtifactManifestError(f"unsafe manifest {label}: {value!r}")
    return relative


def _resolved_artifact_member(root: Path, member: Path, label: str) -> Path:
    """Validate an artifact member through held nofollow directory descriptors."""

    lexical_root = Path(os.path.abspath(root))
    lexical_member = Path(os.path.abspath(member))
    if not lexical_member.is_relative_to(lexical_root):
        raise RuntimeError(f"artifact {label} is outside its role: {lexical_member}")
    relative = lexical_member.relative_to(lexical_root)
    descriptor = _open_nofollow_directory(lexical_root, create_missing=False)
    try:
        for index, component in enumerate(relative.parts):
            visible = os.stat(
                component,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            current = lexical_root.joinpath(*relative.parts[: index + 1])
            if stat.S_ISLNK(visible.st_mode):
                raise RuntimeError(
                    f"artifact {label} cannot traverse a symlink: {current}"
                )
            if index == len(relative.parts) - 1:
                break
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            held = os.fstat(child)
            if not _same_inode(visible, held):
                os.close(child)
                raise RuntimeError(
                    f"artifact {label} changed while traversing: {current}"
                )
            os.close(descriptor)
            descriptor = child
    finally:
        os.close(descriptor)
    return lexical_member


def validate_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the sole managed run manifest contract."""
    required = {
        "schema",
        "contract_kind",
        "owner",
        "operation",
        "variant",
        "operation_id",
        "adapter",
        "run_id",
        "created_at",
        "completed_at",
        "status",
        "source",
        "files",
        "partial_failure",
        "uncertain_reason",
        "incident_reference",
        "completion_evidence",
        "details",
    }
    if set(value) != required:
        raise ArtifactManifestError("run manifest fields are invalid")
    if value.get("schema") != 4 or value.get("contract_kind") != "run-manifest":
        raise ArtifactManifestError("run manifest header is invalid")
    status = value.get("status")
    if status not in ARTIFACT_STATUSES:
        raise ArtifactManifestError(f"invalid artifact status: {status!r}")
    run_id = value.get("run_id")
    try:
        validate_artifact_id(run_id, "run id")
    except ValueError as exc:
        raise ArtifactManifestError(str(exc)) from exc
    if not isinstance(value.get("files"), dict):
        raise ArtifactManifestError("manifest files must be an object")
    source = value.get("source")
    if not isinstance(source, dict) or not source:
        raise ArtifactManifestError("manifest source must be a non-empty object")
    for field_name in ("owner", "operation", "adapter", "created_at"):
        if not isinstance(value.get(field_name), str) or not value[field_name]:
            raise ArtifactManifestError(f"manifest {field_name} must be a non-empty string")
    for field_name in ("owner", "operation"):
        try:
            validate_artifact_component(value[field_name], field_name)
        except ValueError as exc:
            raise ArtifactManifestError(str(exc)) from exc
    variant = value.get("variant")
    if variant is not None:
        try:
            validate_artifact_component(variant, "variant")
        except ValueError as exc:
            raise ArtifactManifestError(str(exc)) from exc
    try:
        datetime.fromisoformat(value["created_at"])
    except ValueError as exc:
        raise ArtifactManifestError("manifest created_at is not an ISO timestamp") from exc
    operation_id = value.get("operation_id")
    if operation_id is not None:
        try:
            validate_artifact_id(operation_id, "operation id")
        except ValueError as exc:
            raise ArtifactManifestError(str(exc)) from exc
    files = value["files"]
    if set(files) != RUN_ROLES:
        raise ArtifactManifestError(f"run manifest file roles are invalid: {sorted(files)}")
    registered_paths: set[str] = set()
    registered_files: set[str] = set()
    for role, references in files.items():
        if not isinstance(references, list):
            raise ArtifactManifestError(f"manifest file role {role} must be a list")
        for reference in references:
            if not isinstance(reference, dict):
                raise ArtifactManifestError("manifest file reference must be an object")
            relative = reference.get("path")
            path = _safe_manifest_relative(relative, "file reference")
            root_result = role == "outputs" and relative == "result.json"
            if path.parts[0] != role and not root_result:
                raise ArtifactManifestError(f"unsafe manifest file reference: {relative!r}")
            kind = reference.get("kind")
            if root_result and kind != "file":
                raise ArtifactManifestError("run result must be a regular file")
            if kind not in {"file", "directory"}:
                raise ArtifactManifestError("manifest file reference has invalid kind")
            size = reference.get("size")
            if kind == "file" and (
                set(reference).difference({"path", "kind", "size", "sha256", "label"})
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size < 0
                or (
                    "sha256" in reference
                    and (
                        not isinstance(reference["sha256"], str)
                        or len(reference["sha256"]) != 64
                        or any(
                            character not in "0123456789abcdef"
                            for character in reference["sha256"]
                        )
                    )
                )
            ):
                raise ArtifactManifestError("manifest file reference has invalid metadata")
            if kind == "directory" and (
                set(reference).difference({"path", "kind", "label"})
                or "size" in reference
                or "sha256" in reference
            ):
                raise ArtifactManifestError(
                    "manifest directory reference cannot have file metadata"
                )
            if relative in registered_paths:
                raise ArtifactManifestError(
                    f"manifest file reference is duplicated: {relative}"
                )
            registered_paths.add(relative)
            if kind == "file":
                registered_files.add(relative)
    completion_evidence = value.get("completion_evidence")
    if not isinstance(completion_evidence, list) or not all(
        isinstance(path, str) and path in registered_paths for path in completion_evidence
    ):
        raise ArtifactManifestError(
            "manifest completion_evidence must reference registered files"
        )
    if len(completion_evidence) != len(set(completion_evidence)):
        raise ArtifactManifestError("manifest completion_evidence must be unique")
    if any(path not in registered_files for path in completion_evidence):
        raise ArtifactManifestError(
            "manifest completion_evidence must reference regular files"
        )
    if any(path != "result.json" and Path(path).parts[0] != "outputs" for path in completion_evidence):
        raise ArtifactManifestError(
            "manifest completion_evidence for this artifact must use outputs/"
        )
    partial_failure = value.get("partial_failure")
    if partial_failure is not None and not isinstance(partial_failure, dict):
        raise ArtifactManifestError("manifest partial_failure must be an object or null")
    uncertain_reason = value.get("uncertain_reason")
    if uncertain_reason is not None and (
        not isinstance(uncertain_reason, str) or not uncertain_reason
    ):
        raise ArtifactManifestError("manifest uncertain_reason must be a string or null")
    incident = value.get("incident_reference")
    if incident is not None:
        if not isinstance(incident, str) or not incident:
            raise ArtifactManifestError("manifest incident_reference must be a string or null")
        try:
            incident_path = _safe_manifest_relative(incident, "incident reference")
            expected_incident = operation_incident_reference(operation_id)
        except (ArtifactManifestError, ValueError) as exc:
            raise ArtifactManifestError(f"unsafe incident reference: {incident!r}") from exc
        if incident_path != expected_incident:
            raise ArtifactManifestError(f"unsafe incident reference: {incident!r}")
    completed_at = value.get("completed_at")
    if status == "running":
        if completed_at is not None:
            raise ArtifactManifestError("running artifact cannot have completed_at")
        if completion_evidence or partial_failure is not None or uncertain_reason is not None:
            raise ArtifactManifestError("running artifact cannot contain terminal provenance")
    else:
        if not isinstance(completed_at, str) or not completed_at:
            raise ArtifactManifestError("terminal artifact must have completed_at")
        try:
            datetime.fromisoformat(completed_at)
        except ValueError as exc:
            raise ArtifactManifestError(
                "manifest completed_at is not an ISO timestamp"
            ) from exc
    if status == "succeeded":
        if not completion_evidence:
            raise ArtifactManifestError("succeeded artifact requires completion evidence")
        if partial_failure is not None or uncertain_reason is not None:
            raise ArtifactManifestError("succeeded artifact cannot contain failure provenance")
    if status == "failed" and (
        partial_failure is not None or uncertain_reason is not None
    ):
        raise ArtifactManifestError("failed artifact cannot contain partial/uncertain provenance")
    if status == "partial":
        if not partial_failure:
            raise ArtifactManifestError("partial artifact requires partial failure provenance")
        if uncertain_reason is not None:
            raise ArtifactManifestError("partial artifact cannot contain uncertain_reason")
    if status == "uncertain" and not uncertain_reason:
        raise ArtifactManifestError("uncertain artifact requires uncertain_reason")
    details = value.get("details")
    if details is not None and not isinstance(details, dict):
        raise ArtifactManifestError("manifest details must be an object")
    return dict(value)


def load_manifest(path: Path) -> dict[str, Any]:
    return validate_manifest(read_json_object(path, "artifact manifest"))


def load_operation_incident(path: Path, operation_id: str) -> dict[str, Any]:
    """Read one nofollow operation incident and verify its exact identity."""

    identity = validate_artifact_id(operation_id, "operation id")
    value = read_json_object(path, "operation incident")
    required = {
        "schema",
        "contract_kind",
        "operation_id",
        "name",
        "policy",
        "status",
        "workspace_root",
        "recorded_at",
        "error_type",
        "error",
        "uncertain_reason",
        "view_snapshots",
        "ownership_scopes",
    }
    if set(value) != required:
        raise ArtifactManifestError("operation incident fields are invalid")
    if (
        value["schema"] != 1
        or value["contract_kind"] != "workspace-operation-incident"
        or value["operation_id"] != identity
        or value["status"] not in {"failed", "uncertain"}
        or any(
            not isinstance(value[field], str) or not value[field]
            for field in (
                "name",
                "policy",
                "workspace_root",
                "recorded_at",
                "error_type",
            )
        )
        or not isinstance(value["error"], str)
        or not isinstance(value["view_snapshots"], list)
        or not isinstance(value["ownership_scopes"], list)
        or any(
            not isinstance(item, dict)
            for field in ("view_snapshots", "ownership_scopes")
            for item in value[field]
        )
    ):
        raise ArtifactManifestError("operation incident identity is invalid")
    try:
        datetime.fromisoformat(value["recorded_at"])
    except ValueError as exc:
        raise ArtifactManifestError(
            "operation incident recorded_at is not an ISO timestamp"
        ) from exc
    uncertain_reason = value["uncertain_reason"]
    if (
        value["status"] == "uncertain"
        and (not isinstance(uncertain_reason, str) or not uncertain_reason)
    ) or (value["status"] == "failed" and uncertain_reason is not None):
        raise ArtifactManifestError(
            "operation incident uncertainty does not match its status"
        )
    return value


@dataclass
class RunRecord:
    """Mutable handle to one immutable-identity operation run manifest."""

    paths: RunPaths
    manifest: dict[str, Any]
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _file_states: dict[str, tuple[int, int, int, str]] = field(
        default_factory=dict, repr=False
    )
    _file_indexes: dict[str, dict[str, int]] = field(
        default_factory=dict, repr=False
    )

    def __post_init__(self) -> None:
        self._file_indexes = {
            role: {
                reference["path"]: index
                for index, reference in enumerate(entries)
            }
            for role, entries in self.manifest["files"].items()
        }

    @classmethod
    def begin(
        cls,
        paths: RunPaths,
        *,
        adapter: str,
        source: Mapping[str, Any],
    ) -> "RunRecord":
        files = {role: [] for role in paths.roles}
        manifest = {
            "schema": 4,
            "contract_kind": "run-manifest",
            "owner": paths.owner,
            "operation": paths.operation,
            "variant": paths.variant,
            "operation_id": None,
            "adapter": adapter,
            "run_id": paths.run_id,
            "created_at": utc_now(),
            "completed_at": None,
            "status": "running",
            "source": dict(source),
            "files": files,
            "partial_failure": None,
            "uncertain_reason": None,
            "incident_reference": None,
            "completion_evidence": [],
            "details": None,
        }
        validate_manifest(manifest)
        paths.create()
        expected_root = paths.root.stat(follow_symlinks=False)
        try:
            atomic_write_json(paths.manifest, manifest)
        except BaseException as error:
            try:
                SafeTree(paths.root).remove(expected_root)
            except BaseException as cleanup_error:
                error.add_note(
                    f"could not remove half-initialized run: {cleanup_error}"
                )
            raise
        return cls(paths=paths, manifest=manifest)

    @property
    def status(self) -> str:
        return str(self.manifest["status"])

    def _require_running(self, action: str) -> None:
        if self.status != "running":
            raise RuntimeError(
                f"cannot {action} for terminal artifact with status {self.status!r}"
            )

    def directory(self, role: str, *components: str) -> Path:
        with self._lock:
            self._require_running("create a role directory")
            path = self.paths.path(role, *components)
            try:
                descriptor = _open_nofollow_directory(path, create_missing=True)
            except OSError as exc:
                raise RuntimeError(
                    f"artifact directory cannot traverse a symlink or unsafe component: {path}"
                ) from exc
            os.close(descriptor)
            _resolved_artifact_member(self.paths.role(role), path, f"{role} directory")
            return path

    def path(self, role: str, *components: str) -> Path:
        return self.paths.path(role, *components)

    def add_file(self, role: str, path: Path, *, label: str | None = None) -> dict[str, Any]:
        with self._lock:
            self._require_running("register a file")
            role_root = (
                self.paths.root
                if role == "outputs" and Path(path) == self.paths.result
                else self.paths.role(role)
            )
            candidate = _resolved_artifact_member(
                role_root, Path(path), f"{role} file reference"
            )
            try:
                metadata, digest = _inspect_nofollow_file(candidate)
            except IsADirectoryError:
                descriptor = _open_nofollow_directory(
                    candidate,
                    create_missing=False,
                )
                try:
                    metadata = os.fstat(descriptor)
                finally:
                    os.close(descriptor)
                digest = ""
            if stat.S_ISREG(metadata.st_mode):
                reference = {
                    "path": candidate.relative_to(self.paths.root).as_posix(),
                    "kind": "file",
                    "size": metadata.st_size,
                    "sha256": digest,
                }
                self._file_states[reference["path"]] = (
                    metadata.st_ino,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    digest,
                )
            elif stat.S_ISDIR(metadata.st_mode):
                descriptor = _open_nofollow_directory(candidate, create_missing=False)
                os.close(descriptor)
                reference = {
                    "path": candidate.relative_to(self.paths.root).as_posix(),
                    "kind": "directory",
                }
            else:
                raise RuntimeError(f"artifact reference does not exist: {candidate}")
            if label is not None:
                if not isinstance(label, str) or not label:
                    raise ValueError("artifact file label must be a non-empty string")
                reference["label"] = label
            entries = self.manifest["files"][role]
            indexes = self._file_indexes[role]
            index = indexes.get(reference["path"])
            if index is None:
                indexes[reference["path"]] = len(entries)
                entries.append(reference)
            else:
                entries[index] = reference
            return reference

    def discard_waveforms(self) -> set[Path]:
        """Drop temporary waveforms after the run's consumers have finished."""
        with self._lock:
            self._require_running("discard temporary waveforms")
            removed: set[Path] = set()
            for role in ("outputs", "work"):
                root = self.paths.role(role)
                if root.is_symlink():
                    raise RuntimeError(f"managed {role} root was replaced: {root}")
                for directory, children, files in os.walk(root, followlinks=False):
                    for name in [*children, *files]:
                        path = Path(directory) / name
                        relative = path.relative_to(self.paths.root)
                        if not is_waveform_path(relative):
                            continue
                        if path.is_dir() and not path.is_symlink():
                            shutil.rmtree(path)
                        else:
                            path.unlink()
                        if name in children:
                            children.remove(name)
                        removed.add(relative)
            for relative in tuple(removed):
                parent = relative.parent
                while len(parent.parts) > 1:
                    path = self.paths.root / parent
                    if not path.is_dir() or any(path.iterdir()):
                        break
                    path.rmdir()
                    removed.add(parent)
                    parent = parent.parent
            for role, entries in self.manifest["files"].items():
                retained = [entry for entry in entries
                            if not any(Path(entry["path"]).is_relative_to(path) for path in removed)]
                self.manifest["files"][role] = retained
                self._file_indexes[role] = {entry["path"]: i for i, entry in enumerate(retained)}
            self._file_states = {path: state for path, state in self._file_states.items()
                                 if not any(Path(path).is_relative_to(item) for item in removed)}
            return removed

    def _verify_registered_files(self) -> None:
        for entries in self.manifest["files"].values():
            for reference in entries:
                path = self.paths.root / reference["path"]
                if reference["kind"] == "directory":
                    descriptor = _open_nofollow_directory(path, create_missing=False)
                    os.close(descriptor)
                    continue
                metadata, digest = _inspect_nofollow_file(path)
                state = (
                    metadata.st_ino,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    digest,
                )
                if (
                    reference.get("sha256") != digest
                    or reference.get("size") != metadata.st_size
                    or self._file_states.get(reference["path"]) != state
                ):
                    raise RuntimeError(
                        f"artifact file changed after registration: {reference['path']}"
                    )

    def write_text(
        self,
        role: str,
        components: Sequence[str],
        value: str,
        *,
        label: str | None = None,
    ) -> Path:
        with self._lock:
            self._require_running("write a text file")
            path = self.path(role, *components)
            _write_exclusive_bytes(path, value.encode("utf-8"))
            self.add_file(role, path, label=label)
            return path

    def write_bytes(
        self,
        role: str,
        components: Sequence[str],
        value: bytes,
        *,
        label: str | None = None,
    ) -> Path:
        with self._lock:
            self._require_running("write a binary file")
            if not isinstance(value, bytes):
                raise TypeError("binary artifact value must be bytes")
            path = self.path(role, *components)
            _write_exclusive_bytes(path, value)
            self.add_file(role, path, label=label)
            return path

    def copy_file(
        self,
        role: str,
        components: Sequence[str],
        source: Path,
        *,
        label: str | None = None,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
    ) -> Path:
        with self._lock:
            self._require_running("copy a file")
            path = self.path(role, *components)
            copy_immutable_file(
                source,
                path,
                expected_size=expected_size,
                expected_sha256=expected_sha256,
            )
            self.add_file(role, path, label=label)
            return path

    def write_json(
        self,
        role: str,
        components: Sequence[str],
        value: Mapping[str, Any],
        *,
        label: str | None = None,
    ) -> Path:
        with self._lock:
            self._require_running("write a JSON file")
            path = self.path(role, *components)
            payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode(
                "utf-8"
            )
            _write_exclusive_bytes(path, payload)
            self.add_file(role, path, label=label)
            return path

    def _persist_candidate(self, candidate: Mapping[str, Any]) -> None:
        validated = validate_manifest(candidate)
        atomic_write_json(self.paths.manifest, validated)
        self.manifest = validated

    def write_result(self, value: Mapping[str, Any]) -> Path:
        """Publish the immutable run-level result as registered output evidence."""

        with self._lock:
            self._require_running("write the run result")
            path = self.paths.result
            payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
            _write_exclusive_bytes(path, payload)
            self.add_file("outputs", path)
            return path

    def _transition(
        self,
        status: str,
        *,
        completion_evidence: Sequence[Path] = (),
        partial_failure: Mapping[str, Any] | None = None,
        uncertain_reason: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> Path:
        with self._lock:
            if status not in TERMINAL_STATUSES:
                raise ValueError(f"invalid terminal artifact status: {status!r}")
            if self.status != "running":
                raise RuntimeError(
                    f"illegal artifact status transition {self.status!r} -> {status!r}"
                )
            if details:
                reserved = set(self.manifest).intersection(details)
                if reserved:
                    raise ValueError(
                        f"reserved manifest detail fields: {', '.join(sorted(reserved))}"
                    )
            if completion_evidence:
                self._verify_registered_files()
            proof: list[str] = []
            for path in completion_evidence:
                candidate = _resolved_artifact_member(
                    self.paths.root, Path(path), "completion evidence"
                )
                relative = candidate.relative_to(self.paths.root).as_posix()
                role = "outputs" if relative == "result.json" else Path(relative).parts[0]
                index = self._file_indexes[role].get(relative)
                if index is None:
                    raise RuntimeError(
                        f"completion evidence is not registered in manifest: {relative}"
                    )
                registered = self.manifest["files"][role][index]
                metadata, digest = _inspect_nofollow_file(candidate)
                current_state = (
                    metadata.st_ino,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    digest,
                )
                if (
                    registered["kind"] != "file"
                    or self._file_states.get(relative) != current_state
                ):
                    raise RuntimeError(
                        f"completion evidence changed after registration: {relative}"
                    )
                proof.append(relative)
            if len(proof) != len(set(proof)):
                raise RuntimeError("completion evidence must not contain duplicates")
            if status == "succeeded" and not proof:
                raise RuntimeError("cannot mark artifact succeeded without completion evidence")
            if status == "uncertain" and not uncertain_reason:
                raise RuntimeError("uncertain artifact status requires a reason")
            if status == "partial" and not partial_failure:
                raise RuntimeError("partial artifact status requires provenance")
            candidate_manifest = dict(self.manifest)
            candidate_manifest.update(
                {
                    "status": status,
                    "completed_at": utc_now(),
                    "completion_evidence": proof,
                    "partial_failure": dict(partial_failure) if partial_failure else None,
                    "uncertain_reason": uncertain_reason,
                }
            )
            if details:
                candidate_manifest["details"] = dict(details)
            self._persist_candidate(candidate_manifest)
            return self.paths.manifest

    def succeed(
        self,
        *,
        completion_evidence: Sequence[Path],
        details: Mapping[str, Any] | None = None,
    ) -> Path:
        return self._transition(
            "succeeded", completion_evidence=completion_evidence, details=details
        )

    def complete(
        self,
        status: str,
        *,
        completion_evidence: Sequence[Path],
        partial_failure: Mapping[str, Any] | None = None,
        uncertain_reason: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> Path:
        """Close a fully recorded result without conflating outcome with success."""

        return self._transition(
            status,
            completion_evidence=completion_evidence,
            partial_failure=partial_failure,
            uncertain_reason=uncertain_reason,
            details=details,
        )

    def fail(
        self,
        error: BaseException,
        *,
        partial_failure: Mapping[str, Any] | None = None,
        uncertain_reason: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> Path:
        failure_details = {
            "error_type": type(error).__name__,
            "error": str(error)[:4000],
            **dict(details or {}),
        }
        if uncertain_reason:
            return self._transition(
                "uncertain",
                partial_failure=partial_failure,
                uncertain_reason=uncertain_reason,
                details=failure_details,
            )
        if partial_failure:
            return self._transition(
                "partial", partial_failure=partial_failure, details=failure_details
            )
        return self._transition("failed", details=failure_details)

    def attach_incident(self, incident_path: Path) -> None:
        """Atomically link a safety incident without changing terminal status."""

        with self._lock:
            operation_id = self.manifest.get("operation_id")
            if not isinstance(operation_id, str):
                raise RuntimeError("artifact has no bound operation identity")
            root = Path(os.path.abspath(self.paths.artifact_root))
            incident = _resolved_artifact_member(
                root, Path(incident_path), "operation incident"
            )
            load_operation_incident(incident, operation_id)
            reference = incident.relative_to(root).as_posix()
            existing = self.manifest.get("incident_reference")
            if existing not in {None, reference}:
                raise RuntimeError("artifact already refers to a different operation incident")
            candidate_manifest = dict(self.manifest)
            candidate_manifest["incident_reference"] = reference
            self._persist_candidate(candidate_manifest)

    def bind_operation(self, operation_id: str) -> None:
        """Bind the full workspace operation identity before any OA/tool action."""

        identity = validate_artifact_id(operation_id, "operation id")
        with self._lock:
            self._require_running("bind an operation")
            existing = self.manifest.get("operation_id")
            if existing not in {None, identity}:
                raise RuntimeError("artifact belongs to a different workspace operation")
            candidate_manifest = dict(self.manifest)
            candidate_manifest["operation_id"] = identity
            self._persist_candidate(candidate_manifest)

    @contextmanager
    def failure_boundary(
        self,
        *,
        uncertainty: Callable[[], str | None] = lambda: None,
        partial_failure: Callable[[], Mapping[str, Any] | None] = lambda: None,
        details: Callable[[], Mapping[str, Any] | None] = lambda: None,
        prepare_failure: Callable[[], None] = lambda: None,
    ) -> Iterator[None]:
        """Terminalize failures that happen before a workspace can register callbacks."""

        try:
            yield
        except BaseException as error:
            if self.status == "running":
                try:
                    prepare_failure()
                except Exception as inventory_error:
                    error.add_note(
                        f"could not close artifact failure inventory: {inventory_error}"
                    )
                try:
                    self.fail(
                        error,
                        uncertain_reason=uncertainty(),
                        partial_failure=partial_failure(),
                        details=details(),
                    )
                except Exception as record_error:
                    error.add_note(f"could not record artifact failure: {record_error}")
            raise


def new_identity() -> str:
    return uuid.uuid4().hex
