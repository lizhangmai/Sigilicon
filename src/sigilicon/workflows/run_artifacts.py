"""Tool workspace helper owned by one managed execution Step."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from sigilicon.artifacts import (
    copy_immutable_file,
    ensure_nofollow_directory,
    read_nofollow_text,
    write_immutable_text,
)
from sigilicon.execution import StepContext
from sigilicon.paths import validate_artifact_component


@dataclass(frozen=True)
class RunArtifacts:
    """Artifact directories owned by one execution Step."""

    run_id: str
    root: Path
    input_root: Path
    work_root: Path
    output_root: Path
    log_root: Path
    source: Mapping[str, Any]

    @classmethod
    def from_step_context(
        cls,
        context: StepContext,
        output_role: str,
        source: Mapping[str, Any],
    ) -> RunArtifacts:
        role = validate_artifact_component(output_role, "output role")
        run_root = context.output_root.parents[1]
        return cls(
            run_id=context.run_id,
            root=run_root,
            input_root=context.work_root / "inputs",
            work_root=context.work_root / "tool",
            output_root=context.output_root / role,
            log_root=context.work_root / "logs",
            source=source,
        )

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
    context: StepContext,
    output_role: str,
    source: Mapping[str, Any],
) -> dict[str, str]:
    """Write the exact child-process boundary for one current Step."""

    role = validate_artifact_component(output_role, "output role")
    run_root = context.output_root.parents[1]
    path = context.work_root / "managed-run-artifacts.json"
    write_immutable_text(
        path,
        json.dumps(
            {
                "schema": 1,
                "run_id": context.run_id,
                "run_root": str(run_root),
                "input_root": str(context.work_root / "inputs"),
                "work_root": str(context.work_root / "tool"),
                "output_root": str(context.output_root / role),
                "log_root": str(context.work_root / "logs"),
                "source": dict(source),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    return {_MANAGED_ARTIFACT_CONTEXT: str(path)}


def managed_run_artifacts_from_environment() -> RunArtifacts | None:
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
    return RunArtifacts(
        run_id=run_id,
        root=run_root,
        input_root=roots["input_root"],
        work_root=roots["work_root"],
        output_root=roots["output_root"],
        log_root=roots["log_root"],
        source=source,
    )


def scoped_run_artifacts(
    artifacts: RunArtifacts,
    component: str,
) -> RunArtifacts:
    """Give one measurement collision-free directories in the same run."""

    name = validate_artifact_component(component, "artifact scope")
    return RunArtifacts(
        run_id=artifacts.run_id,
        root=artifacts.root,
        input_root=artifacts.input_root / name,
        work_root=artifacts.work_root / name,
        output_root=artifacts.output_root / name,
        log_root=artifacts.log_root / name,
        source=artifacts.source,
    )


__all__ = [
    "RunArtifacts",
    "managed_run_artifact_environment",
    "managed_run_artifacts_from_environment",
    "scoped_run_artifacts",
]
