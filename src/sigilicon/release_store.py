"""Content-addressed storage for immutable cross-owner release packages."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
from types import MappingProxyType
from typing import Any

from sigilicon.artifacts import _inspect_nofollow_file, _read_nofollow_bytes
from sigilicon.paths import validate_artifact_component, validate_artifact_id


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class ReleaseRef:
    """Immutable release locator recorded by a dependency lock."""

    store: str
    object: str
    manifest_sha256: str

    def __post_init__(self) -> None:
        validate_artifact_component(self.store, "release store")
        validate_artifact_id(self.object, "release object")
        if self.object != f"sha256-{self.manifest_sha256}" or _SHA256.fullmatch(
            self.manifest_sha256
        ) is None:
            raise ValueError(
                "release object must be the content address of its manifest"
            )


@dataclass(frozen=True)
class ReleaseArtifact:
    """One digest-bound role in an audited release package."""

    export: str
    role: str
    path: Path
    relative_path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class AuditedRelease:
    """A release whose locator, inventory, and payloads have been audited."""

    ref: ReleaseRef
    manifest_path: Path
    manifest: Mapping[str, Any]
    artifacts: tuple[ReleaseArtifact, ...]

    def role(self, export: str, role: str) -> ReleaseArtifact:
        matches = tuple(
            artifact
            for artifact in self.artifacts
            if artifact.export == export and artifact.role == role
        )
        if len(matches) != 1:
            raise RuntimeError(
                f"release role is missing or ambiguous: {export}/{role}"
            )
        artifact = matches[0]
        metadata, digest = _inspect_nofollow_file(artifact.path)
        if metadata.st_size != artifact.size or digest != artifact.sha256:
            raise RuntimeError(
                f"release role content changed after audit: {export}/{role}"
            )
        return artifact


class ReleaseStore:
    """Open exact immutable packages without exposing storage layout to consumers."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).absolute()

    @classmethod
    def from_artifact_root(cls, artifact_root: Path) -> "ReleaseStore":
        return cls(Path(artifact_root).absolute() / "release-store")

    def object_root(self, ref: ReleaseRef) -> Path:
        result = self.root / ref.store / "objects" / ref.object
        if result.absolute() != result or result.resolve() != result:
            raise RuntimeError("release store path traverses a symlink")
        return result

    def open(
        self,
        ref: ReleaseRef,
        *,
        validate: Callable[[Path], Mapping[str, Any]] | None = None,
    ) -> AuditedRelease:
        """Audit one object, optionally apply domain validation, then re-audit it."""

        release = self._audit(ref)
        if validate is not None:
            validated = validate(release.manifest_path)
            if dict(validated) != dict(release.manifest):
                raise RuntimeError("release validator changed the manifest projection")
            release = self._audit(ref)
        return release

    def _audit(self, ref: ReleaseRef) -> AuditedRelease:
        root = self.object_root(ref)
        manifest_path = root / "manifest.json"
        try:
            manifest_bytes = _read_nofollow_bytes(manifest_path)
        except (OSError, RuntimeError) as exc:
            raise FileNotFoundError(
                f"release store {ref.store!r} has no object {ref.object!r}"
            ) from exc
        if hashlib.sha256(manifest_bytes).hexdigest() != ref.manifest_sha256:
            raise RuntimeError("release store object digest disagrees with its lock")
        try:
            manifest = json.loads(manifest_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("release store manifest is invalid JSON") from exc
        if not isinstance(manifest, dict):
            raise RuntimeError("release store manifest must be a JSON object")
        if (
            manifest.get("schema") != 2
            or manifest.get("contract_kind") != "ip-release-manifest"
            or manifest.get("release_kind") != "source-package"
        ):
            raise RuntimeError("release store manifest identity is invalid")
        views = manifest.get("views")
        if not isinstance(views, list) or not views:
            raise RuntimeError("release store manifest has no views")
        artifacts: list[ReleaseArtifact] = []
        identities: set[tuple[str, str]] = set()
        expected_files = {PurePosixPath("manifest.json")}
        for row in views:
            if not isinstance(row, Mapping):
                raise RuntimeError("release store view entry is invalid")
            export = row.get("export")
            role = row.get("role")
            relative_text = row.get("path")
            digest = row.get("sha256")
            size = row.get("size")
            if (
                not isinstance(export, str)
                or not export
                or not isinstance(role, str)
                or not role
                or not isinstance(relative_text, str)
                or not isinstance(digest, str)
                or _SHA256.fullmatch(digest) is None
                or type(size) is not int
                or size < 0
            ):
                raise RuntimeError("release store view metadata is invalid")
            identity = (export, role)
            if identity in identities:
                raise RuntimeError(
                    f"release store contains duplicate role: {export}/{role}"
                )
            identities.add(identity)
            relative = PurePosixPath(relative_text)
            if (
                relative.is_absolute()
                or relative.as_posix() != relative_text
                or any(part in {"", ".", ".."} for part in relative.parts)
            ):
                raise RuntimeError("release store view path is unsafe")
            path = root.joinpath(*relative.parts)
            if path.absolute() != path or path.resolve() != path:
                raise RuntimeError("release store view traverses a symlink")
            try:
                metadata, actual_digest = _inspect_nofollow_file(path)
            except (OSError, RuntimeError) as exc:
                raise RuntimeError(
                    f"release store view is missing or unsafe: {relative_text}"
                ) from exc
            if metadata.st_size != size or actual_digest != digest:
                raise RuntimeError(
                    f"release store view content drifted: {relative_text}"
                )
            artifacts.append(
                ReleaseArtifact(export, role, path, relative_text, digest, size)
            )
            expected_files.add(relative)
        actual_files: set[PurePosixPath] = set()
        for path in root.rglob("*"):
            metadata = path.stat(follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                raise RuntimeError("release store object cannot contain symlinks")
            if stat.S_ISREG(metadata.st_mode):
                actual_files.add(PurePosixPath(path.relative_to(root).as_posix()))
            elif not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeError("release store object contains an unsupported entry")
        if actual_files != expected_files:
            raise RuntimeError("release store inventory disagrees with its manifest")
        return AuditedRelease(
            ref,
            manifest_path,
            MappingProxyType(manifest),
            tuple(artifacts),
        )


__all__ = ["AuditedRelease", "ReleaseArtifact", "ReleaseRef", "ReleaseStore"]
