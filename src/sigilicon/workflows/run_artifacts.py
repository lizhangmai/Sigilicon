"""Artifact workspace seam shared by standalone and Flow-managed execution."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from sigilicon.artifacts import (
    ArtifactRecord,
    copy_immutable_file,
    ensure_nofollow_directory,
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
        *,
        label: str | None = None,
    ) -> Path: ...

    def write_json(
        self,
        role: str,
        components: Sequence[str],
        value: Mapping[str, Any],
        *,
        label: str | None = None,
    ) -> Path: ...

    def copy_file(
        self,
        role: str,
        components: Sequence[str],
        source: Path,
        *,
        label: str | None = None,
    ) -> Path: ...

    def add_file(
        self,
        role: str,
        path: Path,
        *,
        label: str | None = None,
    ) -> object: ...


@dataclass(frozen=True)
class StandaloneRunArtifacts:
    """RunArtifacts Adapter backed by one standalone ArtifactRecord."""

    record: ArtifactRecord

    @property
    def run_id(self) -> str:
        return self.record.paths.identity

    @property
    def root(self) -> Path:
        return self.record.paths.root

    @property
    def source(self) -> Mapping[str, Any]:
        value = self.record.manifest.get("source")
        return value if isinstance(value, Mapping) else {}

    def path(self, role: str, *components: str) -> Path:
        return self.record.path(role, *components)

    def directory(self, role: str, *components: str) -> Path:
        return self.record.directory(role, *components)

    def write_text(
        self,
        role: str,
        components: Sequence[str],
        value: str,
        *,
        label: str | None = None,
    ) -> Path:
        return self.record.write_text(role, components, value, label=label)

    def write_json(
        self,
        role: str,
        components: Sequence[str],
        value: Mapping[str, Any],
        *,
        label: str | None = None,
    ) -> Path:
        return self.record.write_json(role, components, value, label=label)

    def copy_file(
        self,
        role: str,
        components: Sequence[str],
        source: Path,
        *,
        label: str | None = None,
    ) -> Path:
        return self.record.copy_file(role, components, source, label=label)

    def add_file(
        self,
        role: str,
        path: Path,
        *,
        label: str | None = None,
    ) -> object:
        return self.record.add_file(role, path, label=label)


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
        *,
        label: str | None = None,
    ) -> Path:
        del label
        destination = self.path(role, *components)
        write_immutable_text(destination, value)
        return destination

    def write_json(
        self,
        role: str,
        components: Sequence[str],
        value: Mapping[str, Any],
        *,
        label: str | None = None,
    ) -> Path:
        return self.write_text(
            role,
            components,
            json.dumps(value, indent=2, sort_keys=True) + "\n",
            label=label,
        )

    def copy_file(
        self,
        role: str,
        components: Sequence[str],
        source: Path,
        *,
        label: str | None = None,
    ) -> Path:
        del label
        destination = self.path(role, *components)
        return copy_immutable_file(source, destination)

    def add_file(
        self,
        role: str,
        path: Path,
        *,
        label: str | None = None,
    ) -> object:
        del label
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


__all__ = [
    "FlowRunArtifacts",
    "RunArtifacts",
    "StandaloneRunArtifacts",
]
