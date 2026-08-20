"""Generic loader for project-owned layout generator entry points."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import hashlib
import importlib
import importlib.util
from pathlib import Path
import sys
from types import ModuleType

from sigilicon.layout.ir import LayoutPlan
from sigilicon.layout.spec import LayoutSpec


@contextmanager
def _project_import_path(project_root: Path) -> Iterator[None]:
    """Make explicitly selected project modules importable for one load."""

    root = str(project_root.resolve())
    already_present = root in sys.path
    if not already_present:
        sys.path.insert(0, root)
    try:
        yield
    finally:
        if not already_present:
            sys.path.remove(root)


def _purge_project_modules(
    project_root: Path,
    modules: tuple[tuple[str, Path], ...],
    dependency_sources: tuple[Path, ...],
) -> None:
    """Discard caller modules so one process cannot execute stale recipes."""

    root = project_root.resolve()
    owned_names = tuple(
        name for name, source in modules if source.resolve().is_relative_to(root)
    )
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
        if any(
            loaded_name == name or loaded_name.startswith(f"{name}.")
            for name in owned_names
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
    identity = b"\0".join(
        (
            str(source.resolve()).encode(),
            str(project_root.resolve()).encode(),
            *(path.read_bytes() for path in sources),
        )
    )
    digest = hashlib.sha256(identity).hexdigest()
    module_name = f"_sigilicon_layout_generator_{digest}"
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


def build_layout_plan(spec: LayoutSpec) -> LayoutPlan:
    """Load the design-owned generator and enforce the stable IR boundary."""

    with _project_import_path(spec.project_root):
        module = _load_generator_module(
            spec.generator_source,
            project_root=spec.project_root,
            dependency_sources=(
                *getattr(spec, "generator_dependencies", ()),
                *getattr(spec, "generator_module_sources", ()),
            ),
            project_modules=tuple(
                zip(
                    getattr(spec, "generator_modules", ()),
                    getattr(spec, "generator_module_sources", ()),
                    strict=True,
                )
            ),
        )
        entrypoint = getattr(module, "build_layout_plan", None)
        if not callable(entrypoint):
            raise ValueError(
                f"layout generator {spec.generator_source} must export build_layout_plan"
            )
        plan = entrypoint(spec)
    if not isinstance(plan, LayoutPlan):
        raise TypeError(
            f"layout generator {spec.generator_source} returned {type(plan).__name__}, "
            "expected LayoutPlan"
        )
    identity = (plan.library, plan.cell, plan.view, plan.generator, plan.stage)
    expected = (spec.library, spec.cell, spec.view, spec.generator, spec.stage)
    if identity != expected:
        raise ValueError(
            f"layout generator changed spec identity: got={identity}, expected={expected}"
        )
    if plan.source_fingerprint != spec.source_fingerprint:
        raise ValueError("layout generator returned a stale source fingerprint")
    return plan
