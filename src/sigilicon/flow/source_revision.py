"""Resolve owner source contracts into immutable pre-execution revisions."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import tomllib
from typing import Any, Mapping

from sigilicon.flow.model import (
    ActionContract,
    FlowContractError,
    FlowNode,
    FlowSpec,
    SourceArtifactRevision,
    SourceAssetRevision,
    SourceRevisionMember,
)
from sigilicon.flow.serialization import canonical_digest, json_value


_HEADER_FIELDS = {"schema", "contract_kind", "path_scope", "owner"}


def _sha256(path: Path) -> str:
    hasher = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _reject_unknown(raw: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise FlowContractError(f"{label} contains unknown fields: {sorted(unknown)}")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise FlowContractError(f"{label} must be a non-empty string")
    return value


def _table(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FlowContractError(f"{label} must be a table")
    return value


def _owner_path(owner_root: Path, value: object, label: str) -> tuple[str, Path]:
    root = Path(owner_root).resolve()
    relative_text = _text(value, label)
    relative = Path(relative_text)
    if (
        relative.is_absolute()
        or "\\" in relative_text
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise FlowContractError(f"{label} must stay within the explicit owner root")
    resolved = (root / relative).resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise FlowContractError(f"{label} escaped the explicit owner root")
    return relative.as_posix(), resolved


def source_revision_payload(revision: SourceAssetRevision) -> dict[str, Any]:
    """Return the portable identity recorded in plans and requests."""

    return {
        "owner": revision.owner,
        "name": revision.revision_id,
        "fingerprint": revision.fingerprint,
        "contracts": {
            role: {"path": member.path, "digest": member.digest}
            for role, member in revision.contracts.items()
        },
        "artifacts": {
            artifact.role: {
                "kind": artifact.kind,
                "materialization": artifact.materialization,
                "qualifiers": json_value(artifact.qualifiers),
                "fingerprint": artifact.fingerprint,
                "members": [
                    {"path": member.path, "digest": member.digest}
                    for member in artifact.members
                ],
            }
            for artifact in revision.artifacts
        },
    }


def load_source_asset_revision(
    path: Path,
    *,
    owner_root: Path,
    expected_owner: str,
) -> SourceAssetRevision:
    """Hash one current-schema owner contract without materializing a run."""

    root = Path(owner_root).resolve()
    contract = Path(path).resolve()
    if not contract.is_relative_to(root):
        raise FlowContractError("Source Asset Revision must be inside its owner root")
    try:
        with contract.open("rb") as stream:
            raw: dict[str, Any] = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise FlowContractError(
            f"cannot read Source Asset Revision {contract}: {exc}"
        ) from exc
    _reject_unknown(
        raw,
        _HEADER_FIELDS | {"name", "qualifiers", "contracts", "artifacts"},
        str(contract),
    )
    if raw.get("schema") != 1:
        raise FlowContractError("Source Asset Revision must use the current schema 1")
    if raw.get("contract_kind") != "source-asset-revision":
        raise FlowContractError(
            "Source Asset Revision contract_kind must be 'source-asset-revision'"
        )
    if raw.get("path_scope") != "owner":
        raise FlowContractError("Source Asset Revision path_scope must be 'owner'")
    owner = _text(raw.get("owner"), "Source Asset Revision owner")
    if owner != expected_owner:
        raise FlowContractError(
            f"Source Asset Revision owner {owner!r} does not match {expected_owner!r}"
        )
    qualifiers = _table(raw.get("qualifiers", {}), "source revision qualifiers")
    contracts_raw = _table(raw.get("contracts", {}), "source revision contracts")
    contracts: dict[str, SourceRevisionMember] = {}
    for role, value in contracts_raw.items():
        relative, location = _owner_path(
            root,
            value,
            f"source revision contracts.{role}",
        )
        if not location.is_file():
            raise FlowContractError(
                f"source revision contract is not a regular file: {relative}"
            )
        contracts[role] = SourceRevisionMember(
            path=relative,
            digest=_sha256(location),
            location=location,
        )
    artifacts_raw = raw.get("artifacts")
    if not isinstance(artifacts_raw, list) or not artifacts_raw:
        raise FlowContractError("Source Asset Revision artifacts must be a non-empty array")

    artifacts: list[SourceArtifactRevision] = []
    for index, value in enumerate(artifacts_raw):
        artifact = _table(value, f"artifacts[{index}]")
        _reject_unknown(
            artifact,
            {"role", "kind", "materialization", "members"},
            f"artifacts[{index}]",
        )
        members_raw = artifact.get("members")
        if not isinstance(members_raw, list) or not members_raw:
            raise FlowContractError(f"artifacts[{index}].members must be non-empty")
        members: list[SourceRevisionMember] = []
        for member_index, value in enumerate(members_raw):
            relative, location = _owner_path(
                root,
                value,
                f"artifacts[{index}].members[{member_index}]",
            )
            if not location.is_file():
                raise FlowContractError(
                    f"source revision member is not a regular file: {relative}"
                )
            members.append(
                SourceRevisionMember(
                    path=relative,
                    digest=_sha256(location),
                    location=location,
                )
            )
        role = _text(artifact.get("role"), f"artifacts[{index}].role")
        kind = _text(artifact.get("kind"), f"artifacts[{index}].kind")
        materialization = _text(
            artifact.get("materialization"),
            f"artifacts[{index}].materialization",
        )
        artifact_payload = {
            "role": role,
            "kind": kind,
            "materialization": materialization,
            "qualifiers": qualifiers,
            "contracts": {
                role: {"path": member.path, "digest": member.digest}
                for role, member in contracts.items()
            },
            "members": [
                {"path": member.path, "digest": member.digest}
                for member in members
            ],
        }
        artifacts.append(
            SourceArtifactRevision(
                role=role,
                kind=kind,
                materialization=materialization,
                qualifiers=qualifiers,
                members=tuple(members),
                fingerprint=canonical_digest(artifact_payload),
            )
        )
    revision_id = _text(raw.get("name"), "Source Asset Revision name")
    revision_payload = {
        "owner": owner,
        "name": revision_id,
        "contracts": {
            role: {"path": member.path, "digest": member.digest}
            for role, member in contracts.items()
        },
        "artifacts": [
            {
                "role": artifact.role,
                "kind": artifact.kind,
                "materialization": artifact.materialization,
                "qualifiers": json_value(artifact.qualifiers),
                "fingerprint": artifact.fingerprint,
            }
            for artifact in artifacts
        ],
    }
    return SourceAssetRevision(
        owner=owner,
        revision_id=revision_id,
        contracts=contracts,
        artifacts=tuple(artifacts),
        fingerprint=canonical_digest(revision_payload),
    )


def resolve_node_source_revision(
    spec: FlowSpec,
    node: FlowNode,
    action: ActionContract,
) -> SourceAssetRevision | None:
    """Resolve the one generic source-revision convention hidden by FlowEngine."""

    if not action.resolves_source_revision:
        return None
    if spec.owner_root is None:
        raise FlowContractError(
            f"source revision node {node.node_id!r} requires an explicit owner root"
        )
    if set(node.config) != {"revision"}:
        raise FlowContractError(
            f"source revision node {node.node_id!r} config must contain only 'revision'"
        )
    relative, contract = _owner_path(
        spec.owner_root,
        node.config["revision"],
        f"source revision node {node.node_id!r}",
    )
    revision = load_source_asset_revision(
        contract,
        owner_root=spec.owner_root,
        expected_owner=spec.owner,
    )
    expected = {port.role: port.kind for port in action.outputs}
    actual = {artifact.role: artifact.kind for artifact in revision.artifacts}
    if actual != expected:
        raise FlowContractError(
            f"source revision {relative!r} roles do not match Action {action.kind!r}: "
            f"expected={expected}, actual={actual}"
        )
    return revision


def stale_source_members(revision: SourceAssetRevision) -> tuple[str, ...]:
    """Return owner-relative members missing or changed since planning."""

    stale: list[str] = []
    for member in revision.contracts.values():
        if not member.location.is_file() or _sha256(member.location) != member.digest:
            stale.append(member.path)
    for artifact in revision.artifacts:
        for member in artifact.members:
            if not member.location.is_file() or _sha256(member.location) != member.digest:
                stale.append(member.path)
    return tuple(dict.fromkeys(stale))


__all__ = [
    "load_source_asset_revision",
    "resolve_node_source_revision",
    "source_revision_payload",
    "stale_source_members",
]
