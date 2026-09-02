"""Bounded import access to modules owned by an explicitly selected project."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import importlib
import os
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
    excluded_roots: tuple[Path, ...] = (),
    working_directory: Path | None = None,
) -> Iterator[None]:
    """Isolate source-bound imports to one project and restore global state."""

    with _PROJECT_IMPORT_LOCK:
        project = project_root.resolve()
        root = str(project)
        roots = _module_roots(project, module_names)
        previous_path = tuple(sys.path)
        previous_directory = Path.cwd()
        excluded = tuple(path.resolve() for path in excluded_roots)
        runtime_roots = tuple(
            {
                Path(sys.prefix).resolve(),
                Path(sys.base_prefix).resolve(),
            }
        )

        def excluded_path(value: str) -> bool:
            try:
                path = (Path.cwd() if value == "" else Path(value)).resolve()
            except (OSError, RuntimeError):
                return False
            matches = tuple(
                item
                for item in excluded
                if path == item or path.is_relative_to(item)
            )
            if not matches:
                return False
            if any(
                item == runtime or item.is_relative_to(runtime)
                for item in matches
                for runtime in runtime_roots
            ):
                return True
            if any(
                path == item or path.is_relative_to(item)
                for item in runtime_roots
            ):
                return False
            return True

        def excluded_module(module: ModuleType) -> bool:
            values: list[str] = []
            source = getattr(module, "__file__", None)
            if isinstance(source, str):
                values.append(source)
            locations = getattr(module, "__path__", ())
            if locations is not None:
                values.extend(
                    value for value in locations if isinstance(value, str)
                )
            spec = getattr(module, "__spec__", None)
            search = getattr(spec, "submodule_search_locations", None)
            if search is not None:
                values.extend(value for value in search if isinstance(value, str))
            return any(excluded_path(value) for value in values)

        displaced: dict[str, ModuleType] = {
            name: module
            for name, module in tuple(sys.modules.items())
            if _belongs_to_roots(name, roots) or excluded_module(module)
        }
        try:
            for name in displaced:
                sys.modules.pop(name, None)
            sys.path[:] = [
                root,
                *(
                    entry
                    for entry in previous_path
                    if entry != root and not excluded_path(entry)
                ),
            ]
            if working_directory is not None:
                os.chdir(working_directory)
            importlib.invalidate_caches()
            yield
        finally:
            for name in tuple(sys.modules):
                if _belongs_to_roots(name, roots) or excluded_module(
                    sys.modules[name]
                ):
                    sys.modules.pop(name, None)
            sys.modules.update(displaced)
            sys.path[:] = previous_path
            if working_directory is not None:
                os.chdir(previous_directory)
            importlib.invalidate_caches()


__all__ = ["project_import_path"]
