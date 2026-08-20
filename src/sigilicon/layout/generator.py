"""Generic loader for project-owned layout generator entry points."""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import sys
from types import ModuleType

from sigilicon.layout.ir import LayoutPlan
from sigilicon.layout.spec import LayoutSpec


def _load_generator_module(source: Path) -> ModuleType:
    payload = source.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    module_name = f"_flow_layout_generator_{digest}"
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
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

    module = _load_generator_module(spec.generator_source)
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
