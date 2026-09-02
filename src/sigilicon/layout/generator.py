"""Generic loader for project-owned layout generator entry points."""

from __future__ import annotations

import importlib
import importlib.util
from dataclasses import dataclass
from pathlib import Path
import sys
from types import ModuleType
from typing import Mapping
import uuid

from sigilicon.domain.netlist import NetlistSnapshot
from sigilicon.layout.ir import LayoutPlan
from sigilicon.project_modules import project_import_path


@dataclass(frozen=True)
class LayoutGeneratorInput:
    """Sealed design facts exposed to owner-authored layout code."""

    library: str
    cell: str
    view: str
    generator: str
    stage: str
    source_snapshot: NetlistSnapshot
    source_snapshots: tuple[NetlistSnapshot, ...]
    ports: tuple[str, ...]
    directions: Mapping[str, str]
    primitive_masters: tuple[str, ...]
    technology_library: str
    dbu_per_micron: int


def _purge_project_modules(
    project_root: Path,
    modules: tuple[tuple[str, Path], ...],
    dependency_sources: tuple[Path, ...],
) -> None:
    """Discard caller modules so one process cannot execute stale recipes."""

    root = project_root.resolve()
    declared_names = tuple(
        name for name, source in modules if source.resolve().is_relative_to(root)
    )
    owned_names = {
        ".".join(name.split(".")[:index])
        for name in declared_names
        for index in range(1, len(name.split(".")) + 1)
    }
    owned_sources = {
        source.resolve()
        for source in dependency_sources
        if source.resolve().is_relative_to(root)
    }
    for loaded_name, loaded_module in tuple(sys.modules.items()):
        module_file = getattr(loaded_module, "__file__", None)
        loaded_from_dependency = False
        if module_file is not None:
            try:
                loaded_from_dependency = Path(module_file).resolve() in owned_sources
            except (OSError, RuntimeError):
                pass
        if loaded_name in owned_names or any(
            loaded_name.startswith(f"{name}.") for name in declared_names
        ) or loaded_from_dependency:
            sys.modules.pop(loaded_name, None)
    importlib.invalidate_caches()


def _discard_bytecode(sources: tuple[Path, ...], *, project_root: Path) -> None:
    """Force declared design sources to reload even after same-tick edits."""

    root = project_root.resolve()
    for source in sources:
        if not source.resolve().is_relative_to(root):
            continue
        try:
            Path(importlib.util.cache_from_source(str(source))).unlink(missing_ok=True)
        except (NotImplementedError, OSError):
            pass


def _load_generator_module(
    source: Path,
    *,
    project_root: Path,
    dependency_sources: tuple[Path, ...],
    project_modules: tuple[tuple[str, Path], ...],
) -> ModuleType:
    sources = (source, *dependency_sources)
    _discard_bytecode(sources, project_root=project_root)
    _purge_project_modules(project_root, project_modules, dependency_sources)
    module_name = f"_sigilicon_layout_generator_{uuid.uuid4().hex}"
    module_spec = importlib.util.spec_from_file_location(module_name, source)
    if module_spec is None or module_spec.loader is None:
        raise ValueError(f"cannot load layout generator source: {source}")
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_name] = module
    try:
        module_spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def build_layout_plan_from_sources(
    spec: LayoutGeneratorInput,
    *,
    project_root: Path,
    source_project_root: Path,
    generator_source: Path,
    dependency_sources: tuple[Path, ...],
    project_modules: tuple[tuple[str, Path], ...],
) -> LayoutPlan:
    """Execute one generator from an explicitly materialized source closure."""

    module_sources = tuple(source for _name, source in project_modules)
    owned_module_names = tuple(
        name
        for name, source in project_modules
        if source.resolve().is_relative_to(project_root.resolve())
    )
    with project_import_path(
        project_root,
        module_names=owned_module_names,
        excluded_roots=(source_project_root,),
        working_directory=project_root,
    ):
        module = _load_generator_module(
            generator_source,
            project_root=project_root,
            dependency_sources=(*dependency_sources, *module_sources),
            project_modules=project_modules,
        )
        try:
            entrypoint = getattr(module, "build_layout_plan", None)
            if not callable(entrypoint):
                raise ValueError(
                    f"layout generator {generator_source} must export build_layout_plan"
                )
            plan = entrypoint(spec)
        finally:
            sys.modules.pop(module.__name__, None)
    if not isinstance(plan, LayoutPlan):
        raise TypeError(
            f"layout generator {generator_source} returned {type(plan).__name__}, "
            "expected LayoutPlan"
        )
    identity = (plan.library, plan.cell, plan.view, plan.generator, plan.stage)
    expected = (spec.library, spec.cell, spec.view, spec.generator, spec.stage)
    if identity != expected:
        raise ValueError(
            f"layout generator changed spec identity: got={identity}, expected={expected}"
        )
    return plan
