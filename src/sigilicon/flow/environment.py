"""Resolve one explicit current-site Execution Environment contract."""

from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
import tomllib
from typing import Any, Mapping

from sigilicon.flow.model import (
    ExecutionEnvironment,
    FlowContractError,
    ResolvedCapability,
    ResolvedPlatformAsset,
    ResolvedPlatformAssetMember,
    identifier,
    owner_identity,
)
from sigilicon.flow.serialization import canonical_digest


_HEADER_FIELDS = {"schema", "contract_kind", "path_scope", "owner"}


def _reject_unknown(raw: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise FlowContractError(f"{label} contains unknown fields: {sorted(unknown)}")


def _table(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FlowContractError(f"{label} must be a table")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise FlowContractError(f"{label} must be a non-empty string")
    return value


def _site_path(
    value: object,
    label: str,
    *,
    preserve_launcher: bool = False,
) -> Path:
    raw = _text(value, label)
    path = Path(raw)
    if not path.is_absolute():
        raise FlowContractError(f"{label} must be an absolute current-site path")
    if preserve_launcher:
        return Path(os.path.abspath(path))
    return path.resolve()


def _sha256(path: Path) -> str:
    hasher = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _load_capabilities(value: object) -> dict[str, ResolvedCapability]:
    raw = _table(value, "Execution Environment capabilities")
    result: dict[str, ResolvedCapability] = {}
    for name, item_value in raw.items():
        identifier(name, "Execution Environment capability")
        item = _table(item_value, f"capabilities.{name}")
        _reject_unknown(item, {"identity", "executable"}, f"capabilities.{name}")
        executable_value = item.get("executable")
        executable = (
            None
            if executable_value is None
            else _site_path(
                executable_value,
                f"capabilities.{name}.executable",
                preserve_launcher=True,
            )
        )
        if executable is not None and (
            not executable.is_file() or not os.access(executable, os.X_OK)
        ):
            raise FlowContractError(
                f"capability executable is unavailable: {name!r}"
            )
        result[name] = ResolvedCapability(
            identity=_text(item.get("identity"), f"capabilities.{name}.identity"),
            executable=executable,
        )
    return result


def _load_platform_assets(value: object) -> tuple[ResolvedPlatformAsset, ...]:
    if not isinstance(value, list):
        raise FlowContractError("Execution Environment platform_assets must be an array")
    assets: list[ResolvedPlatformAsset] = []
    for index, item_value in enumerate(value):
        label = f"platform_assets[{index}]"
        item = _table(item_value, label)
        _reject_unknown(item, {"role", "kind", "identity", "members"}, label)
        members_value = item.get("members")
        if not isinstance(members_value, list) or not members_value:
            raise FlowContractError(f"{label}.members must be a non-empty array")
        members: list[ResolvedPlatformAssetMember] = []
        for member_index, member_value in enumerate(members_value):
            member_label = f"{label}.members[{member_index}]"
            member = _table(member_value, member_label)
            _reject_unknown(member, {"role", "path"}, member_label)
            location = _site_path(member.get("path"), f"{member_label}.path")
            if not location.is_file():
                raise FlowContractError(
                    f"platform asset member is unavailable: {member_label}"
                )
            members.append(
                ResolvedPlatformAssetMember(
                    role=_text(member.get("role"), f"{member_label}.role"),
                    digest=_sha256(location),
                    location=location,
                )
            )
        role = _text(item.get("role"), f"{label}.role")
        kind = _text(item.get("kind"), f"{label}.kind")
        identity_value = _text(item.get("identity"), f"{label}.identity")
        digest = canonical_digest(
            {
                "role": role,
                "kind": kind,
                "identity": identity_value,
                "members": [
                    {"role": member.role, "digest": member.digest}
                    for member in sorted(members, key=lambda candidate: candidate.role)
                ],
            }
        )
        assets.append(
            ResolvedPlatformAsset(
                role=role,
                kind=kind,
                identity=identity_value,
                digest=digest,
                members=tuple(members),
            )
        )
    return tuple(assets)


def load_execution_environment(path: Path) -> ExecutionEnvironment:
    """Load and verify one explicitly selected, non-discovered site contract."""

    contract = Path(path).resolve()
    try:
        with contract.open("rb") as stream:
            raw: dict[str, Any] = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise FlowContractError(
            f"cannot read Execution Environment {contract}: {exc}"
        ) from exc
    _reject_unknown(
        raw,
        _HEADER_FIELDS | {"name", "capabilities", "platform_assets"},
        str(contract),
    )
    if raw.get("schema") != 1:
        raise FlowContractError("Execution Environment must use the current schema 1")
    if raw.get("contract_kind") != "execution-environment":
        raise FlowContractError(
            "Execution Environment contract_kind must be 'execution-environment'"
        )
    if raw.get("path_scope") != "site":
        raise FlowContractError("Execution Environment path_scope must be 'site'")
    owner_identity(_text(raw.get("owner"), "Execution Environment owner"), "site owner")
    identifier(_text(raw.get("name"), "Execution Environment name"), "environment identity")
    return ExecutionEnvironment(
        capabilities=_load_capabilities(raw.get("capabilities", {})),
        platform_assets=_load_platform_assets(raw.get("platform_assets", [])),
    )


def capability_available(capability: ResolvedCapability) -> bool:
    """Recheck private executable availability immediately before execution."""

    executable = capability.executable
    return executable is None or (
        executable.is_file() and os.access(executable, os.X_OK)
    )


def stale_platform_asset_members(asset: ResolvedPlatformAsset) -> tuple[str, ...]:
    """Return member roles whose current content differs from the resolved asset."""

    stale: list[str] = []
    for member in asset.members:
        if not member.location.is_file() or _sha256(member.location) != member.digest:
            stale.append(member.role)
    return tuple(stale)


__all__ = [
    "capability_available",
    "load_execution_environment",
    "stale_platform_asset_members",
]
