"""Lifecycle, state machine, and atomic I/O for persistent artifacts."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from sigilicon.paths import (
    ArtifactExecutionPaths,
    operation_incident_reference,
    validate_artifact_component,
    validate_artifact_id,
)


ARTIFACT_STATUSES = frozenset(
    {"running", "succeeded", "failed", "partial", "uncertain", "cancelled"}
)
TERMINAL_STATUSES = ARTIFACT_STATUSES - {"running"}
_EXECUTION_ROLES = frozenset({"inputs", "work", "outputs", "logs"})
ARTIFACT_ROLES = {
    kind: _EXECUTION_ROLES
    for kind in (
        "design_sync",
        "oa_text_view",
        "layout_generation",
        "physical_verification",
        "netlist_export",
        "standalone_simulation",
        "oa_maestro_simulation",
        "netlist_import",
        "analysis",
        "execution-run",
    )
}
ARTIFACT_IDENTITY_KINDS = {
    "design_sync": "attempt_id",
    "oa_text_view": "attempt_id",
    "layout_generation": "attempt_id",
    "physical_verification": "run_id",
    "netlist_export": "run_id",
    "standalone_simulation": "run_id",
    "oa_maestro_simulation": "run_id",
    "netlist_import": "attempt_id",
    "analysis": "run_id",
    "execution-run": "run_id",
}
ARTIFACT_ENTITY_FIELDS = {
    "design_sync": ({"library", "cell"}, {"library", "cell"}),
    "oa_text_view": (
        {"library", "cell", "view"},
        {"library", "cell", "view"},
    ),
    "layout_generation": (
        {"library", "cell", "view"},
        {"library", "cell", "view"},
    ),
    "physical_verification": (
        {"library", "cell", "view", "check"},
        {"library", "cell", "view", "check"},
    ),
    "netlist_export": (
        {"library", "cell", "view", "simulator"},
        {"library", "cell", "view", "simulator"},
    ),
    "standalone_simulation": (
        {"library", "cell", "testbench"},
        {"library", "cell", "testbench"},
    ),
    "oa_maestro_simulation": (
        {"library", "cell", "testbench"},
        {"library", "cell", "testbench"},
    ),
    "netlist_import": ({"library", "source"}, {"library", "source", "cell"}),
    "analysis": (
        {"library", "cell", "analysis", "model"},
        {"library", "cell", "analysis", "model"},
    ),
    "execution-run": ({"owner", "target"}, {"owner", "target"}),
}


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
        return b"".join(chunks)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def _inspect_nofollow_file(path: Path) -> tuple[os.stat_result, str]:
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
            or before.st_nlink != 1
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


def read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(_read_nofollow_bytes(path).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RuntimeError) as exc:
        raise ArtifactManifestError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ArtifactManifestError(f"invalid {label} {path}: expected a JSON object")
    return value


def read_nofollow_text(path: Path, *, errors: str = "strict") -> str:
    return _read_nofollow_bytes(path).decode("utf-8", errors=errors)


def ensure_nofollow_directory(path: Path) -> Path:
    """Create/open one directory chain while rejecting symlink components."""

    absolute = Path(os.path.abspath(path))
    descriptor = _open_nofollow_directory(absolute, create_missing=True)
    os.close(descriptor)
    return absolute


def copy_immutable_file(source: Path, destination: Path) -> Path:
    """Copy one stable regular file to a new nofollow artifact path."""

    target = Path(os.path.abspath(destination))
    _write_exclusive_bytes(target, _read_nofollow_bytes(Path(source)))
    return target


def write_immutable_text(path: Path, value: str) -> None:
    """Create one nofollow regular text artifact without replacement semantics."""

    if not isinstance(value, str):
        raise ValueError("immutable text artifact must be text")
    _write_exclusive_bytes(Path(path), value.encode("utf-8"))


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
    """Resolve an artifact member while rejecting every existing symlink component."""

    lexical_root = Path(os.path.abspath(root))
    lexical_member = Path(os.path.abspath(member))
    if not lexical_member.is_relative_to(lexical_root):
        raise RuntimeError(f"artifact {label} is outside its role: {lexical_member}")
    root_descriptor = _open_nofollow_directory(
        lexical_root,
        create_missing=False,
    )
    os.close(root_descriptor)
    relative = lexical_member.relative_to(lexical_root)
    current = lexical_root
    for component in relative.parts:
        current /= component
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise RuntimeError(f"artifact {label} cannot traverse a symlink: {current}")
    resolved_root = lexical_root.resolve()
    resolved_member = lexical_member.resolve()
    if not resolved_member.is_relative_to(resolved_root):
        raise RuntimeError(f"artifact {label} escaped its role: {resolved_member}")
    return resolved_member


def validate_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the current artifact manifest contract."""
    required = {
        "artifact_kind",
        "entities",
        "operation",
        "operation_id",
        "backend",
        "run_id",
        "attempt_id",
        "created_at",
        "completed_at",
        "status",
        "source",
        "files",
        "partial_failure",
        "uncertain_reason",
        "incident_reference",
        "completion_evidence",
    }
    missing = required.difference(value)
    if missing:
        raise ArtifactManifestError(
            f"artifact manifest is missing fields: {', '.join(sorted(missing))}"
        )
    status = value.get("status")
    if status not in ARTIFACT_STATUSES:
        raise ArtifactManifestError(f"invalid artifact status: {status!r}")
    run_id = value.get("run_id")
    attempt_id = value.get("attempt_id")
    if (run_id is None) == (attempt_id is None):
        raise ArtifactManifestError("manifest must contain exactly one run_id or attempt_id")
    try:
        validate_artifact_id(run_id if run_id is not None else attempt_id, "execution id")
    except ValueError as exc:
        raise ArtifactManifestError(str(exc)) from exc
    for field_name in ("entities", "files"):
        if not isinstance(value.get(field_name), dict):
            raise ArtifactManifestError(f"manifest {field_name} must be an object")
    source = value.get("source")
    if source is not None and not isinstance(source, dict):
        raise ArtifactManifestError("manifest source must be an object or null")
    artifact_kind = value.get("artifact_kind")
    if artifact_kind not in ARTIFACT_ROLES:
        raise ArtifactManifestError(f"unsupported artifact_kind: {artifact_kind!r}")
    for field_name in ("operation", "backend", "created_at"):
        if not isinstance(value.get(field_name), str) or not value[field_name]:
            raise ArtifactManifestError(f"manifest {field_name} must be a non-empty string")
    try:
        datetime.fromisoformat(value["created_at"])
    except ValueError as exc:
        raise ArtifactManifestError("manifest created_at is not an ISO timestamp") from exc
    expected_identity_kind = ARTIFACT_IDENTITY_KINDS[artifact_kind]
    if expected_identity_kind == "run_id" and attempt_id is not None:
        raise ArtifactManifestError(f"{artifact_kind} requires run_id identity")
    if expected_identity_kind == "attempt_id" and run_id is not None:
        raise ArtifactManifestError(f"{artifact_kind} requires attempt_id identity")
    operation_id = value.get("operation_id")
    if operation_id is not None:
        try:
            validate_artifact_id(operation_id, "operation id")
        except ValueError as exc:
            raise ArtifactManifestError(str(exc)) from exc
    entity_labels = set(value["entities"])
    required_entities, allowed_entities = ARTIFACT_ENTITY_FIELDS[artifact_kind]
    if not required_entities.issubset(entity_labels) or not entity_labels.issubset(
        allowed_entities
    ):
        raise ArtifactManifestError(
            f"manifest entities do not match {artifact_kind}: {sorted(entity_labels)}"
        )
    for label, component in value["entities"].items():
        if not isinstance(label, str):
            raise ArtifactManifestError("manifest entity labels must be strings")
        try:
            validate_artifact_component(component, f"entity {label}")
        except ValueError as exc:
            raise ArtifactManifestError(str(exc)) from exc
    files = value["files"]
    if set(files) != ARTIFACT_ROLES[artifact_kind]:
        raise ArtifactManifestError(
            f"manifest file roles do not match {artifact_kind}: {sorted(files)}"
        )
    registered_paths: set[str] = set()
    for role, references in files.items():
        if not isinstance(references, list):
            raise ArtifactManifestError(f"manifest file role {role} must be a list")
        for reference in references:
            if not isinstance(reference, dict):
                raise ArtifactManifestError("manifest file reference must be an object")
            relative = reference.get("path")
            path = _safe_manifest_relative(relative, "file reference")
            if path.parts[0] != role:
                raise ArtifactManifestError(f"unsafe manifest file reference: {relative!r}")
            kind = reference.get("kind")
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
            registered_paths.add(relative)
    completion_evidence = value.get("completion_evidence")
    if not isinstance(completion_evidence, list) or not all(
        isinstance(path, str) and path in registered_paths for path in completion_evidence
    ):
        raise ArtifactManifestError(
            "manifest completion_evidence must reference registered files"
        )
    if len(completion_evidence) != len(set(completion_evidence)):
        raise ArtifactManifestError("manifest completion_evidence must be unique")
    if any(Path(path).parts[0] != "outputs" for path in completion_evidence):
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
class ArtifactRecord:
    """Mutable handle to one immutable-identity run or attempt manifest."""

    paths: ArtifactExecutionPaths
    manifest: dict[str, Any]
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _file_states: dict[str, tuple[int, int, int, str]] = field(
        default_factory=dict, repr=False
    )

    @classmethod
    def begin(
        cls,
        paths: ArtifactExecutionPaths,
        *,
        entities: Mapping[str, str],
        operation: str,
        backend: str,
        source: Mapping[str, Any] | None = None,
    ) -> "ArtifactRecord":
        files = {role: [] for role in paths.roles}
        manifest = {
            "artifact_kind": paths.artifact_kind,
            "entities": dict(entities),
            "operation": operation,
            "operation_id": None,
            "backend": backend,
            "run_id": paths.identity if paths.identity_kind == "run_id" else None,
            "attempt_id": paths.identity if paths.identity_kind == "attempt_id" else None,
            "created_at": utc_now(),
            "completed_at": None,
            "status": "running",
            "source": None if source is None else dict(source),
            "files": files,
            "partial_failure": None,
            "uncertain_reason": None,
            "incident_reference": None,
            "completion_evidence": [],
        }
        validate_manifest(manifest)
        paths.create()
        atomic_write_json(paths.manifest, manifest)
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
            self.add_file(role, path)
            return path

    def path(self, role: str, *components: str) -> Path:
        return self.paths.path(role, *components)

    def add_file(self, role: str, path: Path, *, label: str | None = None) -> dict[str, Any]:
        with self._lock:
            self._require_running("register a file")
            role_root = self.paths.role(role)
            candidate = _resolved_artifact_member(
                role_root, Path(path), f"{role} file reference"
            )
            try:
                metadata, digest = _inspect_nofollow_file(candidate)
            except IsADirectoryError:
                metadata = candidate.stat(follow_symlinks=False)
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
            candidate_manifest = copy.deepcopy(self.manifest)
            entries = candidate_manifest["files"][role]
            entries[:] = [entry for entry in entries if entry["path"] != reference["path"]]
            entries.append(reference)
            self._persist_candidate(candidate_manifest)
            return reference

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

    def copy_file(
        self,
        role: str,
        components: Sequence[str],
        source: Path,
        *,
        label: str | None = None,
    ) -> Path:
        with self._lock:
            self._require_running("copy a file")
            payload = _read_nofollow_bytes(source)
            path = self.path(role, *components)
            _write_exclusive_bytes(path, payload)
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
                if not candidate.is_file() or not candidate.is_relative_to(
                    self.paths.root.resolve()
                ):
                    raise RuntimeError(f"invalid completion evidence: {candidate}")
                relative = candidate.relative_to(self.paths.root.resolve()).as_posix()
                registered = next(
                    (
                        entry
                        for entries in self.manifest["files"].values()
                        for entry in entries
                        if entry["path"] == relative
                    ),
                    None,
                )
                if registered is None:
                    raise RuntimeError(
                        f"completion evidence is not registered in manifest: {relative}"
                    )
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
            candidate_manifest = copy.deepcopy(self.manifest)
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
            root = self.paths.artifact_root.resolve()
            incident = _resolved_artifact_member(
                root, Path(incident_path), "operation incident"
            )
            if not incident.is_file():
                raise RuntimeError(f"operation incident is outside artifacts: {incident}")
            load_operation_incident(incident, operation_id)
            reference = incident.relative_to(root).as_posix()
            existing = self.manifest.get("incident_reference")
            if existing not in {None, reference}:
                raise RuntimeError("artifact already refers to a different operation incident")
            candidate_manifest = copy.deepcopy(self.manifest)
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
            candidate_manifest = copy.deepcopy(self.manifest)
            candidate_manifest["operation_id"] = identity
            self._persist_candidate(candidate_manifest)

    @contextmanager
    def failure_boundary(
        self,
        *,
        uncertainty: Callable[[], str | None] = lambda: None,
        partial_failure: Callable[[], Mapping[str, Any] | None] = lambda: None,
        details: Callable[[], Mapping[str, Any] | None] = lambda: None,
    ) -> Iterator[None]:
        """Terminalize failures that happen before a workspace can register callbacks."""

        try:
            yield
        except BaseException as error:
            if self.status == "running":
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
