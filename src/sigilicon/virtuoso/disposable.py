"""Small temporary work-directory helper for source-driven OA operations."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
from typing import Any, Mapping, Sequence

from sigilicon.paths import validate_artifact_component


class DisposableWork:
    """Own one exact temporary directory and nothing else.

    This is intentionally not an artifact or transaction object.  Workspace
    safety and OA parity belong to :mod:`sigilicon.virtuoso.workspace`; this class
    only stages files for external tools.  Call :meth:`keep` when a caller
    explicitly wants to retain the temporary result directory after return.
    """

    _roles = frozenset(("inputs", "evidence", "logs", "results", "work"))

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._owned = True
        self.root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def create(cls, *, prefix: str = "sigilicon-oa-") -> "DisposableWork":
        return cls(Path(tempfile.mkdtemp(prefix=prefix)))

    def cleanup(self) -> None:
        """Remove only the exact temporary directory created by this object."""

        if not self._owned:
            return
        self._owned = False
        shutil.rmtree(self.root)

    def keep(self) -> Path:
        """Detach ownership and retain the directory for an explicit caller."""

        self._owned = False
        return self.root

    def __enter__(self) -> "DisposableWork":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.cleanup()

    def __del__(self) -> None:
        if getattr(self, "_owned", False):
            try:
                shutil.rmtree(self.root)
            except (FileNotFoundError, OSError):
                pass

    def role(self, role: str) -> Path:
        checked = validate_artifact_component(role, "temporary work role")
        if checked not in self._roles:
            raise ValueError(f"unknown temporary work role: {role}")
        path = self.root / checked
        path.mkdir(parents=True, exist_ok=True)
        return path

    def path(self, role: str, *components: str) -> Path:
        result = self.role(role)
        for component in components:
            result /= validate_artifact_component(
                component, "temporary work path component"
            )
        return result

    def directory(self, role: str, *components: str) -> Path:
        result = self.path(role, *components)
        result.mkdir(parents=True, exist_ok=True)
        return result

    def write_text(
        self,
        role: str,
        components: Sequence[str],
        value: str,
        *,
        label: str | None = None,
    ) -> Path:
        del label
        path = self.path(role, *components)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        return path

    def copy_file(
        self,
        role: str,
        components: Sequence[str],
        source: Path,
        *,
        label: str | None = None,
    ) -> Path:
        del label
        source = Path(source).resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        path = self.path(role, *components)
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, path)
        return path

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
