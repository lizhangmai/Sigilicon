"""Resolve Git-owned source selections and snapshot them into one Flow Run."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import stat
import tomllib
from typing import Any, Mapping

from sigilicon.artifacts import atomic_write_json, read_nofollow_text
from sigilicon.external_tools import run_readonly_capture
from sigilicon.flow.adapter_result import complete_staged_run
from sigilicon.flow.model import (
    ActionContext,
    ActionContract,
    AdapterExecution,
    AdapterResult,
    CollectedActionResult,
    FlowContractError,
    FlowExecutionError,
    FlowNode,
    FlowSpec,
    GitSource,
    ProducedArtifact,
    SourceArtifact,
    SourceAssets,
    SourceMember,
)


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


def _owner_path(root: Path, value: object, label: str) -> tuple[str, Path]:
    text = _text(value, label)
    relative = PurePosixPath(text)
    if (
        relative.is_absolute()
        or "\\" in text
        or relative.as_posix() != text
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise FlowContractError(f"{label} must stay within the explicit owner root")
    resolved = root.joinpath(*relative.parts).resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise FlowContractError(f"{label} escaped the explicit owner root")
    return relative.as_posix(), resolved


def _git(root: Path, *args: str) -> bytes:
    try:
        return run_readonly_capture(
            ("git", *args),
            cwd=root,
        )
    except Exception as exc:
        raise FlowContractError("source owner is not a readable Git checkout") from exc


def git_source(root: Path) -> GitSource:
    """Read owner-scoped Git state without inventing parallel source identity."""

    owner_root = Path(root).resolve()
    repository_root = Path(
        _git(owner_root, "rev-parse", "--show-toplevel").decode().strip()
    ).resolve()
    commit = _git(owner_root, "rev-parse", "HEAD").decode().strip()
    changes = tuple(
        item.decode("utf-8", errors="surrogateescape")
        for item in _git(
            owner_root,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--",
            ".",
        ).split(b"\0")
        if item
    )
    return GitSource(
        commit=commit,
        changes=changes,
        repository_root=repository_root,
        scope_root=owner_root,
    )


def source_assets_payload(source: SourceAssets) -> dict[str, Any]:
    """Return the portable source selection recorded in a plan."""

    return {
        "name": source.name,
        "git": {
            "commit": source.git.commit,
            "changes": list(source.git.changes),
        },
        "artifacts": {
            artifact.role: {
                "kind": artifact.kind,
                "materialization": artifact.materialization,
                "qualifiers": dict(artifact.qualifiers),
                "members": [
                    {
                        "path": member.path,
                        "record_text": member.record_text,
                        "executable": member.executable,
                    }
                    for member in artifact.members
                ],
            }
            for artifact in source.artifacts
        },
    }


def source_member_matches(member: SourceMember) -> bool:
    """Compare one selected member with its exact persisted source record."""

    return (
        read_nofollow_text(member.location) == member.record_text
        and bool(
            member.location.stat(follow_symlinks=False).st_mode
            & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        )
        == member.executable
    )


def load_source_assets(
    path: Path,
    *,
    owner_root: Path,
    expected_owner: str,
) -> SourceAssets:
    """Load one Git-owned source selection without content re-hashing."""

    root = Path(owner_root).resolve()
    contract = Path(path).resolve()
    if not contract.is_relative_to(root):
        raise FlowContractError("Source Assets must be inside their owner root")
    try:
        with contract.open("rb") as stream:
            raw: dict[str, Any] = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise FlowContractError("cannot read Source Assets contract") from exc
    _reject_unknown(
        raw,
        _HEADER_FIELDS | {"name", "qualifiers", "artifacts"},
        str(contract),
    )
    if raw.get("schema") != 1:
        raise FlowContractError("Source Assets must use the current schema 1")
    if raw.get("contract_kind") != "source-assets":
        raise FlowContractError("Source Assets contract_kind must be 'source-assets'")
    if raw.get("path_scope") != "owner":
        raise FlowContractError("Source Assets path_scope must be 'owner'")
    owner = _text(raw.get("owner"), "Source Assets owner")
    if owner != expected_owner:
        raise FlowContractError(
            f"Source Assets owner {owner!r} does not match {expected_owner!r}"
        )
    qualifiers = _table(raw.get("qualifiers", {}), "source qualifiers")
    artifacts_raw = raw.get("artifacts")
    if not isinstance(artifacts_raw, list) or not artifacts_raw:
        raise FlowContractError("Source Assets artifacts must be a non-empty array")

    artifacts: list[SourceArtifact] = []
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
        members: list[SourceMember] = []
        for member_index, value in enumerate(members_raw):
            relative, location = _owner_path(
                root,
                value,
                f"artifacts[{index}].members[{member_index}]",
            )
            if not location.is_file():
                raise FlowContractError(
                    f"source member is not a regular file: {relative}"
                )
            try:
                record_text = read_nofollow_text(location)
            except (OSError, RuntimeError, UnicodeError) as exc:
                raise FlowContractError(
                    f"source member must be readable UTF-8 text: {relative}"
                ) from exc
            members.append(
                SourceMember(
                    path=relative,
                    source_root=root,
                    record_text=record_text,
                    executable=bool(
                        location.stat(follow_symlinks=False).st_mode
                        & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                    ),
                    location=location,
                )
            )
        artifacts.append(
            SourceArtifact(
                role=_text(artifact.get("role"), f"artifacts[{index}].role"),
                kind=_text(artifact.get("kind"), f"artifacts[{index}].kind"),
                materialization=_text(
                    artifact.get("materialization"),
                    f"artifacts[{index}].materialization",
                ),
                qualifiers=qualifiers,
                members=tuple(members),
            )
        )
    return SourceAssets(
        owner=owner,
        name=_text(raw.get("name"), "Source Assets name"),
        git=git_source(root),
        artifacts=tuple(artifacts),
        owner_root=root,
    )


def resolve_node_source_assets(
    spec: FlowSpec,
    node: FlowNode,
    action: ActionContract,
) -> SourceAssets | None:
    if not action.resolves_source_assets:
        return None
    if spec.owner_root is None:
        raise FlowContractError(
            f"source assets node {node.node_id!r} requires an explicit owner root"
        )
    if set(node.config) != {"source"}:
        raise FlowContractError(
            f"source assets node {node.node_id!r} config must contain only 'source'"
        )
    _relative, contract = _owner_path(
        spec.owner_root,
        node.config["source"],
        f"source assets node {node.node_id!r}",
    )
    source = load_source_assets(
        contract,
        owner_root=spec.owner_root,
        expected_owner=spec.owner,
    )
    expected = {port.role: port.kind for port in action.outputs}
    actual = {artifact.role: artifact.kind for artifact in source.artifacts}
    if actual != expected:
        raise FlowContractError(
            f"source roles do not match Action {action.kind!r}: "
            f"expected={expected}, actual={actual}"
        )
    return source


class SourceAssetsAdapter:
    """Snapshot selected source below one managed Action directory."""

    def run(self, context: ActionContext) -> AdapterResult:
        return complete_staged_run(
            context,
            validate_inputs=self._validate_inputs,
            prepare=self._prepare,
            execute=self._execute,
            collect_result=self._collect_result,
        )

    def _validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        source = context.source_assets
        if source is None:
            return ("source-assets Action omitted its source selection",)
        return (
            ()
            if git_source(source.owner_root) == source.git
            else ("Git source changed after planning",)
        )

    def _prepare(self, context: ActionContext) -> None:
        pass

    def _execute(self, context: ActionContext) -> AdapterExecution:
        source = context.source_assets
        if source is None:
            raise FlowExecutionError("source-assets Action has no source selection")
        for artifact in source.artifacts:
            output = self._output_path(context, artifact)
            if artifact.materialization == "file":
                member = artifact.members[0]
                if not source_member_matches(member):
                    raise FlowExecutionError(
                        f"source member changed after planning: {member.path}"
                    )
                output.write_text(member.record_text, encoding="utf-8")
                output.chmod(0o755 if member.executable else 0o644)
                continue
            members: list[dict[str, str]] = []
            for member in artifact.members:
                relative_file = Path("files") / member.path
                destination = (output.parent / relative_file).resolve()
                if not destination.is_relative_to(output.parent.resolve()):
                    raise FlowExecutionError(
                        f"source snapshot escaped its artifact root: {member.path}"
                    )
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not source_member_matches(member):
                    raise FlowExecutionError(
                        f"source member changed after planning: {member.path}"
                    )
                destination.write_text(member.record_text, encoding="utf-8")
                destination.chmod(0o755 if member.executable else 0o644)
                members.append(
                    {"path": member.path, "file": relative_file.as_posix()}
                )
            atomic_write_json(
                output,
                {
                    "schema": 1,
                    "contract_kind": "source-set-manifest",
                    "kind": artifact.kind,
                    "qualifiers": dict(artifact.qualifiers),
                    "members": members,
                },
            )
        if git_source(source.owner_root) != source.git:
            raise FlowExecutionError("Git source changed while creating the run snapshot")
        if any(
            not source_member_matches(member)
            for artifact in source.artifacts
            for member in artifact.members
        ):
            raise FlowExecutionError("source member changed while creating the run snapshot")
        return AdapterExecution.succeeded()

    def _collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        source = context.source_assets
        if source is None:
            raise FlowExecutionError("source-assets Action has no source selection")
        return CollectedActionResult(
            artifacts=tuple(
                ProducedArtifact(
                    artifact.role,
                    artifact.kind,
                    self._output_path(context, artifact),
                    qualifiers=artifact.qualifiers,
                )
                for artifact in source.artifacts
            )
        )

    @staticmethod
    def _output_path(context: ActionContext, artifact: SourceArtifact) -> Path:
        filename = (
            Path(artifact.members[0].path).name
            if artifact.materialization == "file"
            else "manifest.json"
        )
        return context.output_path(artifact.role, filename)


__all__ = [
    "SourceAssetsAdapter",
    "git_source",
    "load_source_assets",
    "resolve_node_source_assets",
    "source_assets_payload",
    "source_member_matches",
]
