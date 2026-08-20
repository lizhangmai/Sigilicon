"""Legacy AMS/ADE validation and publication on the artifact lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from sigilicon.artifacts import (
    ArtifactManifestError,
    ArtifactRecord,
    atomic_write_json,
    file_sha256,
    load_manifest,
    read_nofollow_text,
    read_json_object,
)
from sigilicon.paths import AdeArtifactPaths, validate_artifact_id, validate_fingerprint


SETUP_COMPONENTS = ("load", "systemverilog", "config", "maestro")


def _safe_execution_relative(root: Path, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ArtifactManifestError(f"invalid ADE setup {label}: {value!r}")
    relative = Path(value)
    if (
        relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
        or "\\" in value
    ):
        raise ArtifactManifestError(f"unsafe ADE setup {label}: {value!r}")
    resolved_root = root.resolve()
    resolved = (resolved_root / relative).resolve()
    if not resolved.is_relative_to(resolved_root):
        raise ArtifactManifestError(f"unsafe ADE setup {label}: {value!r}")
    return resolved


def _component_path(setup_dir: Path, name: str) -> Path:
    return setup_dir / "evidence" / "components" / f"{name}.json"


def _load_component_receipts(
    setup_dir: Path,
    fingerprint: str,
) -> dict[str, dict[str, Any]]:
    components: dict[str, dict[str, Any]] = {}
    for name in SETUP_COMPONENTS:
        receipt = read_json_object(
            _component_path(setup_dir, name),
            f"ADE {name} evidence",
        )
        if receipt.get("component") != name:
            raise ArtifactManifestError(f"invalid ADE component evidence for {name}")
        if receipt.get("setup_fingerprint") != fingerprint:
            raise ArtifactManifestError(f"ADE {name} fingerprint does not match setup")
        components[name] = receipt
    return components


def _validate_component_files(
    setup_dir: Path,
    fingerprint: str,
    components: Mapping[str, Mapping[str, Any]],
) -> None:
    component_sources: dict[str, Path] = {}
    for name, label in (("load", "load wrapper"), ("systemverilog", "SystemVerilog")):
        receipt = components[name]
        source = _safe_execution_relative(setup_dir, receipt.get("source"), f"{label} source")
        expected_sha = receipt.get("source_sha256")
        if not source.is_file() or expected_sha != file_sha256(source):
            raise ArtifactManifestError(f"ADE {label} source does not match component evidence")
        component_sources[name] = source
    load_receipt = components["load"]
    device_map = _safe_execution_relative(
        setup_dir,
        load_receipt.get("device_map"),
        "load device map",
    )
    expected_device_map_sha = load_receipt.get("device_map_sha256")
    if not device_map.is_file() or expected_device_map_sha != file_sha256(device_map):
        raise ArtifactManifestError("ADE load device map does not match component evidence")
    source_text = read_nofollow_text(
        component_sources["systemverilog"],
        errors="replace",
    )
    if f"FLOW_FINGERPRINT {fingerprint}" not in source_text:
        raise ArtifactManifestError("ADE SystemVerilog source has a stale AMS fingerprint")


@dataclass(frozen=True)
class CommittedAdeSetup:
    namespace: AdeArtifactPaths
    setup_dir: Path
    manifest_path: Path
    fingerprint: str
    attempt_id: str
    components: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True)
class AdeSetupAttempt:
    """One setup attempt; only verified success publishes ``current.json``."""

    namespace: AdeArtifactPaths
    record: ArtifactRecord
    fingerprint: str

    @classmethod
    def begin(
        cls,
        namespace: AdeArtifactPaths,
        fingerprint: str,
        *,
        attempt_id: str,
        entities: Mapping[str, str],
        operation: str,
        backend: str,
        source_fingerprint: str,
    ) -> "AdeSetupAttempt":
        validate_fingerprint(fingerprint, "setup fingerprint")
        record = ArtifactRecord.begin(
            namespace.setup_attempt(fingerprint, attempt_id),
            entities=entities,
            operation=operation,
            backend=backend,
            source_fingerprint=source_fingerprint,
            setup_fingerprint=fingerprint,
        )
        return cls(namespace, record, fingerprint)

    @property
    def setup_dir(self) -> Path:
        return self.record.paths.root

    @property
    def manifest_path(self) -> Path:
        return self.record.paths.manifest

    @property
    def attempt_id(self) -> str:
        return self.record.paths.identity

    def path(self, role: str, *components: str) -> Path:
        return self.record.path(role, *components)

    def directory(self, role: str, *components: str) -> Path:
        return self.record.directory(role, *components)

    def record_component(self, component: str, **details: Any) -> Path:
        if component not in SETUP_COMPONENTS:
            raise ValueError(f"unsupported ADE setup component: {component!r}")
        reserved = {"component", "setup_fingerprint"}.intersection(details)
        if reserved:
            raise ValueError(f"reserved component fields: {', '.join(sorted(reserved))}")
        return self.record.write_json(
            "evidence",
            ("components", f"{component}.json"),
            {
                "component": component,
                "setup_fingerprint": self.fingerprint,
                **details,
            },
            label=f"{component} completion evidence",
        )

    def record_failure(
        self,
        error: BaseException,
        *,
        uncertain_reason: str | None = None,
        partial_failure: Mapping[str, Any] | None = None,
    ) -> None:
        if self.record.status != "running":
            return
        self.record.fail(
            error,
            uncertain_reason=uncertain_reason,
            partial_failure=partial_failure,
        )

    def commit(
        self,
        *,
        validate_components: Callable[
            [Mapping[str, Mapping[str, Any]]], None
        ]
        | None = None,
    ) -> Path:
        components = _load_component_receipts(self.setup_dir, self.fingerprint)
        _validate_component_files(self.setup_dir, self.fingerprint, components)
        if validate_components is not None:
            validate_components(components)
        final_components = _load_component_receipts(self.setup_dir, self.fingerprint)
        if final_components != components:
            raise ArtifactManifestError(
                "ADE component evidence changed during commit validation"
            )
        _validate_component_files(self.setup_dir, self.fingerprint, final_components)
        evidence = [_component_path(self.setup_dir, name) for name in SETUP_COMPONENTS]
        manifest_path = self.record.succeed(
            completion_evidence=evidence,
            details={"components": final_components},
        )
        current = {
            "artifact_kind": "ade_setup",
            "status": "succeeded",
            "setup_fingerprint": self.fingerprint,
            "attempt_id": self.attempt_id,
            "manifest": manifest_path.relative_to(
                self.namespace.artifact_root
            ).as_posix(),
        }
        # begin/failure never touches this file. A failed atomic replacement
        # therefore leaves the preceding successful pointer intact.
        atomic_write_json(self.namespace.current, current)
        return manifest_path


def load_committed_ade_setup(
    namespace: AdeArtifactPaths,
    expected_fingerprint: str,
) -> CommittedAdeSetup:
    """Load the exact successful setup selected by the atomic current pointer."""

    fingerprint = validate_fingerprint(expected_fingerprint, "setup fingerprint")
    current = read_json_object(namespace.current, "ADE current setup pointer")
    if current.get("artifact_kind") != "ade_setup" or current.get("status") != "succeeded":
        raise ArtifactManifestError("ADE current pointer does not name a successful setup")
    if current.get("setup_fingerprint") != fingerprint:
        raise ArtifactManifestError(
            "ADE setup fingerprint does not match the current AMS spec; rerun setup-ams-ade"
        )
    attempt_id = validate_artifact_id(current.get("attempt_id"), "attempt id")
    expected_paths = namespace.setup_attempt(fingerprint, attempt_id)
    expected_reference = expected_paths.manifest.relative_to(namespace.artifact_root).as_posix()
    if current.get("manifest") != expected_reference:
        raise ArtifactManifestError("ADE current pointer has an inconsistent manifest path")
    manifest = load_manifest(expected_paths.manifest)
    if (
        manifest["artifact_kind"] != "ade_setup"
        or manifest["status"] != "succeeded"
        or manifest["attempt_id"] != attempt_id
        or manifest["fingerprints"].get("setup") != fingerprint
        or manifest["entities"].get("library") != namespace.library
        or manifest["entities"].get("testbench") != namespace.testbench
    ):
        raise ArtifactManifestError(
            "ADE setup manifest is inconsistent with current.json"
        )
    details = manifest.get("details")
    raw_components = details.get("components") if isinstance(details, dict) else None
    if not isinstance(raw_components, dict) or set(raw_components) != set(SETUP_COMPONENTS):
        raise ArtifactManifestError("ADE setup manifest lacks complete component evidence")
    components = _load_component_receipts(expected_paths.root, fingerprint)
    if components != raw_components:
        raise ArtifactManifestError("ADE component evidence does not match setup manifest")
    _validate_component_files(expected_paths.root, fingerprint, components)
    return CommittedAdeSetup(
        namespace=namespace,
        setup_dir=expected_paths.root,
        manifest_path=expected_paths.manifest,
        fingerprint=fingerprint,
        attempt_id=attempt_id,
        components=components,
    )
