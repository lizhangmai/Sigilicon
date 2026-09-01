"""Read and remove closed execution runs without loading current owner source."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any, Mapping

from sigilicon.artifacts import load_manifest, read_json_object, read_nofollow_text
from sigilicon.canonical import canonical_digest
from sigilicon.execution.model import (
    Artifact,
    ContractError,
    RunFailure,
    RunResult,
    StepOutcome,
    StepResult,
)
from sigilicon.paths import ArtifactExecutionPaths, ArtifactLayout, ProjectContext


class RunStoreError(ValueError):
    """A requested run is missing, unsafe, or internally inconsistent."""


@dataclass(frozen=True)
class _SelectedRun:
    paths: ArtifactExecutionPaths
    owner: str
    target: str
    operation: str
    run_id: str


@dataclass(frozen=True)
class RunStore:
    """Read or clean one exact managed execution result."""

    context: ProjectContext

    def __post_init__(self) -> None:
        if not isinstance(self.context, ProjectContext):
            raise TypeError("RunStore requires an explicit ProjectContext")
        if self.context.artifact_root == Path(self.context.artifact_root.anchor):
            raise RunStoreError("artifact root cannot be a filesystem root")

    @property
    def artifact_root(self) -> Path:
        return self.context.artifact_root

    def _select(
        self,
        *,
        owner: str,
        target: str,
        operation: str,
        run_id: str,
    ) -> _SelectedRun:
        try:
            paths = ArtifactLayout(self.artifact_root).execution(
                owner=owner,
                target=target,
                flow=operation,
                variant="default",
                identity=run_id,
                artifact_kind="execution-run",
                identity_kind="run_id",
            )
        except (RuntimeError, ValueError) as exc:
            raise RunStoreError(str(exc)) from exc
        return _SelectedRun(paths, owner, target, operation, run_id)

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
        paths: ArtifactExecutionPaths,
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
                    or (
                        manifest.get("status") == "succeeded"
                        and (
                            metadata.st_size != reference["size"]
                            or digest != reference.get("sha256")
                        )
                    )
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
            manifest.get("artifact_kind") != "execution-run"
            or manifest.get("entities")
            != {"owner": selected.owner, "target": selected.target}
            or manifest.get("operation") != selected.operation
            or manifest.get("run_id") != selected.run_id
            or manifest.get("status") not in {"succeeded", "failed", "partial", "uncertain"}
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
        if manifest.get("status") != "succeeded":
            raise RunStoreError("execution did not persist a complete run result")
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
            "target": selected.target,
            "operation": selected.operation,
            "run_id": selected.run_id,
            "plan_identity": identity,
        }
        if (
            manifest.get("artifact_kind") != "execution-run"
            or manifest.get("entities")
            != {"owner": selected.owner, "target": selected.target}
            or manifest.get("operation") != selected.operation
            or manifest.get("run_id") != selected.run_id
            or manifest.get("source") != {"plan_identity": identity}
        ):
            raise RunStoreError("execution manifest identity or closure drift")
        if (
            plan.get("schema") != 1
            or plan.get("contract_kind") != "execution-plan"
            or plan.get("owner") != selected.owner
            or plan.get("target") != selected.target
            or plan.get("operation") != selected.operation
        ):
            raise RunStoreError("persisted execution plan identity drift")
        if (
            result.get("schema") != 1
            or result.get("contract_kind") != "run-result"
            or any(result.get(name) != value for name, value in expected.items())
            or result.get("status")
            not in {"succeeded", "failed", "partial", "uncertain", "cancelled"}
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
        target: str,
        operation: str,
        run_id: str,
    ) -> RunResult | RunFailure:
        selected = self._select(
            owner=owner,
            target=target,
            operation=operation,
            run_id=run_id,
        )
        return self._read_selected(selected)

    def read_if_present(
        self,
        *,
        owner: str,
        target: str,
        operation: str,
        run_id: str,
    ) -> RunResult | RunFailure | None:
        selected = self._select(
            owner=owner,
            target=target,
            operation=operation,
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
                result["target"],
                result["operation"],
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
        if manifest["status"] == "succeeded":
            _manifest, _plan, result = self._records(selected)
            return self._typed_result(result, selected.paths.root)
        details = manifest.get("details")
        error_type = details.get("error_type") if isinstance(details, Mapping) else None
        message = details.get("error") if isinstance(details, Mapping) else None
        try:
            return RunFailure(
                selected.owner,
                selected.target,
                selected.operation,
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
        target: str,
        operation: str,
        run_id: str,
    ) -> None:
        selected = self._select(
            owner=owner,
            target=target,
            operation=operation,
            run_id=run_id,
        )
        manifest = self._manifest(selected)
        if manifest["status"] == "succeeded":
            self._records(selected)
        root = selected.paths.root
        self._validate_inventory(selected.paths, manifest)
        actual = {path.relative_to(root) for path in root.rglob("*")}
        for relative in sorted(actual, key=lambda path: len(path.parts), reverse=True):
            path = root / relative
            if path.is_dir() and not path.is_symlink():
                path.rmdir()
            else:
                path.unlink()
        root.rmdir()


__all__ = ["RunStore", "RunStoreError"]
