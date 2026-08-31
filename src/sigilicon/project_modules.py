"""Bounded import access to modules owned by an explicitly selected project."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import importlib
from pathlib import Path
import sys
from threading import RLock
from types import ModuleType


_PROJECT_IMPORT_LOCK = RLock()


def _module_roots(project_root: Path, module_names: tuple[str, ...]) -> set[str]:
    roots = {
        name.split(".", 1)[0]
        for name in module_names
        if name and name.split(".", 1)[0].isidentifier()
    }
    for child in project_root.iterdir():
        if child.is_symlink():
            continue
        if child.is_file() and child.suffix == ".py" and child.stem.isidentifier():
            roots.add(child.stem)
        elif child.is_dir() and child.name.isidentifier():
            roots.add(child.name)
    return roots


def _belongs_to_roots(name: str, roots: set[str]) -> bool:
    return name.split(".", 1)[0] in roots


@contextmanager
def project_import_path(
    project_root: Path,
    *,
    module_names: tuple[str, ...] = (),
) -> Iterator[None]:
    """Isolate source-bound imports to one project and restore global state."""

    with _PROJECT_IMPORT_LOCK:
        project = project_root.resolve()
        root = str(project)
        roots = _module_roots(project, module_names)
        previous_path = tuple(sys.path)
        displaced: dict[str, ModuleType] = {
            name: module
            for name, module in tuple(sys.modules.items())
            if _belongs_to_roots(name, roots)
        }
        for name in displaced:
            sys.modules.pop(name, None)
        sys.path[:] = [root, *(entry for entry in previous_path if entry != root)]
        importlib.invalidate_caches()
        try:
            yield
        finally:
            for name in tuple(sys.modules):
                if _belongs_to_roots(name, roots):
                    sys.modules.pop(name, None)
            sys.modules.update(displaced)
            sys.path[:] = previous_path
            importlib.invalidate_caches()


__all__ = ["project_import_path"]
