"""Filesystem view owned by one managed execution Step."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from sigilicon.artifacts import (
    copy_immutable_file,
    ensure_nofollow_directory,
    write_immutable_text,
)
from sigilicon.paths import validate_artifact_component

if TYPE_CHECKING:
    from sigilicon.execution.model import StepContext


@dataclass(frozen=True)
class StepFiles:
    """Narrow file interface for one Backend Step."""

    run_id: str
    root: Path
    input_root: Path
    work_root: Path
    output_root: Path
    log_root: Path
    source: Mapping[str, Any]

    @classmethod
    def from_context(
        cls,
        context: "StepContext",
        output_role: str,
        source: Mapping[str, Any],
        *,
        tool_work_root: Path | None = None,
    ) -> "StepFiles":
        from sigilicon.execution.model import StepContext

        if not isinstance(context, StepContext):
            raise TypeError("StepFiles requires a StepContext")
        role = validate_artifact_component(output_role, "output role")
        run_root = context.output_root.parents[1]
        return cls(
            run_id=context.run_id,
            root=run_root,
            input_root=context.work_root / "inputs",
            work_root=(
                context.work_root / "tool"
                if tool_work_root is None
                else Path(tool_work_root).absolute()
            ),
            output_root=context.output_root / role,
            log_root=context.work_root / "logs",
            source=source,
        )

    def scoped(self, component: str) -> "StepFiles":
        name = validate_artifact_component(component, "step file scope")
        return StepFiles(
            run_id=self.run_id,
            root=self.root,
            input_root=self.input_root / name,
            work_root=self.work_root / name,
            output_root=self.output_root / name,
            log_root=self.log_root / name,
            source=self.source,
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
            raise ValueError(f"unknown step file role: {role!r}") from exc
        return ensure_nofollow_directory(root)

    def path(self, role: str, *components: str) -> Path:
        root = self._root_for(role)
        current = root
        for component in components:
            current /= validate_artifact_component(component, "step file component")
        if not Path(os.path.abspath(current)).is_relative_to(Path(os.path.abspath(root))):
            raise RuntimeError("step file escaped its managed role")
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

    def add_file(self, role: str, path: Path) -> Path:
        root = Path(os.path.abspath(self._root_for(role)))
        candidate = Path(os.path.abspath(path))
        if not candidate.is_relative_to(root) or not candidate.exists():
            raise RuntimeError("step file is outside its managed role")
        if candidate.is_symlink():
            raise RuntimeError("step file cannot be a symlink")
        if candidate.is_dir():
            ensure_nofollow_directory(candidate)
        else:
            ensure_nofollow_directory(candidate.parent)
        return candidate
