"""Cross-domain dependency assembly for the public operator CLI."""

from __future__ import annotations

from pathlib import Path

from sigilicon.flow.builtin import builtin_registry
from sigilicon.flow.physical_design import (
    REFERENCE_MATERIALIZATION_ADAPTER,
    REFERENCE_PNR_ADAPTER,
)
from sigilicon.flow.physical_verification import (
    CALIBRE_PHYSICAL_VERIFICATION_ADAPTER,
)
from sigilicon.flow.registry import FlowRegistry
from sigilicon.workflows.physical_design import (
    MaterializationPlanAdapter,
    ReferencePhysicalDesignAdapter,
)
from sigilicon.workflows.layout_verification import (
    CalibrePhysicalVerificationAdapter,
)


def builtin_workflow_registry(owner_root: Path | None = None) -> FlowRegistry:
    registry = builtin_registry(owner_root)
    registry.register_adapter(
        REFERENCE_PNR_ADAPTER,
        ReferencePhysicalDesignAdapter(),
    )
    registry.register_adapter(
        REFERENCE_MATERIALIZATION_ADAPTER,
        MaterializationPlanAdapter(),
    )
    registry.register_adapter(
        CALIBRE_PHYSICAL_VERIFICATION_ADAPTER,
        CalibrePhysicalVerificationAdapter(),
    )
    return registry


__all__ = ["builtin_workflow_registry"]
