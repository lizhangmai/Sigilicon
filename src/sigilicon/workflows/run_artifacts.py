"""Artifact workspace seam owned by one parent Flow execution."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from sigilicon.artifacts import (
    copy_immutable_file,
    ensure_nofollow_directory,
    read_nofollow_text,
    write_immutable_text,
)
from sigilicon.flow import ActionContext
from sigilicon.paths import validate_artifact_component


class RunArtifacts(Protocol):
    """Paths and writes available to one already-owned execution lifecycle."""

    run_id: str
    root: Path
    source: Mapping[str, Any]

    def path(self, role: str, *components: str) -> Path: ...

    def directory(self, role: str, *components: str) -> Path: ...

    def write_text(
        self,
        role: str,
        components: Sequence[str],
        value: str,
    ) -> Path: ...

    def write_json(
        self,
        role: str,
        components: Sequence[str],
        value: Mapping[str, Any],
    ) -> Path: ...

    def copy_file(
        self,
        role: str,
        components: Sequence[str],
        source: Path,
    ) -> Path: ...

    def add_file(
        self,
        role: str,
        path: Path,
    ) -> object: ...


@dataclass(frozen=True)
class FlowRunArtifacts:
    """RunArtifacts Adapter using directories owned by one Flow Action."""

    context: ActionContext
    output_role: str
    source: Mapping[str, Any]

    @property
    def run_id(self) -> str:
        return self.context.run_root.name

    @property
    def root(self) -> Path:
        return self.context.run_root

    def _root_for(self, role: str) -> Path:
        roots = {
            "inputs": self.context.work_root / "inputs",
            "work": self.context.work_root / "tool",
            "outputs": self.context.output_root / self.output_role,
            "logs": self.context.log_root,
        }
        try:
            root = roots[role]
        except KeyError as exc:
            raise ValueError(f"unknown execution artifact role: {role!r}") from exc
        return ensure_nofollow_directory(root)

    def path(self, role: str, *components: str) -> Path:
        root = self._root_for(role)
        current = root
        for component in components:
            current /= validate_artifact_component(component, "artifact path component")
        if not Path(os.path.abspath(current)).is_relative_to(
            Path(os.path.abspath(root))
        ):
            raise RuntimeError("execution artifact escaped its managed role")
        return current

    def directory(self, role: str, *components: str) -> Path:
        result = self.path(role, *components) if components else self._root_for(role)
        return ensure_nofollow_directory(result)

    def write_text(
        self,
        role: str,
        components: Sequence[str],
        value: str,
    ) -> Path:
        destination = self.path(role, *components)
        write_immutable_text(destination, value)
        return destination

    def write_json(
        self,
        role: str,
        components: Sequence[str],
        value: Mapping[str, Any],
    ) -> Path:
        return self.write_text(
            role,
            components,
            json.dumps(value, indent=2, sort_keys=True) + "\n",
        )

    def copy_file(
        self,
        role: str,
        components: Sequence[str],
        source: Path,
    ) -> Path:
        destination = self.path(role, *components)
        return copy_immutable_file(source, destination)

    def add_file(
        self,
        role: str,
        path: Path,
    ) -> object:
        root = Path(os.path.abspath(self._root_for(role)))
        candidate = Path(os.path.abspath(path))
        if not candidate.is_relative_to(root) or not candidate.exists():
            raise RuntimeError("execution artifact is outside its managed role")
        if candidate.is_symlink():
            raise RuntimeError("execution artifact cannot be a symlink")
        if candidate.is_dir():
            ensure_nofollow_directory(candidate)
        else:
            ensure_nofollow_directory(candidate.parent)
        return candidate


@dataclass(frozen=True)
class DirectoryRunArtifacts:
    """Action-owned artifact view reconstructed in one managed child process."""

    run_id: str
    root: Path
    input_root: Path
    work_root: Path
    output_root: Path
    log_root: Path
    source: Mapping[str, Any]

    def _root_for(self, role: str) -> Path:
        roots = {
            "inputs": self.input_root,
            "work": self.work_root,
            "outputs": self.output_root,
            "logs": self.log_root,
        }
        try:
            root = roots[role]
        except KeyError as exc:
            raise ValueError(f"unknown execution artifact role: {role!r}") from exc
        return ensure_nofollow_directory(root)

    def path(self, role: str, *components: str) -> Path:
        root = self._root_for(role)
        current = root
        for component in components:
            current /= validate_artifact_component(
                component, "artifact path component"
            )
        if not Path(os.path.abspath(current)).is_relative_to(
            Path(os.path.abspath(root))
        ):
            raise RuntimeError("execution artifact escaped its managed role")
        return current

    def directory(self, role: str, *components: str) -> Path:
        result = self.path(role, *components) if components else self._root_for(role)
        return ensure_nofollow_directory(result)

    def write_text(
        self,
        role: str,
        components: Sequence[str],
        value: str,
    ) -> Path:
        destination = self.path(role, *components)
        write_immutable_text(destination, value)
        return destination

    def write_json(
        self,
        role: str,
        components: Sequence[str],
        value: Mapping[str, Any],
    ) -> Path:
        return self.write_text(
            role,
            components,
            json.dumps(value, indent=2, sort_keys=True) + "\n",
        )

    def copy_file(
        self,
        role: str,
        components: Sequence[str],
        source: Path,
    ) -> Path:
        return copy_immutable_file(source, self.path(role, *components))

    def add_file(
        self,
        role: str,
        path: Path,
    ) -> object:
        root = Path(os.path.abspath(self._root_for(role)))
        candidate = Path(os.path.abspath(path))
        if not candidate.is_relative_to(root) or not candidate.exists():
            raise RuntimeError("execution artifact is outside its managed role")
        if candidate.is_symlink():
            raise RuntimeError("execution artifact cannot be a symlink")
        if candidate.is_dir():
            ensure_nofollow_directory(candidate)
        else:
            ensure_nofollow_directory(candidate.parent)
        return candidate


_MANAGED_ARTIFACT_CONTEXT = "SIGILICON_MANAGED_RUN_ARTIFACTS"


def managed_run_artifact_environment(
    context: ActionContext,
    output_role: str,
    source: Mapping[str, Any],
) -> dict[str, str]:
    """Write the exact child-process boundary for one current Flow Action."""

    context.action.output(output_role)
    path = context.work_root / "managed-run-artifacts.json"
    write_immutable_text(
        path,
        json.dumps(
            {
                "schema": 1,
                "run_id": context.run_root.name,
                "run_root": str(context.run_root),
                "input_root": str(context.work_root / "inputs"),
                "work_root": str(context.work_root / "tool"),
                "output_root": str(context.output_root / output_role),
                "log_root": str(context.log_root),
                "source": dict(source),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    return {_MANAGED_ARTIFACT_CONTEXT: str(path)}


def managed_run_artifacts_from_environment() -> DirectoryRunArtifacts | None:
    """Recover a parent Action's directories when called from a managed child."""

    value = os.environ.get(_MANAGED_ARTIFACT_CONTEXT)
    if value is None:
        return None
    path = Path(value)
    try:
        payload = json.loads(read_nofollow_text(path))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("cannot read managed run artifact context") from exc
    required = {
        "schema",
        "run_id",
        "run_root",
        "input_root",
        "work_root",
        "output_root",
        "log_root",
        "source",
    }
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != 1
        or set(payload) != required
    ):
        raise RuntimeError("managed run artifact context is invalid")
    run_id = payload.get("run_id")
    source = payload.get("source")
    if not isinstance(run_id, str) or not run_id or not isinstance(source, dict):
        raise RuntimeError("managed run artifact identity is invalid")
    roots = {
        name: Path(payload[name])
        for name in (
            "run_root",
            "input_root",
            "work_root",
            "output_root",
            "log_root",
        )
    }
    if any(not root.is_absolute() for root in roots.values()):
        raise RuntimeError("managed run artifact roots must be absolute")
    run_root = Path(os.path.abspath(roots["run_root"]))
    if run_root.name != run_id or any(
        not Path(os.path.abspath(root)).is_relative_to(run_root)
        for name, root in roots.items()
        if name != "run_root"
    ):
        raise RuntimeError("managed run artifact roots escaped the canonical run")
    context_path = Path(os.path.abspath(path))
    if (
        not context_path.is_relative_to(run_root)
        or not context_path.is_file()
        or context_path.is_symlink()
    ):
        raise RuntimeError("managed run artifact context escaped the canonical run")
    return DirectoryRunArtifacts(
        run_id=run_id,
        root=run_root,
        input_root=roots["input_root"],
        work_root=roots["work_root"],
        output_root=roots["output_root"],
        log_root=roots["log_root"],
        source=source,
    )


def scoped_run_artifacts(
    artifacts: DirectoryRunArtifacts,
    component: str,
) -> DirectoryRunArtifacts:
    """Give one measurement collision-free directories in the same run."""

    name = validate_artifact_component(component, "artifact scope")
    return DirectoryRunArtifacts(
        run_id=artifacts.run_id,
        root=artifacts.root,
        input_root=artifacts.input_root / name,
        work_root=artifacts.work_root / name,
        output_root=artifacts.output_root / name,
        log_root=artifacts.log_root / name,
        source=artifacts.source,
    )


__all__ = [
    "DirectoryRunArtifacts",
    "FlowRunArtifacts",
    "RunArtifacts",
    "managed_run_artifact_environment",
    "managed_run_artifacts_from_environment",
    "scoped_run_artifacts",
]
