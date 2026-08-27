"""Cross-domain dependency assembly for the public operator CLI."""

from __future__ import annotations

from pathlib import Path

from sigilicon.flow.builtin import builtin_registry
from sigilicon.flow.physical_design import REFERENCE_PNR_ADAPTER
from sigilicon.flow.registry import FlowRegistry
from sigilicon.workflows.physical_design import ReferencePhysicalDesignAdapter


def builtin_workflow_registry(owner_root: Path | None = None) -> FlowRegistry:
    registry = builtin_registry(owner_root)
    registry.register_adapter(
        REFERENCE_PNR_ADAPTER,
        ReferencePhysicalDesignAdapter(),
    )
    return registry


__all__ = ["builtin_workflow_registry"]
