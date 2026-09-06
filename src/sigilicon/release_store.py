"""Content-addressed storage for immutable cross-owner release packages."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any

from sigilicon.artifacts import (
    SafeTree,
    _inspect_nofollow_file,
    read_json_object,
)
from sigilicon.paths import validate_artifact_component, validate_artifact_id
from sigilicon.release_views import ViewSelector, view_condition


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class ReleaseRef:
    """Immutable release locator recorded by a dependency lock."""

    store: str
    manifest_sha256: str

    def __post_init__(self) -> None:
        validate_artifact_component(self.store, "release store")
        if _SHA256.fullmatch(self.manifest_sha256) is None:
            raise ValueError("release manifest digest must be SHA-256")


def _release_object_name(ref: ReleaseRef) -> str:
    return validate_artifact_id(
        f"sha256-{ref.manifest_sha256}",
        "release object",
    )


def release_store_resource(store: str) -> str:
    """Return the deployment resource identity for one named release store."""

    name = validate_artifact_component(store, "release store")
    return f"release-store.{name}"


@dataclass(frozen=True)
class ReleaseArtifact:
    """One named, digest-bound view in an audited release package."""

    export: str
    name: str
    role: str
    path: Path
    relative_path: str
    sha256: str
    size: int
    variant: str | None
    condition: Mapping[str, str | int | float | bool]


@dataclass(frozen=True)
class ReleasePackage:
    """One audited release manifest and its exact payload closure."""

    manifest_path: Path
    manifest: Mapping[str, Any]
    artifacts: tuple[ReleaseArtifact, ...]
    ref: ReleaseRef | None = None

    def view(self, export: str, name: str) -> ReleaseArtifact:
        matches = tuple(
            artifact
            for artifact in self.artifacts
            if artifact.export == export and artifact.name == name
        )
        if len(matches) != 1:
            raise RuntimeError(
                f"release view is missing or ambiguous: {export}/{name}"
            )
        artifact = matches[0]
        metadata, digest = _inspect_nofollow_file(artifact.path)
        if metadata.st_size != artifact.size or digest != artifact.sha256:
            raise RuntimeError(
                f"release view content changed after audit: {export}/{name}"
            )
        return artifact

    def select(self, export: str, selector: ViewSelector) -> ReleaseArtifact:
        matches = tuple(artifact for artifact in self.artifacts if artifact.export == export
                        and selector.matches(role=artifact.role, variant=artifact.variant, condition=artifact.condition))
        if len(matches) != 1:
            raise RuntimeError(f"release view selection is missing or ambiguous: {export}/{selector.role}")
        return self.view(export, matches[0].name)


def _audit_release_package(
    manifest_path: Path,
    *,
    manifest_sha256: str | None = None,
) -> ReleasePackage:
    """Audit the common release schema, paths, digests, and exact inventory."""

    path = Path(manifest_path).absolute()
    if path.name != "manifest.json":
        raise RuntimeError("release package manifest must be named manifest.json")
    tree = SafeTree(path.parent)
    try:
        tree.file("manifest.json", "release package manifest")
    except (OSError, RuntimeError) as exc:
        raise FileNotFoundError(f"release package manifest is missing: {path}") from exc
    manifest = read_json_object(
        path,
        "release package manifest",
        sha256=manifest_sha256,
    )
    if (
        manifest.get("schema") != 4
        or manifest.get("contract_kind") != "ip-release-manifest"
        or manifest.get("release_kind") not in {"source-package", "build-artifact-package"}
    ):
        raise RuntimeError("release package manifest identity is invalid")
    raw_exports = manifest.get("exports")
    if not isinstance(raw_exports, list) or not raw_exports:
        raise RuntimeError("release package manifest has no exports")
    exports: set[str] = set()
    for row in raw_exports:
        name = row.get("name") if isinstance(row, Mapping) else None
        if not isinstance(name, str) or not name or name in exports:
            raise RuntimeError("release package export identities are invalid")
        exports.add(name)
    views = manifest.get("views")
    if not isinstance(views, list) or not views:
        raise RuntimeError("release package manifest has no views")
    artifacts: list[ReleaseArtifact] = []
    identities: set[tuple[str, str]] = set()
    expected_files = {Path("manifest.json")}
    for row in views:
        if not isinstance(row, Mapping):
            raise RuntimeError("release package view entry is invalid")
        export = row.get("export")
        name = row.get("name")
        role = row.get("role")
        variant = row.get("variant")
        try:
            condition = view_condition(row.get("condition", {}))
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        relative_text = row.get("path")
        digest = row.get("sha256")
        size = row.get("size")
        if (
            not isinstance(export, str)
            or export not in exports
            or not isinstance(name, str) or not name
            or (variant is not None and (not isinstance(variant, str) or not variant))
            or not isinstance(role, str)
            or not role
            or not isinstance(relative_text, str)
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            or type(size) is not int
            or size < 0
        ):
            raise RuntimeError("release package view metadata is invalid")
        identity = (export, name)
        if identity in identities:
            raise RuntimeError(
                f"release package contains duplicate view: {export}/{name}"
            )
        identities.add(identity)
        try:
            file = tree.file(relative_text, "release package view path")
        except (OSError, RuntimeError) as exc:
            raise RuntimeError(
                f"release package view is missing, symlinked, or unsafe: {relative_text}"
            ) from exc
        if file.size != size or file.sha256 != digest:
            raise RuntimeError(
                f"release package view content drifted: {relative_text}"
            )
        artifacts.append(
            ReleaseArtifact(export, name, role, file.path, relative_text, digest, size, variant, condition)
        )
        expected_files.add(file.relative)
    expected_directories = {
        parent
        for file in expected_files
        for parent in file.parents
        if parent != Path(".")
    }
    try:
        inventory = tree.inventory()
        actual_files = set(inventory.files)
        actual_directories = set(inventory.directories)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(f"release package is unsafe: {exc}") from exc
    if (
        actual_files != expected_files
        or actual_directories != expected_directories
    ):
        raise RuntimeError("release package inventory disagrees with its manifest")
    return ReleasePackage(
        path,
        MappingProxyType(manifest),
        tuple(artifacts),
    )


def audit_release_package(
    manifest_path: Path,
    *,
    manifest_sha256: str | None = None,
    validate: Callable[[ReleasePackage], None] | None = None,
) -> ReleasePackage:
    """Audit structure and optional domain semantics against one stable package."""

    package = _audit_release_package(
        manifest_path,
        manifest_sha256=manifest_sha256,
    )
    if validate is None:
        return package
    validate(package)
    checked = _audit_release_package(
        manifest_path,
        manifest_sha256=manifest_sha256,
    )
    if dict(checked.manifest) != dict(package.manifest):
        raise RuntimeError("release changed during domain validation")
    return checked


class ReleaseStore:
    """Open exact immutable packages without exposing storage layout to consumers."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).absolute()

    def object_root(self, ref: ReleaseRef) -> Path:
        result = self.root / ref.store / "objects" / _release_object_name(ref)
        if result.absolute() != result or result.resolve() != result:
            raise RuntimeError("release store path traverses a symlink")
        return result

    def publish(
        self, store: str, manifest: Mapping[str, Any], payloads: Mapping[str, Path],
        *, validate: Callable[[ReleasePackage], None] | None = None,
    ) -> ReleasePackage:
        """Install a fully audited package atomically; existing objects stay immutable."""

        import hashlib
        import os
        import uuid
        from sigilicon.artifacts import atomic_write_json, copy_immutable_file
        from sigilicon.external_tools import owned_directory

        validate_artifact_component(store, "release store")
        namespace = self.root / store / "objects"
        with owned_directory(namespace, create_missing=True) as held:
            temporary_name = f".publish-{uuid.uuid4().hex}"
            os.mkdir(temporary_name, dir_fd=held.fd)
            temporary = namespace / temporary_name
            installed = False
            try:
                views = manifest.get("views", [])
                expected = {view["path"]: view for view in views}
                if set(expected) != set(payloads) or len(expected) != len(views):
                    raise ValueError("release publication payload closure disagrees with its views")
                from sigilicon.contracts import require_relative_path
                for name, source in payloads.items():
                    relative = require_relative_path(name, "release view path")
                    row = expected[name]
                    copy_immutable_file(source, temporary / relative,
                                        expected_size=row["size"], expected_sha256=row["sha256"])
                atomic_write_json(temporary / "manifest.json", dict(manifest))
                package = audit_release_package(temporary / "manifest.json", validate=validate)
                digest = hashlib.sha256(package.manifest_path.read_bytes()).hexdigest()
                reference = ReleaseRef(store, digest)
                target = _release_object_name(reference)
                try:
                    os.stat(target, dir_fd=held.fd, follow_symlinks=False)
                except FileNotFoundError:
                    SafeTree(temporary).make_readonly()
                    os.rename(temporary_name, target, src_dir_fd=held.fd, dst_dir_fd=held.fd)
                    installed = True
            finally:
                if not installed:
                    tree = SafeTree(temporary)
                    tree.remove(os.stat(temporary_name, dir_fd=held.fd, follow_symlinks=False))
        return self.open(reference, validate=validate)

    def open(
        self,
        ref: ReleaseRef,
        *,
        validate: Callable[[ReleasePackage], None] | None = None,
    ) -> ReleasePackage:
        """Audit one object, apply domain validation, then prove it stayed stable."""

        root = self.object_root(ref)
        object_name = _release_object_name(ref)
        try:
            package = audit_release_package(
                root / "manifest.json",
                manifest_sha256=ref.manifest_sha256,
                validate=validate,
            )
        except FileNotFoundError as exc:
            raise FileNotFoundError(
                f"release store {ref.store!r} has no object {object_name!r}"
            ) from exc
        except (OSError, RuntimeError) as exc:
            if not root.is_dir():
                raise FileNotFoundError(
                    f"release store {ref.store!r} has no object {object_name!r}"
                ) from exc
            raise
        return replace(package, ref=ref)


__all__ = [
    "ReleaseArtifact",
    "ReleasePackage",
    "ReleaseRef",
    "ReleaseStore",
    "audit_release_package",
    "release_store_resource",
]
