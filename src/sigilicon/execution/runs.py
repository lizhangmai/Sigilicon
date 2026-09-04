"""Read and remove closed execution runs without loading current owner source."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from pathlib import PurePosixPath
import re
from typing import Any, Mapping

from sigilicon.artifacts import (
    SafeTree,
    load_manifest,
    read_json_object,
    read_nofollow_text,
)
from sigilicon.canonical import canonical_digest
from sigilicon.execution._model import (
    Artifact,
    ContractError,
    RunFailure,
    RunResult,
    StepOutcome,
    StepResult,
    adapter_identity,
    resource_identity,
    resource_materialization_key,
    validate_resource_record,
)
from sigilicon.paths import ArtifactLayout, RunPaths


_DIGEST = re.compile(r"sha256-[0-9a-f]{64}\Z")


class RunStoreError(ValueError):
    """A requested run is missing, unsafe, or internally inconsistent."""


@dataclass(frozen=True)
class _SelectedRun:
    paths: RunPaths
    owner: str
    operation: str
    variant: str | None
    run_id: str


@dataclass(frozen=True)
class RunStore:
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
        registered: set[str] = set()
        for entries in files.values():
            if not isinstance(entries, list):
                raise RunStoreError("run manifest file inventory is malformed")
            for entry in entries:
                path = entry.get("path") if isinstance(entry, Mapping) else None
                if not isinstance(path, str) or path in registered:
                    raise RunStoreError(
                        "run manifest file inventory contains an invalid path"
                    )
                registered.add(path)
        return registered

    @classmethod
    def _validate_inventory(
        cls,
        paths: RunPaths,
        manifest: Mapping[str, Any],
        *,
        verify_content: bool = False,
    ) -> None:
        references: dict[str, dict[Path, Mapping[str, Any]]] = {
            role: {} for role in paths.roles
        }
        for entries in manifest["files"].values():
            for entry in entries:
                value = Path(entry["path"])
                role = value.parts[0]
                relative = Path(*value.parts[1:])
                if relative in references[role]:
                    raise RunStoreError("run manifest file inventory is duplicated")
                references[role][relative] = entry
        for role in paths.roles:
            try:
                inventory = SafeTree(paths.role(role)).inventory(
                    verify_content=verify_content
                )
            except (OSError, RuntimeError) as exc:
                raise RunStoreError(f"run role root is unsafe: {exc}") from exc
            role_references = references[role]
            expected_files = {
                relative
                for relative, reference in role_references.items()
                if reference["kind"] == "file"
            }
            explicit_directories = {
                relative
                for relative, reference in role_references.items()
                if reference["kind"] == "directory"
            }
            expected_directories = {
                parent
                for relative in role_references
                for parent in relative.parents
                if parent != Path(".")
            } | explicit_directories
            if role != "work" and (
                set(inventory.files) != expected_files
                or set(inventory.directories) != expected_directories
            ):
                raise RunStoreError(
                    f"run {role} inventory disagrees with its manifest"
                )
            for relative, reference in role_references.items():
                if reference["kind"] == "directory":
                    if relative not in inventory.directories:
                        raise RunStoreError(
                            "run directory metadata disagrees with its manifest"
                        )
                    continue
                file = inventory.files.get(relative)
                if (
                    file is None
                    or file.size != reference["size"]
                    or (
                        verify_content
                        and file.sha256 != reference.get("sha256")
                    )
                ):
                    raise RunStoreError(
                        "run file metadata disagrees with its manifest"
                    )

    def _manifest(
        self,
        selected: _SelectedRun,
        *,
        verify_content: bool = False,
        require_terminal: bool = True,
        validate_inventory: bool = True,
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
            manifest.get("schema") != 3
            or manifest.get("contract_kind") != "run-manifest"
            or manifest.get("owner") != selected.owner
            or manifest.get("operation") != selected.operation
            or manifest.get("variant") != selected.variant
            or manifest.get("run_id") != selected.run_id
            or (
                require_terminal
                and manifest.get("status")
                not in {"succeeded", "failed", "partial", "uncertain", "cancelled"}
            )
            or not isinstance(source, Mapping)
            or not isinstance(source.get("plan_identity"), str)
        ):
            raise RunStoreError("execution manifest identity or terminal state drift")
        if validate_inventory:
            self._validate_inventory(
                selected.paths,
                manifest,
                verify_content=verify_content,
            )
        return manifest

    def _records(
        self,
        selected: _SelectedRun,
        manifest: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        root = selected.paths.root
        try:
            plan = read_json_object(
                selected.paths.role("inputs") / "execution-plan.json",
                "Execution Plan",
            )
            result = read_json_object(
                selected.paths.role("outputs") / "run-result.json",
                "Run Result",
            )
            runtime_bindings = read_json_object(
                selected.paths.role("inputs") / "runtime-bindings.json",
                "Runtime Bindings",
            )
        except (OSError, RuntimeError) as exc:
            raise RunStoreError(str(exc)) from exc
        plan_resources = plan.get("resources")
        if not isinstance(plan_resources, list):
            raise RunStoreError("persisted execution plan resources are malformed")
        self._validate_runtime_bindings(
            selected.paths,
            runtime_bindings,
            plan_resources,
        )
        identity = canonical_digest(plan)
        expected = {
            "owner": selected.owner,
            "operation": selected.operation,
            "variant": selected.variant,
            "run_id": selected.run_id,
            "plan_identity": identity,
        }
        if (
            manifest.get("schema") != 3
            or manifest.get("contract_kind") != "run-manifest"
            or manifest.get("owner") != selected.owner
            or manifest.get("operation") != selected.operation
            or manifest.get("variant") != selected.variant
            or manifest.get("run_id") != selected.run_id
            or manifest.get("source") != {"plan_identity": identity}
        ):
            raise RunStoreError("execution manifest identity or closure drift")
        if (
            plan.get("schema") != 14
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

    @staticmethod
    def _validate_runtime_bindings(
        paths: RunPaths,
        value: Mapping[str, Any],
        resources: list[Any],
    ) -> None:
        if set(value) != {
            "schema",
            "contract_kind",
            "capabilities",
            "inherit_environment",
            "environment",
            "configuration",
        } or value.get("schema") != 6 or value.get("contract_kind") != (
            "runtime-bindings"
        ):
            raise RunStoreError("persisted runtime bindings have an invalid shape")
        capabilities = value.get("capabilities")
        inherit_environment = value.get("inherit_environment")
        environment = value.get("environment")
        configuration = value.get("configuration")
        if not isinstance(capabilities, list) or any(
            not isinstance(capability, str) for capability in capabilities
        ):
            raise RunStoreError("persisted runtime bindings are not canonical")
        if (
            capabilities != sorted(set(capabilities))
            or not isinstance(inherit_environment, list)
            or any(not isinstance(name, str) for name in inherit_environment)
            or inherit_environment != list(dict.fromkeys(inherit_environment))
            or not isinstance(environment, Mapping)
            or list(environment) != sorted(environment)
            or not set(inherit_environment).issubset(environment)
            or any(
                digest is not None
                and (
                    not isinstance(digest, str)
                    or _DIGEST.fullmatch(digest) is None
                )
                for digest in environment.values()
            )
        ):
            raise RunStoreError("persisted runtime bindings are not canonical")
        if not isinstance(configuration, Mapping) or set(configuration) != {
            "tools",
            "files",
            "directories",
            "values",
        }:
            raise RunStoreError("persisted runtime configuration is malformed")
        configured_identities: set[str] = set()
        for label in ("tools", "files", "directories", "values"):
            table = configuration[label]
            if not isinstance(table, Mapping) or list(table) != sorted(table):
                raise RunStoreError("persisted runtime configuration is not canonical")
            for name, configured in table.items():
                try:
                    identity = resource_identity(name)
                except ContractError as exc:
                    raise RunStoreError(
                        "persisted runtime configuration identity is invalid"
                    ) from exc
                if (
                    identity in configured_identities
                    or not isinstance(configured, str)
                    or not configured
                    or (label != "values" and not Path(configured).is_absolute())
                ):
                    raise RunStoreError(
                        "persisted runtime configuration value is invalid"
                    )
                configured_identities.add(identity)
        try:
            for capability in capabilities:
                adapter_identity(capability)
        except ContractError as exc:
            raise RunStoreError("persisted runtime capability is invalid") from exc

        resource_root = paths.role("inputs") / "resources"
        identities: set[str] = set()
        ordered_identities: list[str] = []
        expected_files: dict[Path, tuple[int, bool]] = {}
        expected_directories: set[Path] = set()
        for record in resources:
            if not isinstance(record, Mapping):
                raise RunStoreError("persisted runtime resource record is malformed")
            identity = record.get("identity")
            if not isinstance(identity, str) or identity in identities:
                raise RunStoreError("persisted runtime resource identity is invalid")
            identities.add(identity)
            ordered_identities.append(identity)
            try:
                materialization_key = resource_materialization_key(identity)
                kind = validate_resource_record(record)
            except (RuntimeError, ContractError) as exc:
                raise RunStoreError(
                    "persisted runtime resource record is unsafe"
                ) from exc
            if kind == "file":
                expected_files[Path(materialization_key)] = (
                    record["size"],
                    record["executable"],
                )
            elif kind == "directory":
                root = Path(materialization_key)
                expected_directories.add(root)
                expected_directories.update(
                    root.joinpath(*PurePosixPath(path).parts)
                    for path in record["directories"]
                )
                expected_files.update(
                    {
                        root.joinpath(*PurePosixPath(item["path"]).parts): (
                            item["size"],
                            item["executable"],
                        )
                        for item in record["files"]
                    }
                )
        if ordered_identities != sorted(ordered_identities):
            raise RunStoreError("persisted runtime resources are not canonical")
        if not configured_identities.issubset(identities):
            raise RunStoreError(
                "persisted runtime configuration is outside the plan closure"
            )
        if expected_files or expected_directories:
            try:
                inventory = SafeTree(resource_root).inventory(verify_content=False)
            except (OSError, RuntimeError) as exc:
                raise RunStoreError(
                    "persisted runtime resource closure is missing"
                ) from exc
            if (
                set(inventory.files) != set(expected_files)
                or set(inventory.directories) != expected_directories
            ):
                raise RunStoreError("persisted runtime resource closure drift")
            if any(
                (file.size, bool(file.mode & 0o111)) != expected_files[path]
                for path, file in inventory.files.items()
            ):
                raise RunStoreError("persisted runtime resource identity drift")
        elif resource_root.exists():
            raise RunStoreError("persisted runtime resource closure is unexpected")

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

    @staticmethod
    def _typed_result(
        result: Mapping[str, Any],
        root: Path,
        manifest: Mapping[str, Any],
    ) -> RunResult:
        try:
            registered = {
                entry["path"]: entry
                for entries in manifest["files"].values()
                for entry in entries
            }
            outcomes: list[StepOutcome] = []
            for raw_step in result["steps"]:
                artifacts: list[Artifact] = []
                for raw in raw_step["artifacts"]:
                    record = registered[raw["path"]]
                    original = root.joinpath(*Path(raw["path"]).parts)
                    if (
                        record.get("kind") != "file"
                        or not isinstance(record.get("size"), int)
                        or not isinstance(record.get("sha256"), str)
                    ):
                        raise RunStoreError("persisted artifact reference is malformed")
                    artifacts.append(
                        Artifact(
                            raw["role"],
                            raw["kind"],
                            original,
                            raw["qualifiers"],
                            size=record["size"],
                            sha256=record["sha256"],
                        )
                    )
                step_result = StepResult(
                    raw_step["status"],
                    tuple(artifacts),
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
            )
        except (ContractError, KeyError, TypeError, ValueError) as exc:
            raise RunStoreError(f"persisted run result is malformed: {exc}") from exc

    def _read_selected(self, selected: _SelectedRun) -> RunResult | RunFailure:
        manifest = self._manifest(selected, verify_content=True)
        if "outputs/run-result.json" in manifest.get("completion_evidence", ()):
            manifest, _plan, result = self._records(selected, manifest)
            return self._typed_result(result, selected.paths.root, manifest)
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
            tree = SafeTree(root)
        except (OSError, RuntimeError) as exc:
            raise RunStoreError(f"missing execution run: {run_id}") from exc
        manifest = self._manifest(
            selected,
            require_terminal=False,
            validate_inventory=False,
        )
        if manifest["status"] != "running":
            self._validate_inventory(selected.paths, manifest)
        if "outputs/run-result.json" in manifest.get("completion_evidence", ()):
            self._records(selected, manifest)
        try:
            tree.remove(expected)
        except (OSError, RuntimeError) as exc:
            raise RunStoreError(f"could not safely clean execution run: {exc}") from exc

    def audit(
        self,
        *,
        owner: str,
        operation: str,
        variant: str | None = None,
        run_id: str,
    ) -> None:
        """Stream-verify every registered payload in one terminal run."""

        selected = self._select(
            owner=owner,
            operation=operation,
            variant=variant,
            run_id=run_id,
        )
        self._manifest(selected, verify_content=True)


__all__ = ["RunStore", "RunStoreError"]
