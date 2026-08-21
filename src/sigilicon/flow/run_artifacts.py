"""Current-schema records for exact prior-run artifact selection."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

from sigilicon.artifacts import read_json_object
from sigilicon.flow.model import FlowContractError, RunArtifactReference
from sigilicon.flow.model import FlowExecutionError
from sigilicon.flow.serialization import json_value


_FIELDS = {
    "schema",
    "contract_kind",
    "owner",
    "flow",
    "run_id",
    "node",
    "role",
    "kind",
    "qualifiers",
    "digest",
    "required_policy",
}
_DIRECTORY_MANIFEST_KINDS = frozenset({"library.synopsys-ndm"})


def _sha256(path: Path) -> str:
    hasher = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def validate_durable_artifact(
    path: Path,
    *,
    kind: str,
    qualifiers: Mapping[str, Any],
    digest: str,
) -> tuple[Path, ...]:
    """Validate one durable artifact and any directory-manifest members."""

    artifact = Path(path)
    if not artifact.is_file() or artifact.is_symlink():
        raise FlowExecutionError("Run Artifact durable content is missing or unsafe")
    if _sha256(artifact) != digest:
        raise FlowExecutionError("Run Artifact durable digest does not match content")
    if artifact.suffix != ".json":
        return ()
    try:
        manifest = read_json_object(artifact, "Run Artifact directory manifest")
    except (OSError, RuntimeError, ValueError) as exc:
        if kind in _DIRECTORY_MANIFEST_KINDS:
            raise FlowExecutionError(
                "Run Artifact kind requires a valid directory manifest"
            ) from exc
        return ()
    if manifest.get("contract_kind") != "artifact-directory-manifest":
        if kind in _DIRECTORY_MANIFEST_KINDS:
            raise FlowExecutionError(
                "Run Artifact kind requires a directory manifest"
            )
        return ()
    if (
        manifest.get("schema") != 1
        or manifest.get("kind") != kind
        or manifest.get("qualifiers") != dict(qualifiers)
    ):
        raise FlowExecutionError("Run Artifact directory manifest identity differs")

    root_text = manifest.get("root")
    root_relative = Path(root_text) if isinstance(root_text, str) else Path()
    if (
        not isinstance(root_text, str)
        or not root_text
        or root_relative.is_absolute()
        or "\\" in root_text
        or any(part in {"", ".", ".."} for part in root_relative.parts)
    ):
        raise FlowExecutionError("Run Artifact directory root is unsafe")
    directory = artifact.parent / root_relative
    resolved_parent = artifact.parent.resolve()
    if (
        directory.is_symlink()
        or not directory.is_dir()
        or not directory.resolve().is_relative_to(resolved_parent)
    ):
        raise FlowExecutionError("Run Artifact directory root escaped or is missing")

    members = manifest.get("members")
    if not isinstance(members, list) or not members:
        raise FlowExecutionError("Run Artifact directory members are missing")
    declared: set[str] = set()
    member_paths: list[Path] = []
    for index, value in enumerate(members):
        if not isinstance(value, Mapping):
            raise FlowExecutionError(
                f"Run Artifact directory member {index} is invalid"
            )
        relative_text = value.get("path")
        member_digest = value.get("digest")
        relative = Path(relative_text) if isinstance(relative_text, str) else Path()
        if (
            not isinstance(relative_text, str)
            or not relative_text
            or relative.is_absolute()
            or "\\" in relative_text
            or any(part in {"", ".", ".."} for part in relative.parts)
            or relative.as_posix() in declared
        ):
            raise FlowExecutionError("Run Artifact directory member path is unsafe")
        if (
            not isinstance(member_digest, str)
            or len(member_digest) != 64
            or any(
                character not in "0123456789abcdef"
                for character in member_digest
            )
        ):
            raise FlowExecutionError("Run Artifact directory member digest is invalid")
        member = directory / relative
        if (
            member.is_symlink()
            or not member.is_file()
            or not member.resolve().is_relative_to(directory.resolve())
        ):
            raise FlowExecutionError(
                "Run Artifact directory member escaped or is missing"
            )
        member_parent = member.parent
        while member_parent != directory:
            if member_parent.is_symlink():
                raise FlowExecutionError(
                    "Run Artifact directory member traverses a symlink"
                )
            member_parent = member_parent.parent
        if _sha256(member) != member_digest:
            raise FlowExecutionError("Run Artifact directory member digest drifted")
        declared.add(relative.as_posix())
        member_paths.append(member)

    actual: set[str] = set()
    for member in directory.rglob("*"):
        if member.is_symlink():
            raise FlowExecutionError("Run Artifact directory contains a symlink")
        if member.is_file():
            actual.add(member.relative_to(directory).as_posix())
    if actual != declared:
        raise FlowExecutionError("Run Artifact directory inventory drifted")
    return tuple(member_paths)


def load_run_artifact_reference(
    value: Mapping[str, Any],
) -> RunArtifactReference:
    """Load the sole supported public Run Artifact reference schema."""

    if not isinstance(value, Mapping) or set(value) != _FIELDS:
        raise FlowContractError(
            "Run Artifact reference fields do not match the current schema"
        )
    if value.get("schema") != 1 or value.get("contract_kind") != (
        "run-artifact-reference"
    ):
        raise FlowContractError("unsupported Run Artifact reference schema")
    qualifiers = value.get("qualifiers")
    if not isinstance(qualifiers, Mapping):
        raise FlowContractError("Run Artifact qualifiers must be a mapping")
    return RunArtifactReference(
        owner=value.get("owner"),
        flow_id=value.get("flow"),
        run_id=value.get("run_id"),
        node_id=value.get("node"),
        role=value.get("role"),
        kind=value.get("kind"),
        qualifiers=qualifiers,
        digest=value.get("digest"),
        required_policy=value.get("required_policy"),
    )


def run_artifact_reference_payload(
    reference: RunArtifactReference,
) -> dict[str, Any]:
    """Return a portable locator-free public record for one reference."""

    return {
        "schema": 1,
        "contract_kind": "run-artifact-reference",
        "owner": reference.owner,
        "flow": reference.flow_id,
        "run_id": reference.run_id,
        "node": reference.node_id,
        "role": reference.role,
        "kind": reference.kind,
        "qualifiers": json_value(reference.qualifiers),
        "digest": reference.digest,
        "required_policy": reference.required_policy,
    }
