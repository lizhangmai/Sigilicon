"""The bounded I/O capability available to one executing adapter."""

from __future__ import annotations
from dataclasses import dataclass, field
import hashlib
from pathlib import Path, PurePosixPath
import re
import stat
from types import MappingProxyType
from typing import Any, Callable, Mapping
from sigilicon.artifacts import SafeTree, copy_immutable_file, read_nofollow_bytes, read_nofollow_text, write_immutable_text
from sigilicon.paths import validate_artifact_component, validate_artifact_id
from sigilicon.execution._resources import Resources
from sigilicon.execution._plan import Step
from sigilicon.execution._result import Artifact, StepResult
from sigilicon.execution.artifact_reference import ArtifactReference
from sigilicon.execution._values import (
    ContractError,
    ExecutionError,
    _identifier,
    resource_identity,
    resource_materialization_key,
)
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from sigilicon.execution._workspace import ExecutionWorkspace


@dataclass(frozen=True)
class ExecutionIO:
    """Deep managed-I/O interface supplied to one trusted Adapter."""

    plan_identity: str
    step: Step
    run_id: str
    operation_id: str
    _run_root: Path
    _resources: Resources
    _dependencies: Mapping[str, StepResult]
    owner: str
    _source_scopes: Mapping[str, str] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )
    _resource_digests: Mapping[str, str] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )
    _resource_kinds: Mapping[str, str] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )
    _register_mutation: Callable[[Any], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _source_paths: Mapping[str, Path] = field(
        init=False,
        repr=False,
        compare=False,
    )
    _scoped_source_paths: Mapping[tuple[str, str], Path] = field(
        init=False,
        repr=False,
        compare=False,
    )
    _resource_paths: Mapping[str, Path] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.step, Step):
            raise ContractError("execution I/O requires a Step")
        object.__setattr__(self, "owner", _identifier(self.owner, "execution owner"))
        validate_artifact_id(self.run_id, "run id")
        validate_artifact_id(self.operation_id, "operation id")
        validate_artifact_id(self.plan_identity, "plan identity")
        object.__setattr__(self, "_run_root", Path(self._run_root).absolute())
        if not isinstance(self._source_scopes, Mapping) or any(
            name not in self.step.sources or scope not in {"owner", "project"}
            for name, scope in self._source_scopes.items()
        ):
            raise ContractError("execution I/O source scopes disagree with its Step")
        if not isinstance(self._dependencies, Mapping) or any(
            not isinstance(name, str) or not isinstance(result, StepResult)
            for name, result in self._dependencies.items()
        ):
            raise ContractError("step dependencies must map names to StepResult values")
        if set(self._dependencies) != set(self.step.needs):
            raise ContractError("execution I/O dependency closure disagrees with the plan")
        run_root = self._run_root
        if run_root == Path(run_root.anchor):
            raise ContractError("execution I/O run root cannot be a filesystem root")
        if (
            not isinstance(self._resource_digests, Mapping)
            or set(self._resource_digests) != set(self.step.resources)
            or any(
                not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                for digest in self._resource_digests.values()
            )
        ):
            raise ContractError(
                "execution I/O resource digests disagree with its resource closure"
            )
        if (
            not isinstance(self._resource_kinds, Mapping)
            or set(self._resource_kinds) != set(self.step.resources)
            or any(
                kind not in {"tool", "file", "directory", "value", "destination"}
                for kind in self._resource_kinds.values()
            )
        ):
            raise ContractError(
                "execution I/O resource kinds disagree with its resource closure"
            )
        object.__setattr__(
            self, "_dependencies", MappingProxyType(dict(self._dependencies))
        )
        object.__setattr__(
            self,
            "_source_scopes",
            MappingProxyType(dict(self._source_scopes)),
        )
        object.__setattr__(
            self,
            "_resource_digests",
            MappingProxyType(dict(self._resource_digests)),
        )
        object.__setattr__(
            self,
            "_resource_kinds",
            MappingProxyType(dict(self._resource_kinds)),
        )
        source_paths = {
            name: self.source_directory.joinpath(*PurePosixPath(name).parts)
            for name in self.step.sources
        }
        object.__setattr__(self, "_source_paths", MappingProxyType(source_paths))
        object.__setattr__(
            self,
            "_scoped_source_paths",
            MappingProxyType(
                {
                    (scope, name): source_paths[name]
                    for name, scope in self._source_scopes.items()
                }
            ),
        )
        resource_root = self.resource_directory
        object.__setattr__(
            self,
            "_resource_paths",
            MappingProxyType(
                {
                    name: resource_root / resource_materialization_key(name)
                    for name, kind in self._resource_kinds.items()
                    if kind in {"file", "directory"}
                    and resource_root is not None
                }
            ),
        )

    @property
    def work_directory(self) -> Path:
        """Return the Adapter's managed scratch directory."""

        return self._run_root / "work" / self.step.id

    @property
    def output_directory(self) -> Path:
        """Return the root below which the Adapter may publish artifacts."""

        return self._run_root / "outputs" / self.step.id

    @property
    def source_directory(self) -> Path:
        """Return the immutable source closure root."""

        return self._run_root / "inputs" / "sources"

    @property
    def resource_directory(self) -> Path | None:
        """Return the immutable data-resource root when the Step has one."""

        if any(
            kind in {"file", "directory"}
            for kind in self._resource_kinds.values()
        ):
            return self._run_root / "inputs" / "resources"
        return None

    @property
    def runtime(self) -> Resources:
        """Return the plan-filtered runtime deployment."""

        return self._resources

    def source_path(self, source: str) -> Path:
        """Return a run-local tool path for trusted package adapter code."""

        name = source
        relative = PurePosixPath(name)
        if (
            not name
            or relative.is_absolute()
            or "\\" in name
            or relative.as_posix() != name
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ExecutionError(f"source path must be canonical and relative: {name!r}")
        result = self._source_paths.get(name)
        if result is None:
            raise ExecutionError(f"source is outside this step: {name!r}")
        if (
            result.absolute() != result
            or result.resolve() != result
            or not result.is_file()
            or result.is_symlink()
        ):
            raise ExecutionError(f"sealed source is missing or unsafe: {name!r}")
        return result

    def source_text(self, source: str) -> str:
        """Read a step source through the held-fd no-follow input primitive."""

        return read_nofollow_text(self.source_path(source))

    def resource_path(self, resource: str) -> Path:
        """Return one sealed external resource selected by this Step."""

        name = resource_identity(resource)
        if self._resource_kinds.get(name) in {"tool", "value", "destination"}:
            raise ExecutionError(f"external resource is not sealed data: {name!r}")
        result = self._resource_paths.get(name)
        if result is None:
            raise ExecutionError(f"external resource is outside this step: {name!r}")
        expected_kind = self._resource_kinds[name]
        try:
            metadata = result.stat(follow_symlinks=False)
        except OSError as exc:
            raise ExecutionError(
                f"sealed external resource is missing or unsafe: {name!r}"
            ) from exc
        if (
            result.absolute() != result
            or result.resolve() != result
            or result.is_symlink()
            or (
                expected_kind == "file"
                and not stat.S_ISREG(metadata.st_mode)
            )
            or (
                expected_kind == "directory"
                and not stat.S_ISDIR(metadata.st_mode)
            )
        ):
            raise ExecutionError(
                f"sealed external resource is missing or unsafe: {name!r}"
            )
        return result

    def resource_text(self, resource: str) -> str:
        try:
            return self.resource_bytes(resource).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ExecutionError(
                f"sealed external resource is not UTF-8: {resource!r}"
            ) from exc

    def resource_bytes(self, resource: str) -> bytes:
        name = resource_identity(resource)
        if self._resource_kinds.get(name) != "file":
            raise ExecutionError(f"sealed external resource is not a file: {name!r}")
        data = read_nofollow_bytes(self.resource_path(name))
        if hashlib.sha256(data).hexdigest() != self._resource_digests[name]:
            raise ExecutionError(
                f"sealed external resource identity drift: {name!r}"
            )
        return data

    def scoped_source_path(self, scope: str, source: str) -> Path:
        """Resolve one owner- or project-relative source from the sealed closure."""

        result = self._scoped_source_paths.get((scope, source))
        if result is None:
            raise ExecutionError(
                f"step source {scope}:{source} is missing or ambiguous"
            )
        return self.source_path(source)

    def owner_source_path(self, source: str) -> Path:
        return self.scoped_source_path("owner", source)

    def register_mutation(self, operation: Any) -> None:
        """Attach one trusted mutation journal to this managed run."""

        if self._register_mutation is None:
            raise ExecutionError("execution I/O cannot register a mutation")
        if getattr(operation, "operation_id", None) != self.operation_id:
            raise ExecutionError("mutation identity disagrees with this run")
        self._register_mutation(operation)

    def output_path(self, role: str, filename: str) -> Path:
        """Resolve a step-relative filename; the logical role adds no directory."""

        validate_artifact_component(role, "output role")
        relative = PurePosixPath(filename)
        if (
            not relative.parts
            or relative.is_absolute()
            or "\\" in filename
            or relative.as_posix() != filename
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ExecutionError(f"output filename must be canonical and relative: {filename!r}")
        for component in relative.parts:
            validate_artifact_component(component, "output filename component")
        return self.output_directory.joinpath(*relative.parts)

    def write_text(self, role: str, filename: str, value: str) -> Path:
        """Create one immutable text output without following path components."""

        path = self.output_path(role, filename)
        write_immutable_text(path, value)
        return path

    def copy_output(
        self,
        role: str,
        kind: str,
        source: Path,
        filename: str,
    ) -> Artifact:
        """Publish one immutable regular file from tool scratch space."""

        if not source.is_file() or source.is_symlink():
            raise ExecutionError(f"tool omitted required {role!r} output")
        destination = self.output_path(role, filename)
        copy_immutable_file(source, destination)
        return Artifact(role, kind, destination)

    def output_artifacts(
        self,
        role: str,
        kind: str,
        *,
        directory: str,
        required: bool = False,
    ) -> tuple[Artifact, ...]:
        """Publish an explicitly located bundle under one logical artifact role."""

        root = self.output_path(role, directory)
        if not root.is_dir() or root.is_symlink():
            if required:
                raise ExecutionError(f"tool omitted required {role!r} directory")
            return ()
        artifacts = tuple(
            Artifact(role, kind, path.absolute())
            for path in sorted(root.rglob("*"))
            if path.is_file() and not path.is_symlink()
        )
        if required and not artifacts:
            raise ExecutionError(f"tool produced an empty {role!r} directory")
        return artifacts

    def workspace(
        self,
        directory: str,
        source: Mapping[str, Any],
        *,
        tool_work_root: Path | None = None,
    ) -> "ExecutionWorkspace":
        """Create a tool workspace with an explicit output bundle directory."""

        from sigilicon.execution._workspace import ExecutionWorkspace

        bundle = validate_artifact_component(directory, "output bundle directory")
        return ExecutionWorkspace(
            run_id=self.run_id,
            root=self._run_root,
            input_root=self.work_directory / "inputs",
            work_root=(
                self.work_directory / "tool"
                if tool_work_root is None
                else Path(tool_work_root).absolute()
            ),
            output_root=self.output_directory / bundle,
            log_root=self.work_directory / "logs",
            source=source,
        )

    def artifacts(self, reference: ArtifactReference) -> tuple[Artifact, ...]:
        """Resolve a nonempty, format-checked dependency without silently filtering errors."""

        if not isinstance(reference, ArtifactReference):
            raise ExecutionError("artifact consumption requires an ArtifactReference")
        result = self._dependencies.get(reference.step)
        if result is None or result.status != "succeeded":
            raise ExecutionError(f"dependency {reference.step!r} is missing or unsuccessful")
        root = self._run_root / "outputs" / reference.step
        artifacts = tuple(item for item in result.artifacts if item.role == reference.role
                          and (reference.path is None or item.path == root / reference.path))
        if (not artifacts or (reference.cardinality == "one" and len(artifacts) != 1)
                or any(item.kind != reference.kind for item in artifacts)):
            raise ExecutionError(f"artifact {reference.step}/{reference.role} disagrees with its format or cardinality")
        return artifacts

    def materialize_artifact(self, reference: ArtifactReference, destination: Path) -> Path:
        """Copy one registered artifact and verify the copied bytes against its digest."""

        if reference.cardinality != "one":
            raise ExecutionError("file materialization requires singular cardinality")
        artifact, = self.artifacts(reference)
        if artifact.sha256 is None:
            raise ExecutionError("materialization requires a registered artifact identity")
        destination = Path(destination).absolute()
        if not destination.is_relative_to(self.work_directory):
            raise ExecutionError("artifact materialization must stay in step scratch space")
        copy_immutable_file(artifact.path, destination,
                            expected_size=artifact.size, expected_sha256=artifact.sha256)
        return destination

    def artifact_directory(self, reference: ArtifactReference, directory: str) -> Path:
        """Resolve an explicitly named bundle root and verify its complete file inventory."""

        from sigilicon.contracts import require_relative_path

        if reference.cardinality != "many":
            raise ExecutionError("artifact directory requires multiple-file cardinality")
        relative = require_relative_path(directory, "artifact bundle directory")
        root = self._run_root / "outputs" / reference.step / relative
        artifacts = self.artifacts(reference)
        expected = {}
        for artifact in artifacts:
            if not artifact.path.is_relative_to(root) or artifact.sha256 is None:
                raise ExecutionError("artifact bundle does not match its declared root")
            expected[artifact.path.relative_to(root).as_posix()] = (artifact.size, artifact.sha256)
        inventory = SafeTree(root).inventory(verify_content=True)
        actual = {path.as_posix(): (item.size, item.sha256)
                  for path, item in inventory.files.items()}
        if actual != expected:
            raise ExecutionError("artifact bundle inventory or content drifted")
        return root
