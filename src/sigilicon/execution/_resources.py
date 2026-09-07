"""Capture, bind and validate runtime resources by their declared purpose."""

from __future__ import annotations
from dataclasses import dataclass, field
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
from types import MappingProxyType
from typing import Any, Iterator, Mapping
from sigilicon.artifacts import (
    _inspect_nofollow_file,
    _open_nofollow_directory,
    read_nofollow_bytes,
)
from sigilicon.canonical import canonical_digest, canonical_json
from sigilicon.execution._values import (
    ContractError,
    ExecutionError,
    _ADAPTER,
    _ENVIRONMENT,
    resource_identity,
    resource_materialization_key,
)
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from sigilicon.external_tools import OwnedExecutable


def _require_nofollow_path(
    value: str | None,
    identity: str,
    kind: str,
) -> Path:
    """Resolve one configured file or directory without following symlinks."""

    if value is None:
        raise ContractError(f"required runtime {kind} is missing: {identity}")
    path = Path(value).absolute()
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ContractError(
            f"required runtime {kind} is missing or unsafe: {identity}"
        ) from exc
    valid = resolved.is_file() if kind == "file" else resolved.is_dir()
    if path != resolved or not valid:
        raise ContractError(
            f"required runtime {kind} is missing or unsafe: {identity}"
        )
    return path


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
        return _require_nofollow_path(self.files.get(identity), identity, "file")

    def require_directory(self, name: str) -> Path:
        """Return one required project-configured directory."""

        identity = resource_identity(name)
        return _require_nofollow_path(
            self.directories.get(identity), identity, "directory"
        )

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
