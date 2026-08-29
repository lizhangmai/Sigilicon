"""Project-owned extensions assembled into the reusable Flow registry."""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import sys
from types import ModuleType

from sigilicon.domain.repository import RepositoryContext
from sigilicon.flow.registry import FlowRegistry
from sigilicon.workflows.builtin import builtin_workflow_registry


def _load_extension(source: Path) -> ModuleType:
    identity = hashlib.sha256(str(source).encode("utf-8")).hexdigest()
    module_name = f"_sigilicon_project_flow_{identity}"
    spec = importlib.util.spec_from_file_location(module_name, source)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load Flow registry extension {source}")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise ValueError(f"cannot load Flow registry extension {source}: {exc}") from exc
    finally:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
    return module


def project_workflow_registry(
    project: RepositoryContext | Path | str,
    owner_root: Path | str | None,
) -> FlowRegistry:
    """Assemble built-ins and one explicitly selected owner extension.

    The project manifest selects the source, while the cataloged component
    proves that source belongs to the selected owner and its ``flow`` fileset.
    """

    if owner_root is None:
        return builtin_workflow_registry()
    repository = (
        project
        if isinstance(project, RepositoryContext)
        else RepositoryContext.from_project_root(project)
    )
    selected_root = Path(owner_root).resolve()
    owner = repository.require_owner(selected_root)
    if owner.root != selected_root:
        raise ValueError(
            f"Flow owner root must equal its cataloged root: {owner.root}"
        )
    registry = builtin_workflow_registry(owner.root)
    source = repository.flow_registry_extension(owner)
    if source is None:
        return registry
    module = _load_extension(source)
    register = getattr(module, "register_flow_adapters", None)
    if not callable(register):
        raise ValueError(
            f"Flow registry extension {source} must define "
            "register_flow_adapters(registry, owner_root)"
        )
    result = register(registry, owner.root)
    if result is not None:
        raise ValueError(
            f"Flow registry extension {source} must mutate the supplied registry "
            "and return None"
        )
    return registry


__all__ = ["project_workflow_registry"]
