"""Read and remove closed execution runs without loading current owner source."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
from typing import Any, Mapping

from sigilicon.artifacts import (
    _open_nofollow_directory,
    load_manifest,
    read_json_object,
    read_nofollow_text,
)
from sigilicon.canonical import canonical_digest
from sigilicon.execution.model import (
    Artifact,
    ContractError,
    RunFailure,
    RunResult,
    StepOutcome,
    StepResult,
)
from sigilicon.paths import ArtifactLayout, RunPaths


class RunStoreError(ValueError):
    """A requested run is missing, unsafe, or internally inconsistent."""


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _clear_directory(descriptor: int) -> None:
    """Remove a held directory tree without resolving any pathname."""

    os.fchmod(descriptor, 0o700)
    for name in os.listdir(descriptor):
        visible = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISDIR(visible.st_mode):
            child = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            try:
                held = os.fstat(child)
                if not _same_inode(visible, held):
                    raise RunStoreError("run directory changed while cleaning")
                _clear_directory(child)
                current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if not _same_inode(held, current):
                    raise RunStoreError("run directory changed while cleaning")
                os.rmdir(name, dir_fd=descriptor)
            finally:
                os.close(child)
        else:
            os.unlink(name, dir_fd=descriptor)


def _remove_nofollow_tree(root: Path, expected: os.stat_result) -> None:
    """Remove exactly *expected* below held no-follow parent and root fds."""

    parent = _open_nofollow_directory(root.parent, create_missing=False)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            root.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent,
        )
        held = os.fstat(descriptor)
        visible = os.stat(root.name, dir_fd=parent, follow_symlinks=False)
        if not _same_inode(expected, held) or not _same_inode(held, visible):
            raise RunStoreError("run root changed while cleaning")
        _clear_directory(descriptor)
        visible = os.stat(root.name, dir_fd=parent, follow_symlinks=False)
        if not _same_inode(held, visible):
            raise RunStoreError("run root changed while cleaning")
        os.rmdir(root.name, dir_fd=parent)
        os.fsync(parent)
    except OSError as exc:
        raise RunStoreError(f"could not safely clean execution run: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


@dataclass(frozen=True)
class _SelectedRun:
    paths: RunPaths
    owner: str
    operation: str
    variant: str | None
    run_id: str

@dataclass(frozen=True)
class _RunStore:
    """Read or clean one exact managed execution result."""

    artifact_root: Path

    def __post_init__(self) -> None:
        root = Path(self.artifact_root).resolve()
        if root == Path(root.anchor):
            raise RunStoreError("artifact root cannot be a filesystem root")
        object.__setattr__(self, "artifact_root", root)

    def _select(
        self,
        *,
        owner: str,
        operation: str,
        variant: str | None,
        run_id: str,
    ) -> _SelectedRun:
        try:
            paths = ArtifactLayout(self.artifact_root).operation_run(
                owner=owner,
                operation=operation,
                variant=variant,
                run_id=run_id,
            )
        except (RuntimeError, ValueError) as exc:
            raise RunStoreError(str(exc)) from exc
        return _SelectedRun(paths, owner, operation, variant, run_id)

    @staticmethod
    def _registered_files(manifest: Mapping[str, Any]) -> set[str]:
        files = manifest.get("files")
        if not isinstance(files, Mapping):
            raise RunStoreError("run manifest has no file inventory")
        return {
            entry["path"]
            for entries in files.values()
            if isinstance(entries, list)
            for entry in entries
            if isinstance(entry, Mapping) and isinstance(entry.get("path"), str)
        }

    @classmethod
    def _validate_inventory(
        cls,
        paths: RunPaths,
        manifest: Mapping[str, Any],
    ) -> None:
        root = paths.root
        allowed = {Path("manifest.json"), *(Path(role) for role in paths.roles)}
        references = {
            entry["path"]: entry
            for entries in manifest["files"].values()
            for entry in entries
        }
        for value in references:
            relative = Path(value)
            allowed.add(relative)
            allowed.update(parent for parent in relative.parents if parent != Path("."))
        actual = {path.relative_to(root) for path in root.rglob("*")}
        if actual != allowed:
            raise RunStoreError("run filesystem inventory disagrees with its manifest")
        for value, reference in references.items():
            path = root / value
            if path.resolve() != path.absolute() or path.is_symlink():
                raise RunStoreError("run manifest references an unsafe filesystem member")
            if reference["kind"] == "file":
                metadata = path.stat(follow_symlinks=False)
                digest = hashlib.sha256(
                    read_nofollow_text(path, errors="surrogateescape").encode(
                        "utf-8", errors="surrogateescape"
                    )
                ).hexdigest()
                if (
                    not path.is_file()
                    or metadata.st_nlink != 1
                    or metadata.st_size != reference["size"]
                    or digest != reference.get("sha256")
                ):
                    raise RunStoreError("run file metadata disagrees with its manifest")
            elif not path.is_dir():
                raise RunStoreError("run directory metadata disagrees with its manifest")

    def _manifest(
        self,
        selected: _SelectedRun,
    ) -> dict[str, Any]:
        root = selected.paths.root
        if not root.is_dir() or root.is_symlink() or root.resolve() != root.absolute():
            raise RunStoreError(f"missing or unsafe execution run: {selected.run_id}")
        try:
            manifest = load_manifest(selected.paths.manifest)
        except (OSError, RuntimeError) as exc:
            raise RunStoreError(str(exc)) from exc
        source = manifest.get("source")
        if (
            manifest.get("schema") != 1
            or manifest.get("contract_kind") != "run-manifest"
            or manifest.get("owner") != selected.owner
            or manifest.get("operation") != selected.operation
            or manifest.get("variant") != selected.variant
            or manifest.get("run_id") != selected.run_id
            or manifest.get("status")
            not in {"succeeded", "failed", "partial", "uncertain", "cancelled"}
            or not isinstance(source, Mapping)
            or not isinstance(source.get("plan_identity"), str)
        ):
            raise RunStoreError("execution manifest identity or terminal state drift")
        self._validate_inventory(selected.paths, manifest)
        return manifest

    def _records(
        self,
        selected: _SelectedRun,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        root = selected.paths.root
        manifest = self._manifest(selected)
        try:
            plan = read_json_object(
                selected.paths.role("inputs") / "execution-plan.json",
                "Execution Plan",
            )
            result = read_json_object(
                selected.paths.role("outputs") / "run-result.json",
                "Run Result",
            )
        except (OSError, RuntimeError) as exc:
            raise RunStoreError(str(exc)) from exc
        identity = canonical_digest(plan)
        expected = {
            "owner": selected.owner,
            "operation": selected.operation,
            "variant": selected.variant,
            "run_id": selected.run_id,
            "plan_identity": identity,
        }
        if (
            manifest.get("schema") != 1
            or manifest.get("contract_kind") != "run-manifest"
            or manifest.get("owner") != selected.owner
            or manifest.get("operation") != selected.operation
            or manifest.get("variant") != selected.variant
            or manifest.get("run_id") != selected.run_id
            or manifest.get("source") != {"plan_identity": identity}
        ):
            raise RunStoreError("execution manifest identity or closure drift")
        if (
            plan.get("schema") != 4
            or plan.get("contract_kind") != "execution-plan"
            or plan.get("owner") != selected.owner
            or plan.get("operation") != selected.operation
            or plan.get("variant") != selected.variant
        ):
            raise RunStoreError("persisted execution plan identity drift")
        if (
            result.get("schema") != 2
            or result.get("contract_kind") != "run-result"
            or any(result.get(name) != value for name, value in expected.items())
            or result.get("status")
            not in {"succeeded", "failed", "partial", "uncertain", "cancelled"}
            or manifest.get("status") != result.get("status")
            or manifest.get("operation_id") != result.get("operation_id")
        ):
            raise RunStoreError("persisted run result identity drift")
        details = manifest.get("details")
        result_text = read_nofollow_text(
            selected.paths.role("outputs") / "run-result.json"
        )
        if (
            not isinstance(details, Mapping)
            or details.get("run_status") != result["status"]
            or details.get("result_sha256")
            != hashlib.sha256(result_text.encode("utf-8")).hexdigest()
        ):
            raise RunStoreError("persisted run result digest drift")
        planned_steps = plan.get("steps")
        result_steps = result.get("steps")
        if (
            not isinstance(planned_steps, list)
            or not isinstance(result_steps, list)
            or [
                (step.get("id"), step.get("uses"))
                for step in planned_steps
                if isinstance(step, Mapping)
            ]
            != [
                (step.get("id"), step.get("uses"))
                for step in result_steps
                if isinstance(step, Mapping)
            ]
            or len(planned_steps) != len(result_steps)
        ):
            raise RunStoreError("persisted run step lineage drift")
        registered = self._registered_files(manifest)
        for step in result_steps:
            if not isinstance(step, Mapping) or not isinstance(step.get("artifacts"), list):
                raise RunStoreError("persisted run step is malformed")
            for artifact in step["artifacts"]:
                if not isinstance(artifact, Mapping) or not isinstance(
                    artifact.get("path"), str
                ):
                    raise RunStoreError("persisted run artifact is malformed")
                relative = Path(artifact["path"])
                path = root / relative
                if (
                    relative.is_absolute()
                    or ".." in relative.parts
                    or artifact["path"] not in registered
                    or path.resolve() != path.absolute()
                    or not path.is_file()
                ):
                    raise RunStoreError("persisted run artifact is unsafe or unregistered")
        return manifest, plan, result

    def read(
        self,
        *,
        owner: str,
        operation: str,
        variant: str | None = None,
        run_id: str,
    ) -> RunResult | RunFailure:
        selected = self._select(
            owner=owner,
            operation=operation,
            variant=variant,
            run_id=run_id,
        )
        return self._read_selected(selected)

    def read_if_present(
        self,
        *,
        owner: str,
        operation: str,
        variant: str | None = None,
        run_id: str,
    ) -> RunResult | RunFailure | None:
        selected = self._select(
            owner=owner,
            operation=operation,
            variant=variant,
            run_id=run_id,
        )
        if not selected.paths.root.exists():
            return None
        return self._read_selected(selected)

    @staticmethod
    def _typed_result(result: Mapping[str, Any], root: Path) -> RunResult:
        try:
            outcomes: list[StepOutcome] = []
            for raw_step in result["steps"]:
                artifacts = tuple(
                    Artifact(
                        raw["role"],
                        raw["kind"],
                        root.joinpath(*Path(raw["path"]).parts),
                        raw["qualifiers"],
                    )
                    for raw in raw_step["artifacts"]
                )
                step_result = StepResult(
                    raw_step["status"],
                    artifacts,
                    raw_step["facts"],
                    raw_step["message"],
                )
                outcomes.append(
                    StepOutcome(raw_step["id"], raw_step["uses"], step_result)
                )
            return RunResult(
                result["owner"],
                result["operation"],
                result["variant"],
                result["run_id"],
                result["operation_id"],
                result["plan_identity"],
                result["status"],
                tuple(outcomes),
                root,
            )
        except (ContractError, KeyError, TypeError, ValueError) as exc:
            raise RunStoreError(f"persisted run result is malformed: {exc}") from exc

    def _read_selected(self, selected: _SelectedRun) -> RunResult | RunFailure:
        manifest = self._manifest(selected)
        if "outputs/run-result.json" in manifest.get("completion_evidence", ()):
            _manifest, _plan, result = self._records(selected)
            return self._typed_result(result, selected.paths.root)
        details = manifest.get("details")
        error_type = details.get("error_type") if isinstance(details, Mapping) else None
        message = details.get("error") if isinstance(details, Mapping) else None
        try:
            return RunFailure(
                selected.owner,
                selected.operation,
                selected.variant,
                selected.run_id,
                manifest.get("operation_id"),
                manifest["source"]["plan_identity"],
                manifest["status"],
                error_type,
                message,
                manifest.get("partial_failure") or {},
            )
        except (ContractError, TypeError) as exc:
            raise RunStoreError(f"persisted run failure is malformed: {exc}") from exc

    def clean(
        self,
        *,
        owner: str,
        operation: str,
        variant: str | None = None,
        run_id: str,
    ) -> None:
        selected = self._select(
            owner=owner,
            operation=operation,
            variant=variant,
            run_id=run_id,
        )
        root = selected.paths.root
        try:
            expected = root.stat(follow_symlinks=False)
        except OSError as exc:
            raise RunStoreError(f"missing execution run: {run_id}") from exc
        if not stat.S_ISDIR(expected.st_mode):
            raise RunStoreError(f"missing or unsafe execution run: {run_id}")
        manifest = self._manifest(selected)
        if "outputs/run-result.json" in manifest.get("completion_evidence", ()):
            self._records(selected)
        self._validate_inventory(selected.paths, manifest)
        _remove_nofollow_tree(root, expected)


__all__ = ["RunStoreError"]
