"""Filesystem view owned by one managed execution Step."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from sigilicon.artifacts import (
    copy_immutable_file,
    ensure_nofollow_directory,
    write_immutable_bytes,
    write_immutable_text,
)
from sigilicon.paths import validate_artifact_component

@dataclass(frozen=True)
class StepWorkspace:
    """Single filesystem interface for one managed workflow invocation."""

    run_id: str
    root: Path
    input_root: Path
    work_root: Path
    output_root: Path
    log_root: Path
    source: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id:
            raise ValueError("step workspace run id must be non-empty text")
        for name in ("root", "input_root", "work_root", "output_root", "log_root"):
            object.__setattr__(self, name, Path(getattr(self, name)).absolute())
        if self.root == Path(self.root.anchor):
            raise ValueError("step workspace root cannot be a filesystem root")
        if any(
            not path.is_relative_to(self.root)
            for path in (self.input_root, self.output_root, self.log_root)
        ):
            raise ValueError("step workspace roles must remain inside the run root")
        if not isinstance(self.source, Mapping):
            raise ValueError("step workspace source metadata must be a mapping")
        object.__setattr__(self, "source", MappingProxyType(dict(self.source)))

    def scoped(self, component: str) -> "StepWorkspace":
        name = validate_artifact_component(component, "step file scope")
        return StepWorkspace(
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

    def write_bytes(
        self,
        role: str,
        components: Sequence[str],
        value: bytes,
    ) -> Path:
        destination = self.path(role, *components)
        write_immutable_bytes(destination, value)
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
