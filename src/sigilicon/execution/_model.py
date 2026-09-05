"""Small typed interface for planning and running owner operations."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from contextlib import contextmanager
import hashlib
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Callable, Iterator, Mapping, Protocol, runtime_checkable

from sigilicon.artifacts import (
    _inspect_nofollow_file,
    _open_nofollow_directory,
    copy_immutable_file,
    read_nofollow_bytes,
    read_nofollow_text,
)
from sigilicon.canonical import canonical_digest, canonical_json
from sigilicon.contracts import require_relative_path
from sigilicon.paths import validate_artifact_component, validate_artifact_id

if TYPE_CHECKING:
    from sigilicon.execution._workspace import ExecutionWorkspace
    from sigilicon.external_tools import OwnedExecutable


_ADAPTER = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*\Z")
_DIGEST = re.compile(r"sha256-[0-9a-f]{64}\Z")
_RESOURCE = re.compile(r"[A-Za-z][A-Za-z0-9._:/-]{0,255}\Z")
_ENVIRONMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_ROLES = frozenset({"diagnostic", "regression", "qualification", "signoff"})
_LEVELS = frozenset({"l0", "l1", "l2", "l3", "l4"})
_STEP_STATUSES = frozenset(
    {"succeeded", "failed", "blocked", "partial", "uncertain", "cancelled"}
)
_RUN_STATUSES = frozenset({"succeeded", "failed", "partial", "uncertain", "cancelled"})
_RUN_FAILURE_STATUSES = frozenset({"failed", "partial", "uncertain", "cancelled"})

class ContractError(ValueError):
    """An operation, plan, or adapter value violates the execution contract."""


class ExecutionError(RuntimeError):
    """A managed operation could not be executed or restored safely."""


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{label} must be an identifier")
    try:
        return validate_artifact_component(value, label)
    except ValueError as exc:
        raise ContractError(str(exc)) from exc


def adapter_identity(value: object) -> str:
    if not isinstance(value, str) or _ADAPTER.fullmatch(value) is None:
        raise ContractError(f"invalid adapter identity: {value!r}")
    return value


def resource_identity(value: object) -> str:
    if (
        not isinstance(value, str)
        or _RESOURCE.fullmatch(value) is None
        or value.startswith("/")
        or "\\" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ContractError(f"invalid external resource identity: {value!r}")
    return value


def resource_materialization_key(identity: str) -> str:
    logical = resource_identity(identity)
    return "resource-" + hashlib.sha256(logical.encode("utf-8")).hexdigest()


def _source_name(value: object) -> str:
    try:
        return require_relative_path(value, "source name").as_posix()
    except ValueError as exc:
        raise ContractError(str(exc)) from exc


def _freeze(value: Any, label: str) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractError(f"{label} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ContractError(f"{label} contains a non-string key")
        return MappingProxyType({key: _freeze(item, label) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item, label) for item in value)
    raise ContractError(f"{label} contains non-portable {type(value).__name__}")


def json_value(value: Any) -> Any:
    """Return a portable mutable projection of a frozen execution value."""

    if isinstance(value, Mapping):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [json_value(item) for item in value]
    return value


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]


@dataclass(frozen=True)
class Evidence:
    """Cross-domain classification attached to one execution step."""

    role: str
    level: str
    scope: str

    def __post_init__(self) -> None:
        if not isinstance(self.role, str):
            raise ContractError("evidence role must be text")
        if not isinstance(self.level, str):
            raise ContractError("evidence level must be text")
        if self.role not in _ROLES:
            raise ContractError(f"unsupported evidence role: {self.role!r}")
        if self.level not in _LEVELS:
            raise ContractError(f"unsupported evidence level: {self.level!r}")
        if not isinstance(self.scope, str) or not self.scope.strip():
            raise ContractError("evidence scope must be non-empty text")

    @property
    def record(self) -> dict[str, str]:
        return {"role": self.role, "level": self.level, "scope": self.scope}

@dataclass(frozen=True)
class Source:
    """Content-addressed reference to one project-owned source file."""

    path: str
    root: Path = field(repr=False, compare=False)
    sha256: str
    size: int
    executable: bool
    location: Path = field(repr=False, compare=False)
    scope: str = "project"
    device: int = field(default=-1, repr=False, compare=False)
    inode: int = field(default=-1, repr=False, compare=False)
    mtime_ns: int = field(default=-1, repr=False, compare=False)

    def __post_init__(self) -> None:
        relative = PurePosixPath(self.path)
        if (
            not self.path
            or relative.is_absolute()
            or "\\" in self.path
            or relative.as_posix() != self.path
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ContractError(f"source path must be canonical and relative: {self.path!r}")
        root = Path(self.root).resolve()
        location = Path(self.location).absolute()
        expected = root.joinpath(*relative.parts)
        if location != expected or location.resolve() != expected:
            raise ContractError("source location disagrees with its root or traverses a symlink")
        if (
            not isinstance(self.sha256, str)
            or len(self.sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.sha256)
            or type(self.size) is not int
            or self.size < 0
            or not isinstance(self.executable, bool)
        ):
            raise ContractError("source snapshot fields have invalid types")
        if not isinstance(self.scope, str) or _ADAPTER.fullmatch(self.scope) is None:
            raise ContractError("source scope must be a semantic identity")
        if any(type(value) is not int for value in (self.device, self.inode, self.mtime_ns)):
            raise ContractError("source filesystem identity fields must be integers")
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "location", location)

    @classmethod
    def capture(
        cls,
        path: Path,
        *,
        root: Path,
        scope: str = "project",
    ) -> "Source":
        source_root = Path(root).resolve()
        configured = Path(path).absolute()
        resolved = configured.resolve()
        if configured != resolved or not resolved.is_relative_to(source_root):
            raise ContractError(f"source must be a non-symlink below {source_root}: {path}")
        metadata = resolved.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            raise ContractError(f"source must be a regular file: {path}")
        inspected, digest = _inspect_nofollow_file(
            resolved,
            require_single_link=False,
        )
        return cls(
            resolved.relative_to(source_root).as_posix(),
            source_root,
            digest,
            inspected.st_size,
            bool(inspected.st_mode & 0o111),
            resolved,
            scope,
            inspected.st_dev,
            inspected.st_ino,
            inspected.st_mtime_ns,
        )

    @property
    def record(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "executable": self.executable,
        }

    def read_bytes(self) -> bytes:
        payload = read_nofollow_bytes(self.location)
        if (
            len(payload) != self.size
            or hashlib.sha256(payload).hexdigest() != self.sha256
        ):
            raise ContractError(f"source changed after planning: {self.path}")
        return payload

    def read_text(self) -> str:
        try:
            return self.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ContractError(f"source is not UTF-8 text: {self.path}") from exc

    def current(self) -> bool:
        try:
            if not self.metadata_current():
                return False
            _, digest = _inspect_nofollow_file(
                self.location,
                require_single_link=False,
            )
            return (
                digest == self.sha256
            )
        except (OSError, RuntimeError):
            return False

    def metadata_current(self) -> bool:
        """Check stable source identity without rereading its payload."""

        try:
            metadata = self.location.stat(follow_symlinks=False)
            return (
                self.location.absolute() == self.location.resolve()
                and stat.S_ISREG(metadata.st_mode)
                and metadata.st_size == self.size
                and bool(metadata.st_mode & 0o111) == self.executable
                and metadata.st_dev == self.device
                and metadata.st_ino == self.inode
                and metadata.st_mtime_ns == self.mtime_ns
            )
        except (OSError, RuntimeError):
            return False

@dataclass(frozen=True)
class ResourceFile:
    """One content-addressed file inside a runtime resource."""

    path: str
    sha256: str
    size: int
    location: Path = field(repr=False, compare=False)
    executable: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.path, str):
            raise ContractError("resource file path must be text")
        if self.path:
            relative = PurePosixPath(self.path)
            if (
                relative.is_absolute()
                or "\\" in self.path
                or relative.as_posix() != self.path
                or any(part in {"", ".", ".."} for part in relative.parts)
            ):
                raise ContractError("resource file path must be canonical and relative")
        if (
            not isinstance(self.sha256, str)
            or len(self.sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.sha256)
            or type(self.size) is not int
            or self.size < 0
        ):
            raise ContractError("resource file reference is invalid")
        location = Path(self.location).absolute()
        if location != location.resolve():
            raise ContractError("resource file must not traverse a symlink")
        if not isinstance(self.executable, bool):
            raise ContractError("resource file executable flag must be boolean")
        object.__setattr__(self, "location", location)

    @property
    def record(self) -> dict[str, object]:
        return {
            "path": self.path,
            "size": self.size,
            "sha256": self.sha256,
            "executable": self.executable,
        }

    def read_bytes(self) -> bytes:
        payload = read_nofollow_bytes(self.location)
        if (
            len(payload) != self.size
            or hashlib.sha256(payload).hexdigest() != self.sha256
        ):
            raise ContractError("resource file changed after planning")
        return payload


@dataclass(frozen=True)
class ResourceBinding:
    """Exact tool, file, directory, or value bound to one runtime identity."""

    identity: str
    kind: str
    sha256: str
    location: Path | None = field(repr=False)
    files: tuple[ResourceFile, ...] = field(repr=False, compare=False)
    directories: tuple[str, ...] = ()
    value: str | None = field(default=None, repr=False)
    _fingerprint: tuple[tuple[object, ...], ...] = field(
        default=(), repr=False, compare=False
    )

    def __post_init__(self) -> None:
        resource_identity(self.identity)
        if self.kind not in {"tool", "file", "directory", "value", "destination"}:
            raise ContractError("unsupported resource binding kind")
        location = (
            None if self.location is None else Path(self.location).absolute()
        )
        if self.kind == "value":
            if (
                location is not None
                or self.files
                or self.directories
                or not isinstance(self.value, str)
                or not self.value
            ):
                raise ContractError("value resource binding has an invalid payload")
            expected = hashlib.sha256(self.value.encode("utf-8")).hexdigest()
            if expected != self.sha256:
                raise ContractError("resource binding digest disagrees with its value")
            if self._fingerprint:
                raise ContractError("value resource binding cannot have a fingerprint")
            return
        if location is None:
            raise ContractError("path resource binding requires a location")
        if self.kind != "tool" and location != location.resolve():
            raise ContractError("resource binding must not traverse a symlink")
        if self.value is not None:
            raise ContractError("path resource binding cannot contain a value")
        if self.kind == "destination":
            if self.files or self.directories or self.sha256 != hashlib.sha256(
                f"destination:{self.identity}".encode()
            ).hexdigest():
                raise ContractError("destination binding cannot contain input content")
            return
        if not isinstance(self.files, tuple) or any(
            not isinstance(item, ResourceFile) for item in self.files
        ):
            raise ContractError("resource binding files must be ResourceFile values")
        if not isinstance(self.directories, tuple) or any(
            not isinstance(item, str) or not item for item in self.directories
        ):
            raise ContractError("resource binding directories must be relative paths")
        for directory in self.directories:
            relative = PurePosixPath(directory)
            if (
                relative.is_absolute()
                or "\\" in directory
                or relative.as_posix() != directory
                or any(part in {"", ".", ".."} for part in relative.parts)
            ):
                raise ContractError(
                    "resource binding directories must be canonical and relative"
                )
        if self.kind in {"tool", "file"}:
            if len(self.files) != 1 or self.files[0].path or self.directories:
                raise ContractError("file resource binding must contain one root payload")
            expected = self.files[0].sha256
        else:
            file_paths = tuple(item.path for item in self.files)
            if any(not path for path in file_paths):
                raise ContractError("directory resource files must be relative")
            if len(set(file_paths)) != len(file_paths):
                raise ContractError("directory resource contains duplicate files")
            if len(set(self.directories)) != len(self.directories):
                raise ContractError("directory resource contains duplicate directories")
            if file_paths != tuple(sorted(file_paths)) or self.directories != tuple(
                sorted(self.directories)
            ):
                raise ContractError("directory resource manifest must be sorted")
            if set(file_paths) & set(self.directories):
                raise ContractError(
                    "directory resource file and directory paths collide"
                )
            expected = hashlib.sha256(
                canonical_json(
                    {
                        "directories": list(self.directories),
                        "files": [item.record for item in self.files],
                    }
                ).encode("utf-8")
            ).hexdigest()
        if expected != self.sha256:
            raise ContractError("resource binding digest disagrees with its manifest")
        if not isinstance(self._fingerprint, tuple) or any(
            not isinstance(item, tuple) for item in self._fingerprint
        ):
            raise ContractError("resource binding fingerprint is invalid")
        object.__setattr__(self, "location", location)

    @classmethod
    def capture(
        cls,
        path: Path,
        *,
        identity: str,
        kind: str | None = None,
    ) -> "ResourceBinding":
        location = Path(path).absolute()
        resolved = location.resolve(strict=True)
        if kind != "tool" and location != resolved:
            raise ContractError(f"resource binding must not traverse a symlink: {path}")
        selected = resolved if kind == "tool" else location
        metadata = selected.stat(follow_symlinks=False)
        if kind == "destination":
            if not stat.S_ISDIR(metadata.st_mode):
                raise ContractError("publication destination must be a directory")
            return cls(
                resource_identity(identity), kind,
                hashlib.sha256(f"destination:{identity}".encode()).hexdigest(),
                location, (),
                _fingerprint=((metadata.st_dev, metadata.st_ino),),
            )
        if stat.S_ISREG(metadata.st_mode):
            selected_kind = "file" if kind is None else kind
            if selected_kind not in {"tool", "file"}:
                raise ContractError("regular resource must be a tool or file")
            current, digest = _inspect_nofollow_file(
                selected,
                require_single_link=False,
            )
            launcher = location.stat(follow_symlinks=False)
            if location.resolve(strict=True) != resolved:
                raise ContractError("tool launcher changed while being captured")
            fingerprint = (
                (
                    "launcher",
                    launcher.st_dev,
                    launcher.st_ino,
                    launcher.st_size,
                    launcher.st_mtime_ns,
                    launcher.st_mode,
                ),
                (
                    "target",
                    current.st_dev,
                    current.st_ino,
                    current.st_size,
                    current.st_mtime_ns,
                    current.st_mode,
                ),
            )
            entry = ResourceFile(
                "",
                digest,
                current.st_size,
                selected,
                bool(current.st_mode & 0o111),
            )
            return cls(
                resource_identity(identity),
                selected_kind,
                entry.sha256,
                location,
                (entry,),
                _fingerprint=fingerprint,
            )
        if not stat.S_ISDIR(metadata.st_mode):
            raise ContractError(f"resource binding must be a file or directory: {path}")
        if kind not in {None, "directory"}:
            raise ContractError("directory resource must use the directory kind")

        directories: list[str] = []
        files: list[ResourceFile] = []
        fingerprint: list[tuple[object, ...]] = []

        def stable_file(
            descriptor: int,
            before: os.stat_result,
        ) -> tuple[int, str]:
            digest = hashlib.sha256()
            while chunk := os.read(descriptor, 1024 * 1024):
                digest.update(chunk)
            after = os.fstat(descriptor)
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
            ):
                raise ContractError("resource file changed while being captured")
            return after.st_size, digest.hexdigest()

        def capture_directory(descriptor: int, prefix: PurePosixPath) -> None:
            for name in sorted(os.listdir(descriptor)):
                visible = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                relative = (prefix / name).as_posix()
                if stat.S_ISLNK(visible.st_mode):
                    raise ContractError(
                        f"resource directory must not contain symlinks: {relative}"
                    )
                if stat.S_ISDIR(visible.st_mode):
                    child = os.open(
                        name,
                        os.O_RDONLY
                        | os.O_DIRECTORY
                        | os.O_CLOEXEC
                        | os.O_NOFOLLOW,
                        dir_fd=descriptor,
                    )
                elif stat.S_ISREG(visible.st_mode):
                    child = os.open(
                        name,
                        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                        dir_fd=descriptor,
                    )
                else:
                    raise ContractError(
                        "resource directory contains an unsupported entry: "
                        f"{relative}"
                    )
                try:
                    held = os.fstat(child)
                    if (held.st_dev, held.st_ino) != (visible.st_dev, visible.st_ino):
                        raise ContractError(
                            f"resource directory entry changed: {relative}"
                        )
                    fingerprint.append(
                        (
                            relative,
                            held.st_dev,
                            held.st_ino,
                            held.st_size,
                            held.st_mtime_ns,
                            held.st_mode,
                        )
                    )
                    if stat.S_ISDIR(held.st_mode):
                        directories.append(relative)
                        capture_directory(child, prefix / name)
                    else:
                        size, digest = stable_file(child, held)
                        files.append(
                            ResourceFile(
                                relative,
                                digest,
                                size,
                                location.joinpath(*PurePosixPath(relative).parts),
                                bool(held.st_mode & 0o111),
                            )
                        )
                    after = os.fstat(child)
                    visible_after = os.stat(
                        name,
                        dir_fd=descriptor,
                        follow_symlinks=False,
                    )
                    if (
                        held.st_dev,
                        held.st_ino,
                        held.st_size,
                        held.st_mtime_ns,
                        held.st_mode,
                    ) != (
                        after.st_dev,
                        after.st_ino,
                        after.st_size,
                        after.st_mtime_ns,
                        after.st_mode,
                    ) or (after.st_dev, after.st_ino) != (
                        visible_after.st_dev,
                        visible_after.st_ino,
                    ):
                        raise ContractError(
                            f"resource directory entry changed: {relative}"
                        )
                finally:
                    os.close(child)

        root_descriptor = _open_nofollow_directory(
            location,
            create_missing=False,
        )
        try:
            held_root = os.fstat(root_descriptor)
            visible_root = location.stat(follow_symlinks=False)
            if (held_root.st_dev, held_root.st_ino) != (
                visible_root.st_dev,
                visible_root.st_ino,
            ):
                raise ContractError("resource directory root changed while opening")
            fingerprint.append(
                (
                    "",
                    held_root.st_dev,
                    held_root.st_ino,
                    held_root.st_size,
                    held_root.st_mtime_ns,
                    held_root.st_mode,
                )
            )
            capture_directory(root_descriptor, PurePosixPath())
            current_root = os.fstat(root_descriptor)
            visible_root = location.stat(follow_symlinks=False)
            if (
                held_root.st_dev,
                held_root.st_ino,
                held_root.st_size,
                held_root.st_mtime_ns,
                held_root.st_mode,
            ) != (
                current_root.st_dev,
                current_root.st_ino,
                current_root.st_size,
                current_root.st_mtime_ns,
                current_root.st_mode,
            ) or (current_root.st_dev, current_root.st_ino) != (
                visible_root.st_dev,
                visible_root.st_ino,
            ):
                raise ContractError("resource directory root changed while capturing")
        finally:
            os.close(root_descriptor)
        directory_tuple = tuple(sorted(directories))
        file_tuple = tuple(sorted(files, key=lambda item: item.path))
        digest = hashlib.sha256(
            canonical_json(
                {
                    "directories": list(directory_tuple),
                    "files": [item.record for item in file_tuple],
                }
            ).encode("utf-8")
        ).hexdigest()
        return cls(
            resource_identity(identity),
            "directory",
            digest,
            location,
            file_tuple,
            directory_tuple,
            _fingerprint=tuple(fingerprint),
        )

    @classmethod
    def capture_value(cls, value: str, *, identity: str) -> "ResourceBinding":
        if not isinstance(value, str) or not value:
            raise ContractError("runtime value must be non-empty text")
        return cls(
            resource_identity(identity),
            "value",
            hashlib.sha256(value.encode("utf-8")).hexdigest(),
            None,
            (),
            value=value,
        )

    @property
    def record(self) -> dict[str, object]:
        common: dict[str, object] = {
            "identity": self.identity,
            "kind": self.kind,
            "sha256": self.sha256,
            "size": (
                len(self.value.encode("utf-8"))
                if self.kind == "value" and self.value is not None
                else sum(item.size for item in self.files)
            ),
        }
        if self.kind == "value":
            assert self.value is not None
            common["value"] = self.value
        elif self.kind in {"tool", "file"}:
            common["executable"] = self.files[0].executable
        elif self.kind == "directory":
            common["directories"] = list(self.directories)
            common["files"] = [item.record for item in self.files]
        return common

    @property
    def materialization_key(self) -> str:
        return resource_materialization_key(self.identity)

    def read_bytes(self) -> bytes:
        if self.kind not in {"tool", "file"}:
            raise ContractError("resource has no single binary payload")
        return self.files[0].read_bytes()

    def read_text(self) -> str:
        try:
            return self.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ContractError(
                f"resource binding is not UTF-8 text: {self.identity}"
            ) from exc

    def current(self) -> bool:
        if self.kind == "value":
            return True
        assert self.location is not None
        try:
            current = type(self).capture(
                self.location,
                identity=self.identity,
                kind=self.kind,
            )
            return (
                current.record == self.record
                and current._fingerprint == self._fingerprint
            )
        except (OSError, RuntimeError, ContractError):
            return False


    def matches_owned_tool(self, owned: OwnedExecutable) -> bool:
        """Match a held executable to this exact planned tool binding."""

        if self.kind != "tool" or self.location is None:
            return False
        try:
            launcher = self.location.stat(follow_symlinks=False)
            target = os.fstat(owned.target.fd)
            actual = (
                (
                    "launcher",
                    launcher.st_dev,
                    launcher.st_ino,
                    launcher.st_size,
                    launcher.st_mtime_ns,
                    launcher.st_mode,
                ),
                (
                    "target",
                    target.st_dev,
                    target.st_ino,
                    target.st_size,
                    target.st_mtime_ns,
                    target.st_mode,
                ),
            )
            digest = hashlib.sha256()
            offset = 0
            while chunk := os.pread(owned.target.fd, 1024 * 1024, offset):
                digest.update(chunk)
                offset += len(chunk)
            return (
                actual == self._fingerprint
                and self.location.resolve(strict=True) == owned.target.path
                and digest.hexdigest() == self.sha256
            )
        except (OSError, RuntimeError):
            return False


def validate_resource_record(record: Mapping[str, Any]) -> str:
    """Validate one portable ResourceBinding record through its typed model."""

    if not isinstance(record, Mapping):
        raise ContractError("resource record must be a mapping")
    common = {"identity", "kind", "sha256", "size"}
    identity = record.get("identity")
    kind = record.get("kind")
    digest = record.get("sha256")
    size = record.get("size")
    if (
        not isinstance(identity, str)
        or kind not in {"tool", "file", "directory", "value", "destination"}
        or not isinstance(digest, str)
        or type(size) is not int
        or size < 0
    ):
        raise ContractError("resource record header is invalid")
    if kind == "destination":
        if set(record) != common or size != 0:
            raise ContractError("destination resource record is invalid")
        binding = ResourceBinding(identity, kind, digest, Path("/"), ())
    elif kind == "value":
        value = record.get("value")
        if set(record) != common | {"value"} or not isinstance(value, str):
            raise ContractError("value resource record is invalid")
        binding = ResourceBinding(identity, kind, digest, None, (), value=value)
    elif kind in {"tool", "file"}:
        executable = record.get("executable")
        if set(record) != common | {"executable"} or not isinstance(
            executable, bool
        ):
            raise ContractError("file resource record is invalid")
        binding = ResourceBinding(
            identity,
            kind,
            digest,
            Path("/"),
            (ResourceFile("", digest, size, Path("/"), executable),),
        )
    else:
        directories = record.get("directories")
        files = record.get("files")
        if (
            set(record) != common | {"directories", "files"}
            or not isinstance(directories, list)
            or not isinstance(files, list)
        ):
            raise ContractError("directory resource record is invalid")
        try:
            entries = tuple(
                ResourceFile(
                    item["path"],
                    item["sha256"],
                    item["size"],
                    Path("/"),
                    item["executable"],
                )
                for item in files
                if isinstance(item, Mapping)
                and set(item) == {"path", "sha256", "size", "executable"}
            )
        except (KeyError, TypeError) as exc:
            raise ContractError("directory resource file record is invalid") from exc
        if len(entries) != len(files):
            raise ContractError("directory resource file record is invalid")
        binding = ResourceBinding(
            identity,
            kind,
            digest,
            Path("/"),
            entries,
            tuple(directories),
        )
    if binding.record != dict(record):
        raise ContractError("resource record is not canonical")
    return binding.kind


@dataclass(frozen=True)
class RuntimeEnvironment:
    """Owner-declared mapping from runner environment names to resource identities."""

    tools: Mapping[str, str] = field(default_factory=dict)
    files: Mapping[str, str] = field(default_factory=dict)
    directories: Mapping[str, str] = field(default_factory=dict)
    values: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        names: set[str] = set()
        for label in ("tools", "files", "directories", "values"):
            raw = getattr(self, label)
            if not isinstance(raw, Mapping):
                raise ContractError(f"runtime environment {label} must be a mapping")
            checked: dict[str, str] = {}
            for name, identity in raw.items():
                if not isinstance(name, str) or _ENVIRONMENT.fullmatch(name) is None:
                    raise ContractError(
                        f"invalid runtime environment name in {label}: {name!r}"
                    )
                if name in names:
                    raise ContractError(
                        f"runtime environment name is bound more than once: {name}"
                    )
                names.add(name)
                checked[name] = resource_identity(identity)
            object.__setattr__(self, label, MappingProxyType(checked))

    @property
    def record(self) -> dict[str, dict[str, str]]:
        return {
            label: dict(sorted(getattr(self, label).items()))
            for label in ("tools", "files", "directories", "values")
        }


@runtime_checkable
class PlannedAction(Protocol):
    """Adapter-owned typed action recorded as part of one Step."""

    @property
    def record(self) -> Mapping[str, JsonValue]: ...


@dataclass(frozen=True)
class Step:
    """One typed adapter action and its exact input closure."""

    id: str
    uses: str
    config: Mapping[str, JsonValue]
    needs: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    evidence: Evidence | None = None
    resources: tuple[str, ...] = ()
    runtime: RuntimeEnvironment = field(default_factory=RuntimeEnvironment)
    action: PlannedAction | None = field(default=None, repr=False, compare=False)
    source_closure: tuple[Source, ...] = field(default=(), repr=False, compare=False)
    resource_closure: tuple[ResourceBinding, ...] = field(
        default=(), repr=False, compare=False
    )
    _action_identity: str | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _identifier(self.id, "step id"))
        object.__setattr__(self, "uses", adapter_identity(self.uses))
        if not isinstance(self.config, Mapping):
            raise ContractError("step config must be a mapping")
        if not isinstance(self.needs, tuple):
            raise ContractError("prepared step needs must be a tuple")
        needs = tuple(_identifier(value, "step dependency") for value in self.needs)
        if self.id in needs or len(needs) != len(set(needs)):
            raise ContractError(f"step {self.id!r} has invalid dependencies")
        if not isinstance(self.sources, tuple):
            raise ContractError("prepared step sources must be a tuple")
        sources = tuple(_source_name(source) for source in self.sources)
        if len(sources) != len(set(sources)):
            raise ContractError("prepared step sources contain duplicates")
        if self.evidence is not None and not isinstance(self.evidence, Evidence):
            raise ContractError("prepared step evidence must be an Evidence value")
        if not isinstance(self.resources, tuple):
            raise ContractError("prepared step resources must be a tuple")
        resources = tuple(resource_identity(value) for value in self.resources)
        if len(resources) != len(set(resources)):
            raise ContractError("prepared step resources contain duplicates")
        if not isinstance(self.runtime, RuntimeEnvironment):
            raise ContractError("prepared step runtime must be a RuntimeEnvironment")
        if self.action is not None:
            if not isinstance(self.action, PlannedAction):
                raise ContractError("step action must expose one portable record")
            action_record = _freeze(self.action.record, "step action")
            object.__setattr__(
                self,
                "_action_identity",
                canonical_digest(json_value(action_record)),
            )
        if not isinstance(self.source_closure, tuple) or any(
            not isinstance(source, Source) for source in self.source_closure
        ):
            raise ContractError("step source closure must contain Source values")
        if len({source.path for source in self.source_closure}) != len(
            self.source_closure
        ):
            raise ContractError("step source closure contains duplicate names")
        captured_sources = tuple(source.path for source in self.source_closure)
        if captured_sources:
            if sources and set(sources) != set(captured_sources):
                raise ContractError("step source names disagree with their exact closure")
            if not sources:
                sources = captured_sources
        if not isinstance(self.resource_closure, tuple) or any(
            not isinstance(resource, ResourceBinding)
            for resource in self.resource_closure
        ):
            raise ContractError(
                "step resource closure must contain ResourceBinding values"
            )
        if len({resource.identity for resource in self.resource_closure}) != len(
            self.resource_closure
        ):
            raise ContractError("step resource closure contains duplicate identities")
        captured_resources = tuple(
            resource.identity for resource in self.resource_closure
        )
        if captured_resources:
            if resources and set(resources) != set(captured_resources):
                raise ContractError(
                    "step resource names disagree with their exact closure"
                )
            if not resources:
                resources = captured_resources
        object.__setattr__(self, "needs", needs)
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "resources", resources)
        object.__setattr__(self, "config", _freeze(self.config, "step config"))

    def validate_action(self) -> None:
        """Reject mutation of an adapter-owned action after planning."""

        if self.action is None:
            if self._action_identity is not None:
                raise ExecutionError("step action identity drift")
            return
        current = canonical_digest(json_value(self.action.record))
        if current != self._action_identity:
            raise ExecutionError("step action identity drift")

    @property
    def record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "uses": self.uses,
            "needs": list(self.needs),
            "config": json_value(self.config),
            "action": None if self.action is None else json_value(self.action.record),
            "sources": list(self.sources),
            "resources": list(self.resources),
            "runtime": self.runtime.record,
            "evidence": None if self.evidence is None else self.evidence.record,
        }


def _topology(steps: tuple[Step, ...]) -> tuple[Step, ...]:
    by_id = {step.id: step for step in steps}
    if len(by_id) != len(steps):
        raise ContractError("operation contains duplicate step ids")
    unknown = {
        dependency
        for step in steps
        for dependency in step.needs
        if dependency not in by_id
    }
    if unknown:
        raise ContractError(f"operation references unknown step dependencies: {sorted(unknown)}")
    indegree = {step.id: len(step.needs) for step in steps}
    dependents: dict[str, list[Step]] = {step.id: [] for step in steps}
    for step in steps:
        for dependency in step.needs:
            dependents[dependency].append(step)
    ready = deque(step for step in steps if indegree[step.id] == 0)
    ordered: list[Step] = []
    while ready:
        step = ready.popleft()
        ordered.append(step)
        for dependent in dependents[step.id]:
            indegree[dependent.id] -= 1
            if indegree[dependent.id] == 0:
                ready.append(dependent)
    if len(ordered) != len(steps):
        raise ContractError("operation step graph contains a cycle")
    return tuple(ordered)


@dataclass(frozen=True)
class ExecutionPlan:
    """Complete immutable source and runtime closure for one operation."""

    project_identity: str
    owner: str
    operation: str
    variant: str | None
    steps: tuple[Step, ...]
    sources: tuple[Source, ...]
    resources: tuple[ResourceBinding, ...] = field(repr=False)
    _composition_sources: tuple[Source, ...] = field(
        default=(),
        init=False,
        repr=False,
        compare=False,
    )
    _authority: object | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    _identity: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.project_identity, str) or _DIGEST.fullmatch(
            self.project_identity
        ) is None:
            raise ContractError("execution plan project identity must be a SHA-256 digest")
        object.__setattr__(self, "owner", _identifier(self.owner, "owner"))
        object.__setattr__(self, "operation", _identifier(self.operation, "operation"))
        if self.variant is not None:
            object.__setattr__(self, "variant", _identifier(self.variant, "variant"))
        if not isinstance(self.steps, tuple) or not self.steps:
            raise ContractError("execution plan must contain at least one step")
        if any(not isinstance(step, Step) for step in self.steps):
            raise ContractError("execution plan steps must be Step values")
        if not isinstance(self.sources, tuple) or not self.sources:
            raise ContractError("execution plan must retain its operation source")
        if any(not isinstance(source, Source) for source in self.sources):
            raise ContractError("execution plan sources must be Source values")
        if not isinstance(self._composition_sources, tuple) or any(
            not isinstance(source, Source) for source in self._composition_sources
        ):
            raise ContractError("execution plan composition monitor is invalid")
        closure = {(source.root, source.path): source for source in self.sources}
        if len(closure) != len(self.sources):
            raise ContractError("execution plan contains duplicate source identities")
        if len({source.path for source in self.sources}) != len(self.sources):
            raise ContractError("execution plan source paths collide across scopes")
        source_names = {item.path for item in self.sources}
        for step in self.steps:
            for source in step.sources:
                if source not in source_names:
                    raise ContractError(
                        f"step {step.id!r} source is outside the plan source closure"
                    )
        if not isinstance(self.resources, tuple) or any(
            not isinstance(resource, ResourceBinding) for resource in self.resources
        ):
            raise ContractError("execution plan resources must be ResourceBinding values")
        resource_closure = {
            resource.identity: resource for resource in self.resources
        }
        if len(resource_closure) != len(self.resources):
            raise ContractError("execution plan contains duplicate resource identities")
        referenced = {resource for step in self.steps for resource in step.resources}
        if referenced != set(resource_closure):
            raise ContractError(
                "execution plan resource closure disagrees with its steps"
            )
        object.__setattr__(self, "steps", _topology(self.steps))
        object.__setattr__(self, "_identity", canonical_digest(self.record))

    @property
    def record(self) -> dict[str, Any]:
        return {
            "schema": 15,
            "contract_kind": "execution-plan",
            "project_identity": self.project_identity,
            "owner": self.owner,
            "operation": self.operation,
            "variant": self.variant,
            "sources": [source.record for source in self.sources],
            "resources": [resource.record for resource in self.resources],
            "steps": [step.record for step in self.steps],
        }

    @property
    def identity(self) -> str:
        return self._identity


def _bind_execution_plan(
    plan: ExecutionPlan,
    *,
    composition_sources: tuple[Source, ...],
    authority: object,
) -> ExecutionPlan:
    """Bind Project-only monitoring and authority to a new public plan value."""

    if not isinstance(composition_sources, tuple) or any(
        not isinstance(source, Source) for source in composition_sources
    ):
        raise ContractError("execution plan composition monitor is invalid")
    object.__setattr__(plan, "_composition_sources", composition_sources)
    object.__setattr__(plan, "_authority", authority)
    return plan


@dataclass(frozen=True)
class Resources:
    """Project-configured resources plus one filtered host environment."""

    capabilities: frozenset[str] = frozenset()
    tools: Mapping[str, str] = field(default_factory=dict)
    files: Mapping[str, str] = field(default_factory=dict)
    directories: Mapping[str, str] = field(default_factory=dict)
    destinations: Mapping[str, str] = field(default_factory=dict)
    values: Mapping[str, str] = field(default_factory=dict)
    inherit_environment: tuple[str, ...] = ()
    environment: Mapping[str, str] = field(default_factory=dict)
    _tool_bindings: Mapping[str, ResourceBinding] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.capabilities, frozenset) or any(
            not isinstance(item, str) or _ADAPTER.fullmatch(item) is None
            for item in self.capabilities
        ):
            raise ContractError("resource capabilities must be semantic identities")
        tables = {
            "tools": self.tools,
            "files": self.files,
            "directories": self.directories,
            "destinations": self.destinations,
            "values": self.values,
        }
        identities: set[str] = set()
        for label, table in tables.items():
            if not isinstance(table, Mapping):
                raise ContractError(f"resource {label} must be a mapping")
            checked: dict[str, str] = {}
            for name, value in table.items():
                identity = resource_identity(name)
                if identity in identities:
                    raise ContractError(
                        f"resource identity is configured more than once: {identity}"
                    )
                if not isinstance(value, str) or not value:
                    raise ContractError(
                        f"resource {label}.{identity} must be non-empty text"
                    )
                if label != "values" and not Path(value).is_absolute():
                    raise ContractError(
                        f"resource {label}.{identity} must be an absolute path"
                    )
                identities.add(identity)
                checked[identity] = value
            object.__setattr__(self, label, MappingProxyType(checked))
        if (
            not isinstance(self.inherit_environment, tuple)
            or any(
                not isinstance(name, str) or _ENVIRONMENT.fullmatch(name) is None
                for name in self.inherit_environment
            )
            or len(self.inherit_environment) != len(set(self.inherit_environment))
        ):
            raise ContractError(
                "inherited environment must be a unique tuple of environment names"
            )
        if not isinstance(self.environment, Mapping) or any(
            not isinstance(key, str)
            or not key
            or not isinstance(value, str)
            for key, value in self.environment.items()
        ):
            raise ContractError("process environment must map names to strings")
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))
        if (
            not isinstance(self._tool_bindings, Mapping)
            or any(
                identity not in self.tools
                or not isinstance(binding, ResourceBinding)
                or binding.identity != identity
                or binding.kind != "tool"
                or binding.location is None
                or str(binding.location) != self.tools[identity]
                for identity, binding in self._tool_bindings.items()
            )
        ):
            raise ContractError("held tool bindings disagree with configured tools")
        object.__setattr__(
            self,
            "_tool_bindings",
            MappingProxyType(dict(self._tool_bindings)),
        )

    def configured_tool(self, name: str) -> Path | None:
        """Return a configured executable when it currently exists."""

        identity = resource_identity(name)
        value = self.tools.get(identity)
        if value is None:
            return None
        path = Path(value)
        return path if path.is_file() and os.access(path, os.X_OK) else None

    def require_tool(self, name: str) -> Path:
        """Return one required project-configured executable."""

        identity = resource_identity(name)
        path = self.configured_tool(identity)
        if path is None:
            raise ContractError(
                f"required runtime tool is missing or not executable: {identity}"
            )
        return path

    @contextmanager
    def owned_tool(self, name: str) -> Iterator[OwnedExecutable]:
        """Hold a tool and prove it matches the execution plan before use."""

        from sigilicon.external_tools import owned_executable

        identity = resource_identity(name)
        path = self.require_tool(identity)
        with owned_executable(path) as owned:
            binding = self._tool_bindings.get(identity)
            if binding is not None and not binding.matches_owned_tool(owned):
                raise ExecutionError(
                    f"runtime tool changed after planning: {identity}"
                )
            yield owned

    def require_file(self, name: str) -> Path:
        """Return one required project-configured regular file."""

        identity = resource_identity(name)
        value = self.files.get(identity)
        path = None if value is None else Path(value)
        if path is None or not path.is_file():
            raise ContractError(f"required runtime file is missing: {identity}")
        return path

    def require_directory(self, name: str) -> Path:
        """Return one required project-configured directory."""

        identity = resource_identity(name)
        value = self.directories.get(identity)
        path = None if value is None else Path(value)
        if path is None:
            raise ContractError(f"required runtime directory is missing: {identity}")
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ContractError(
                f"required runtime directory is missing: {identity}"
            ) from exc
        if not resolved.is_dir():
            raise ContractError(
                f"required runtime directory is missing: {identity}"
            )
        return resolved

    def require_value(self, name: str) -> str:
        """Return one required non-path runtime value."""

        identity = resource_identity(name)
        value = self.values.get(identity)
        if value is None:
            raise ContractError(f"required runtime value is missing: {identity}")
        return value

    def require_destination(self, name: str) -> Path:
        """Resolve a mutable store root without traversing or capturing its contents."""
        identity = resource_identity(name)
        value = self.destinations.get(identity)
        if value is None:
            raise ContractError(f"required runtime destination is missing: {identity}")
        path = Path(value).absolute()
        if path.resolve() != path or not path.is_dir():
            raise ContractError(f"runtime destination is missing or unsafe: {identity}")
        return path

    def capture(self, name: str) -> ResourceBinding:
        """Capture one configured identity without guessing its resource kind."""

        identity = resource_identity(name)
        if identity in self.destinations:
            return ResourceBinding.capture(
                self.require_destination(identity), identity=identity, kind="destination"
            )
        if identity in self.tools:
            return ResourceBinding.capture(
                self.require_tool(identity), identity=identity, kind="tool"
            )
        if identity in self.files:
            return ResourceBinding.capture(
                self.require_file(identity), identity=identity, kind="file"
            )
        if identity in self.directories:
            return ResourceBinding.capture(
                self.require_directory(identity), identity=identity, kind="directory"
            )
        if identity in self.values:
            return ResourceBinding.capture_value(
                self.require_value(identity), identity=identity
            )
        raise ContractError(f"runtime resource is not configured: {identity}")

    @property
    def environment_record(self) -> dict[str, str | None]:
        """Identify every fixed or inherited value without persisting its contents."""

        return {
            name: (
                canonical_digest(self.environment[name])
                if name in self.environment
                else None
            )
            for name in sorted({*self.environment, *self.inherit_environment})
        }

    def matches(self, binding: ResourceBinding) -> bool:
        """Return whether this deployment still provides one exact binding."""

        configured = {
            *self.tools,
            *self.files,
            *self.directories,
            *self.destinations,
            *self.values,
        }
        if binding.identity not in configured:
            return binding.kind in {"file", "directory"} and binding.current()
        try:
            current = self.capture(binding.identity)
            return (
                current.location == binding.location
                and current.record == binding.record
                and current._fingerprint == binding._fingerprint
            )
        except (OSError, RuntimeError, ContractError):
            return False

    def for_execution(
        self,
        bindings: tuple[ResourceBinding, ...],
        resource_root: Path | None,
    ) -> "Resources":
        """Bind adapters to planned values, held tools, and sealed data paths."""

        tables: dict[str, dict[str, str]] = {
            "tools": {},
            "files": {},
            "directories": {},
            "destinations": {},
            "values": {},
        }
        for binding in bindings:
            if binding.kind == "destination":
                assert binding.location is not None
                if not binding.current():
                    raise ExecutionError(f"publication destination changed: {binding.identity}")
                tables["destinations"][binding.identity] = str(binding.location)
                continue
            if binding.kind == "value":
                assert binding.value is not None
                tables["values"][binding.identity] = binding.value
                continue
            if binding.kind == "tool":
                assert binding.location is not None
                tables["tools"][binding.identity] = str(binding.location)
                continue
            if resource_root is None:
                raise ContractError("sealed runtime resource root is missing")
            path = resource_root / binding.materialization_key
            table = "files" if binding.kind == "file" else "directories"
            tables[table][binding.identity] = str(path)
        return Resources(
            capabilities=self.capabilities,
            tools=tables["tools"],
            files=tables["files"],
            directories=tables["directories"],
            destinations=tables["destinations"],
            values=tables["values"],
            inherit_environment=self.inherit_environment,
            environment=self.environment,
            _tool_bindings={
                binding.identity: binding
                for binding in bindings
                if binding.kind == "tool"
            },
        )


@dataclass(frozen=True)
class PreflightCheck:
    kind: str
    subject: str
    status: str
    detail: str = ""

    def __post_init__(self) -> None:
        if self.status not in {"ready", "blocked"}:
            raise ContractError(f"invalid preflight status: {self.status!r}")
        if not all(isinstance(value, str) and value for value in (self.kind, self.subject)):
            raise ContractError("preflight check kind and subject must be non-empty")
        if not isinstance(self.detail, str):
            raise ContractError("preflight detail must be text")

    @property
    def record(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "subject": self.subject,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class PreflightResult:
    plan_identity: str
    checks: tuple[PreflightCheck, ...]

    def __post_init__(self) -> None:
        validate_artifact_id(self.plan_identity, "plan identity")
        if not isinstance(self.checks, tuple) or any(
            not isinstance(check, PreflightCheck) for check in self.checks
        ):
            raise ContractError("preflight checks must be PreflightCheck values")

    @property
    def ready(self) -> bool:
        return all(check.status == "ready" for check in self.checks)

    @property
    def status(self) -> str:
        return "ready" if self.ready else "blocked"

    @property
    def record(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "contract_kind": "preflight-result",
            "plan_identity": self.plan_identity,
            "status": self.status,
            "checks": [check.record for check in self.checks],
        }


@dataclass(frozen=True)
class Artifact:
    role: str
    kind: str
    path: Path
    size: int | None = None
    sha256: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", _identifier(self.role, "artifact role"))
        object.__setattr__(self, "kind", adapter_identity(self.kind))
        object.__setattr__(self, "path", Path(self.path).absolute())
        if (self.size is None) != (self.sha256 is None):
            raise ContractError("artifact size and digest must be provided together")
        if self.size is not None and (
            type(self.size) is not int
            or self.size < 0
            or not isinstance(self.sha256, str)
            or len(self.sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.sha256)
        ):
            raise ContractError("artifact size or digest is invalid")

    def read_bytes(self) -> bytes:
        """Read and, for a stored artifact, verify its payload on demand."""

        payload = read_nofollow_bytes(self.path)
        if self.size is not None and (
            len(payload) != self.size
            or hashlib.sha256(payload).hexdigest() != self.sha256
        ):
            raise ContractError("stored artifact payload disagrees with its reference")
        return payload

    def read_text(self, *, encoding: str = "utf-8") -> str:
        """Read verified text without making an eager retained copy."""

        return self.read_bytes().decode(encoding)


@dataclass(frozen=True)
class StepResult:
    status: str
    artifacts: tuple[Artifact, ...] = ()
    message: str = ""

    def __post_init__(self) -> None:
        if self.status not in _STEP_STATUSES:
            raise ContractError(f"invalid step result status: {self.status!r}")
        if not isinstance(self.artifacts, tuple) or any(
            not isinstance(artifact, Artifact) for artifact in self.artifacts
        ):
            raise ContractError("step result artifacts must be Artifact values")
        if self.status in {"blocked", "cancelled"} and self.artifacts:
            raise ContractError("blocked or cancelled steps cannot publish artifacts")
        if not isinstance(self.message, str):
            raise ContractError("step result message must be text")
        if self.status != "succeeded" and not self.message:
            raise ContractError("failed or blocked steps require a message")

    @classmethod
    def succeeded(
        cls,
        *,
        artifacts: tuple[Artifact, ...] = (),
    ) -> "StepResult":
        return cls("succeeded", artifacts)

    @classmethod
    def failed(cls, message: str) -> "StepResult":
        return cls("failed", message=message)

    @classmethod
    def partial(
        cls,
        message: str,
    ) -> "StepResult":
        return cls("partial", message=message)

    @classmethod
    def uncertain(
        cls,
        message: str,
    ) -> "StepResult":
        return cls("uncertain", message=message)

    @classmethod
    def cancelled(cls, message: str) -> "StepResult":
        return cls("cancelled", message=message)


@dataclass(frozen=True)
class ExecutionIO:
    """Deep managed-I/O interface supplied to one trusted Adapter."""

    plan_identity: str
    step: Step
    run_id: str
    operation_id: str
    _run_root: Path
    _resources: Resources
    _dependencies: Mapping[str, StepResult]
    owner: str
    _source_scopes: Mapping[str, str] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )
    _resource_digests: Mapping[str, str] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )
    _resource_kinds: Mapping[str, str] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )
    _register_mutation: Callable[[Any], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _source_paths: Mapping[str, Path] = field(
        init=False,
        repr=False,
        compare=False,
    )
    _scoped_source_paths: Mapping[tuple[str, str], Path] = field(
        init=False,
        repr=False,
        compare=False,
    )
    _resource_paths: Mapping[str, Path] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.step, Step):
            raise ContractError("execution I/O requires a Step")
        object.__setattr__(self, "owner", _identifier(self.owner, "execution owner"))
        validate_artifact_id(self.run_id, "run id")
        validate_artifact_id(self.operation_id, "operation id")
        validate_artifact_id(self.plan_identity, "plan identity")
        object.__setattr__(self, "_run_root", Path(self._run_root).absolute())
        if not isinstance(self._source_scopes, Mapping) or any(
            name not in self.step.sources or scope not in {"owner", "project"}
            for name, scope in self._source_scopes.items()
        ):
            raise ContractError("execution I/O source scopes disagree with its Step")
        if not isinstance(self._dependencies, Mapping) or any(
            not isinstance(name, str) or not isinstance(result, StepResult)
            for name, result in self._dependencies.items()
        ):
            raise ContractError("step dependencies must map names to StepResult values")
        if set(self._dependencies) != set(self.step.needs):
            raise ContractError("execution I/O dependency closure disagrees with the plan")
        run_root = self._run_root
        if run_root == Path(run_root.anchor):
            raise ContractError("execution I/O run root cannot be a filesystem root")
        if (
            not isinstance(self._resource_digests, Mapping)
            or set(self._resource_digests) != set(self.step.resources)
            or any(
                not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                for digest in self._resource_digests.values()
            )
        ):
            raise ContractError(
                "execution I/O resource digests disagree with its resource closure"
            )
        if (
            not isinstance(self._resource_kinds, Mapping)
            or set(self._resource_kinds) != set(self.step.resources)
            or any(
                kind not in {"tool", "file", "directory", "value", "destination"}
                for kind in self._resource_kinds.values()
            )
        ):
            raise ContractError(
                "execution I/O resource kinds disagree with its resource closure"
            )
        object.__setattr__(
            self, "_dependencies", MappingProxyType(dict(self._dependencies))
        )
        object.__setattr__(
            self,
            "_source_scopes",
            MappingProxyType(dict(self._source_scopes)),
        )
        object.__setattr__(
            self,
            "_resource_digests",
            MappingProxyType(dict(self._resource_digests)),
        )
        object.__setattr__(
            self,
            "_resource_kinds",
            MappingProxyType(dict(self._resource_kinds)),
        )
        source_paths = {
            name: self.source_directory.joinpath(*PurePosixPath(name).parts)
            for name in self.step.sources
        }
        object.__setattr__(self, "_source_paths", MappingProxyType(source_paths))
        object.__setattr__(
            self,
            "_scoped_source_paths",
            MappingProxyType(
                {
                    (scope, name): source_paths[name]
                    for name, scope in self._source_scopes.items()
                }
            ),
        )
        resource_root = self.resource_directory
        object.__setattr__(
            self,
            "_resource_paths",
            MappingProxyType(
                {
                    name: resource_root / resource_materialization_key(name)
                    for name, kind in self._resource_kinds.items()
                    if kind in {"file", "directory"}
                    and resource_root is not None
                }
            ),
        )

    @property
    def work_directory(self) -> Path:
        """Return the Adapter's managed scratch directory."""

        return self._run_root / "work" / self.step.id

    @property
    def output_directory(self) -> Path:
        """Return the root below which the Adapter may publish artifacts."""

        return self._run_root / "outputs" / self.step.id

    @property
    def source_directory(self) -> Path:
        """Return the immutable source closure root."""

        return self._run_root / "inputs" / "sources"

    @property
    def resource_directory(self) -> Path | None:
        """Return the immutable data-resource root when the Step has one."""

        if any(
            kind in {"file", "directory"}
            for kind in self._resource_kinds.values()
        ):
            return self._run_root / "inputs" / "resources"
        return None

    @property
    def runtime(self) -> Resources:
        """Return the plan-filtered runtime deployment."""

        return self._resources

    def source_path(self, source: str) -> Path:
        """Return a run-local tool path for trusted package adapter code."""

        name = source
        relative = PurePosixPath(name)
        if (
            not name
            or relative.is_absolute()
            or "\\" in name
            or relative.as_posix() != name
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ExecutionError(f"source path must be canonical and relative: {name!r}")
        result = self._source_paths.get(name)
        if result is None:
            raise ExecutionError(f"source is outside this step: {name!r}")
        if (
            result.absolute() != result
            or result.resolve() != result
            or not result.is_file()
            or result.is_symlink()
        ):
            raise ExecutionError(f"sealed source is missing or unsafe: {name!r}")
        return result

    def source_text(self, source: str) -> str:
        """Read a step source through the held-fd no-follow input primitive."""

        return read_nofollow_text(self.source_path(source))

    def resource_path(self, resource: str) -> Path:
        """Return one sealed external resource selected by this Step."""

        name = resource_identity(resource)
        if self._resource_kinds.get(name) in {"tool", "value", "destination"}:
            raise ExecutionError(f"external resource is not sealed data: {name!r}")
        result = self._resource_paths.get(name)
        if result is None:
            raise ExecutionError(f"external resource is outside this step: {name!r}")
        expected_kind = self._resource_kinds[name]
        try:
            metadata = result.stat(follow_symlinks=False)
        except OSError as exc:
            raise ExecutionError(
                f"sealed external resource is missing or unsafe: {name!r}"
            ) from exc
        if (
            result.absolute() != result
            or result.resolve() != result
            or result.is_symlink()
            or (
                expected_kind == "file"
                and not stat.S_ISREG(metadata.st_mode)
            )
            or (
                expected_kind == "directory"
                and not stat.S_ISDIR(metadata.st_mode)
            )
        ):
            raise ExecutionError(
                f"sealed external resource is missing or unsafe: {name!r}"
            )
        return result

    def resource_text(self, resource: str) -> str:
        try:
            return self.resource_bytes(resource).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ExecutionError(
                f"sealed external resource is not UTF-8: {resource!r}"
            ) from exc

    def resource_bytes(self, resource: str) -> bytes:
        name = resource_identity(resource)
        if self._resource_kinds.get(name) != "file":
            raise ExecutionError(f"sealed external resource is not a file: {name!r}")
        data = read_nofollow_bytes(self.resource_path(name))
        if hashlib.sha256(data).hexdigest() != self._resource_digests[name]:
            raise ExecutionError(
                f"sealed external resource identity drift: {name!r}"
            )
        return data

    def scoped_source_path(self, scope: str, source: str) -> Path:
        """Resolve one owner- or project-relative source from the sealed closure."""

        result = self._scoped_source_paths.get((scope, source))
        if result is None:
            raise ExecutionError(
                f"step source {scope}:{source} is missing or ambiguous"
            )
        return self.source_path(source)

    def owner_source_path(self, source: str) -> Path:
        return self.scoped_source_path("owner", source)

    def register_mutation(self, operation: Any) -> None:
        """Attach one trusted mutation journal to this managed run."""

        if self._register_mutation is None:
            raise ExecutionError("execution I/O cannot register a mutation")
        if getattr(operation, "operation_id", None) != self.operation_id:
            raise ExecutionError("mutation identity disagrees with this run")
        self._register_mutation(operation)

    def output_path(self, role: str, filename: str) -> Path:
        """Return a managed output path through the workflow workspace."""

        relative = PurePosixPath(filename)
        if (
            relative.is_absolute()
            or "\\" in filename
            or relative.as_posix() != filename
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ExecutionError(f"output filename must be canonical and relative: {filename!r}")
        return self.workspace(role, {}).path("outputs", *relative.parts)

    def write_text(self, role: str, filename: str, value: str) -> Path:
        """Create one immutable text output without following path components."""

        relative = PurePosixPath(filename)
        self.output_path(role, filename)
        return self.workspace(role, {}).write_text("outputs", relative.parts, value)

    def copy_output(
        self,
        role: str,
        kind: str,
        source: Path,
        filename: str,
    ) -> Artifact:
        """Publish one immutable regular file from tool scratch space."""

        if not source.is_file() or source.is_symlink():
            raise ExecutionError(f"tool omitted required {role!r} output")
        destination = self.output_path(role, filename)
        copy_immutable_file(source, destination)
        return Artifact(role, kind, destination)

    def output_artifacts(
        self,
        role: str,
        kind: str,
        *,
        required: bool = False,
    ) -> tuple[Artifact, ...]:
        """Publish the complete regular-file closure below one output role."""

        root = self.output_directory / validate_artifact_component(
            role, "output role"
        )
        if not root.is_dir() or root.is_symlink():
            if required:
                raise ExecutionError(f"tool omitted required {role!r} directory")
            return ()
        artifacts = tuple(
            Artifact(role, kind, path.absolute())
            for path in sorted(root.rglob("*"))
            if path.is_file() and not path.is_symlink()
        )
        if required and not artifacts:
            raise ExecutionError(f"tool produced an empty {role!r} directory")
        return artifacts

    def workspace(
        self,
        output_role: str,
        source: Mapping[str, Any],
        *,
        tool_work_root: Path | None = None,
    ) -> "ExecutionWorkspace":
        """Create the file view owned by this Step."""

        from sigilicon.execution._workspace import ExecutionWorkspace

        role = validate_artifact_component(output_role, "output role")
        return ExecutionWorkspace(
            run_id=self.run_id,
            root=self._run_root,
            input_root=self.work_directory / "inputs",
            work_root=(
                self.work_directory / "tool"
                if tool_work_root is None
                else Path(tool_work_root).absolute()
            ),
            output_root=self.output_directory / role,
            log_root=self.work_directory / "logs",
            source=source,
        )

    def artifacts(self, dependency: str, role: str | None = None) -> tuple[Artifact, ...]:
        try:
            result = self._dependencies[dependency]
        except KeyError as exc:
            raise ExecutionError(f"step {self.step.id!r} has no dependency {dependency!r}") from exc
        return tuple(
            artifact
            for artifact in result.artifacts
            if role is None or artifact.role == role
        )


@dataclass(frozen=True)
class StepOutcome:
    step: str
    adapter: str
    result: StepResult

    def __post_init__(self) -> None:
        object.__setattr__(self, "step", _identifier(self.step, "outcome step"))
        object.__setattr__(self, "adapter", adapter_identity(self.adapter))
        if not isinstance(self.result, StepResult):
            raise ContractError("step outcome requires a StepResult")


@dataclass(frozen=True)
class RunResult:
    owner: str
    operation: str
    variant: str | None
    run_id: str
    operation_id: str
    plan_identity: str
    status: str
    outcomes: tuple[StepOutcome, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "owner", _identifier(self.owner, "run owner"))
        object.__setattr__(self, "operation", _identifier(self.operation, "run operation"))
        if self.variant is not None:
            object.__setattr__(self, "variant", _identifier(self.variant, "run variant"))
        if self.status not in _RUN_STATUSES:
            raise ContractError(f"invalid run status: {self.status!r}")
        validate_artifact_id(self.run_id, "run id")
        validate_artifact_id(self.operation_id, "operation id")
        validate_artifact_id(self.plan_identity, "plan identity")
        if not isinstance(self.outcomes, tuple) or not self.outcomes or any(
            not isinstance(outcome, StepOutcome) for outcome in self.outcomes
        ):
            raise ContractError("run outcomes must be a non-empty StepOutcome tuple")
        if len({outcome.step for outcome in self.outcomes}) != len(self.outcomes):
            raise ContractError("run outcomes contain duplicate steps")
        step_statuses = {outcome.result.status for outcome in self.outcomes}
        if step_statuses == {"succeeded"}:
            expected_status = "succeeded"
        elif "uncertain" in step_statuses:
            expected_status = "uncertain"
        elif "partial" in step_statuses:
            expected_status = "partial"
        elif "cancelled" in step_statuses:
            expected_status = "cancelled"
        else:
            expected_status = "failed"
        if self.status != expected_status:
            raise ContractError("run status disagrees with its step outcomes")
        for outcome in self.outcomes:
            for artifact in outcome.result.artifacts:
                _run_artifact_path(outcome.step, artifact.path)

    @property
    def record(self) -> dict[str, Any]:
        return {
            "schema": 3,
            "contract_kind": "run-result",
            "owner": self.owner,
            "operation": self.operation,
            "variant": self.variant,
            "run_id": self.run_id,
            "operation_id": self.operation_id,
            "plan_identity": self.plan_identity,
            "status": self.status,
            "steps": [
                {
                    "id": outcome.step,
                    "uses": outcome.adapter,
                    "status": outcome.result.status,
                    "message": outcome.result.message,
                    "artifacts": [
                        {
                            "role": artifact.role,
                            "kind": artifact.kind,
                            "path": _run_artifact_path(
                                outcome.step,
                                artifact.path,
                            ),
                        }
                        for artifact in outcome.result.artifacts
                    ],
                }
                for outcome in self.outcomes
            ],
        }


def _run_artifact_path(step: str, path: Path) -> str:
    """Recover the canonical stored reference without retaining a run root."""

    parts = Path(path).absolute().parts
    positions = tuple(
        index
        for index in range(len(parts) - 2)
        if parts[index : index + 2] == ("outputs", step)
    )
    if not positions:
        raise ContractError(
            f"step {step!r} published outside its managed output root"
        )
    relative = PurePosixPath(*parts[positions[-1] :])
    if len(relative.parts) < 3:
        raise ContractError(f"step {step!r} published no artifact filename")
    return relative.as_posix()


@dataclass(frozen=True)
class RunFailureProvenance:
    """Structured progress and uncertainty for a terminal run failure."""

    completed_steps: tuple[str, ...] = ()
    uncertain_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.completed_steps, tuple):
            raise ContractError("run failure completed steps must be a tuple")
        steps = tuple(
            _identifier(step, "completed run step")
            for step in self.completed_steps
        )
        if len(steps) != len(set(steps)):
            raise ContractError("run failure completed steps contain duplicates")
        object.__setattr__(self, "completed_steps", steps)
        if self.uncertain_reason is not None and (
            not isinstance(self.uncertain_reason, str)
            or not self.uncertain_reason
        ):
            raise ContractError(
                "run failure uncertainty reason must be non-empty text or None"
            )

    @classmethod
    def from_manifest(
        cls,
        partial_failure: object,
        uncertain_reason: object,
    ) -> "RunFailureProvenance":
        if partial_failure is None:
            completed_steps: object = ()
        elif not isinstance(partial_failure, Mapping):
            raise ContractError(
                "run failure partial provenance must be an object or null"
            )
        else:
            if set(partial_failure) != {"completed_steps"}:
                raise ContractError(
                    "run failure partial provenance fields are invalid"
                )
            completed_steps = partial_failure.get("completed_steps")
        if not isinstance(completed_steps, (list, tuple)):
            raise ContractError("run failure completed_steps must be an array")
        return cls(tuple(completed_steps), uncertain_reason)

    @property
    def record(self) -> dict[str, Any]:
        return {
            "completed_steps": list(self.completed_steps),
            "uncertain_reason": self.uncertain_reason,
        }


@dataclass(frozen=True)
class RunFailure:
    """Typed terminal record for a run that failed before producing RunResult."""

    owner: str
    operation: str
    variant: str | None
    run_id: str
    operation_id: str | None
    plan_identity: str
    status: str
    error_type: str
    message: str
    provenance: RunFailureProvenance = field(
        default_factory=RunFailureProvenance
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "owner", _identifier(self.owner, "run owner"))
        object.__setattr__(self, "operation", _identifier(self.operation, "run operation"))
        if self.variant is not None:
            object.__setattr__(self, "variant", _identifier(self.variant, "run variant"))
        validate_artifact_id(self.run_id, "run id")
        validate_artifact_id(self.plan_identity, "plan identity")
        if self.operation_id is not None:
            validate_artifact_id(self.operation_id, "operation id")
        if self.status not in _RUN_FAILURE_STATUSES:
            raise ContractError(f"invalid run failure status: {self.status!r}")
        if not isinstance(self.error_type, str) or not self.error_type:
            raise ContractError("run failure error type must be non-empty text")
        if not isinstance(self.message, str):
            raise ContractError("run failure message must be text")
        if not isinstance(self.provenance, RunFailureProvenance):
            raise ContractError("run failure provenance must be RunFailureProvenance")
        if self.status == "failed" and (
            self.provenance.completed_steps
            or self.provenance.uncertain_reason is not None
        ):
            raise ContractError("failed run cannot contain failure provenance")
        if self.status == "partial":
            if not self.provenance.completed_steps:
                raise ContractError("partial run failure requires completed steps")
            if self.provenance.uncertain_reason is not None:
                raise ContractError(
                    "partial run failure cannot contain uncertainty reason"
                )
        elif self.status == "uncertain":
            if self.provenance.uncertain_reason is None:
                raise ContractError(
                    "uncertain run failure requires uncertainty reason"
                )
        elif self.provenance.uncertain_reason is not None:
            raise ContractError(
                "uncertainty reason requires an uncertain run failure"
            )

    @property
    def record(self) -> dict[str, Any]:
        return {
            "schema": 3,
            "contract_kind": "run-failure",
            "owner": self.owner,
            "operation": self.operation,
            "variant": self.variant,
            "run_id": self.run_id,
            "operation_id": self.operation_id,
            "plan_identity": self.plan_identity,
            "status": self.status,
            "error": {"type": self.error_type, "message": self.message},
            "provenance": self.provenance.record,
        }
