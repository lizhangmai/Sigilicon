"""Capture and validate component-qualified source content."""

from __future__ import annotations
from dataclasses import dataclass, field
import hashlib
from pathlib import Path, PurePosixPath
import stat
import tomllib
from typing import Any, Mapping
from sigilicon.artifacts import _inspect_nofollow_file, read_nofollow_bytes
from sigilicon.canonical import canonical_json
from sigilicon.source import SourceReference
from sigilicon.execution._values import ContractError, _ADAPTER, json_value


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
    reference: SourceReference | None = None

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
        if self.reference is not None and not isinstance(self.reference, SourceReference):
            raise ContractError("source reference must identify a component source")
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

    @classmethod
    def capture_document(
        cls, path: Path, *, document: Mapping[str, Any], root: Path, scope: str = "project",
    ) -> "Source":
        """Bind captured TOML bytes to the document used to compile an action."""

        captured = cls.capture(path, root=root, scope=scope)
        try:
            parsed = tomllib.loads(captured.read_text())
        except tomllib.TOMLDecodeError as exc:
            raise ContractError(f"source document snapshot drift: {path}") from exc
        if canonical_json(json_value(parsed)) != canonical_json(json_value(document)):
            raise ContractError(f"source document snapshot drift: {path}")
        return captured

    @property
    def record(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "executable": self.executable,
            "reference": None if self.reference is None else self.reference.record,
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
