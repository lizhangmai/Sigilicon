"""Managed Synopsys adapters, loaded one tool family at a time."""

from __future__ import annotations

from importlib import import_module
from typing import Any


_ADAPTER_MODULES = {
    "SynopsysDCAdapter": ".dc",
    "SynopsysFCAdapter": ".fc",
    "SynopsysHSpiceAdapter": ".hspice",
    "SynopsysStructuralLinkAdapter": ".structural_link",
    "SynopsysVCSAdapter": ".vcs",
}


def __getattr__(name: str) -> Any:
    module_name = _ADAPTER_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted((*globals(), *_ADAPTER_MODULES))


__all__ = tuple(_ADAPTER_MODULES)

