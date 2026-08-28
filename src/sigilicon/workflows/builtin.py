"""Cross-domain dependency assembly for the public operator CLI."""

from __future__ import annotations

from pathlib import Path

from sigilicon.flow.builtin import builtin_registry
from sigilicon.flow.physical_design import (
    OA_XSTREAM_MATERIALIZATION_ADAPTER,
    REFERENCE_MATERIALIZATION_ADAPTER,
    REFERENCE_PNR_ADAPTER,
)
from sigilicon.flow.physical_verification import (
    CALIBRE_PHYSICAL_VERIFICATION_ADAPTER,
    RECEIPT_BOUND_VERIFICATION_SOURCE_ADAPTER,
)
from sigilicon.flow.registry import FlowRegistry
from sigilicon.workflows.physical_design import (
    MaterializationPlanAdapter,
    ReferencePhysicalDesignAdapter,
)
from sigilicon.workflows.layout_verification import (
    CalibrePhysicalVerificationAdapter,
)
from sigilicon.workflows.calibre_pex import (
    CALIBRE_XRC_PEX_ADAPTER,
    CalibreXrcPexAdapter,
)
from sigilicon.flow.post_layout import PEX_ACTION
from sigilicon.workflows.physical_verification import (
    ReceiptBoundVerificationSourceAdapter,
)
from sigilicon.workflows.oa_materialization import OaXStreamMaterializationAdapter
from sigilicon.flow.circuit_design import PHYSICAL_DESIGN_OBSERVATION_ADAPTER
from sigilicon.workflows.design_physical import PhysicalDesignObservationAdapter


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
    registry.register_adapter(
        RECEIPT_BOUND_VERIFICATION_SOURCE_ADAPTER,
        ReceiptBoundVerificationSourceAdapter(),
    )
    registry.register_adapter(
        PHYSICAL_DESIGN_OBSERVATION_ADAPTER,
        PhysicalDesignObservationAdapter(),
    )
    registry.register_action_adapter(
        PEX_ACTION,
        CALIBRE_XRC_PEX_ADAPTER,
        CalibreXrcPexAdapter(),
    )
    if owner_root is not None:
        registry.register_adapter(
            OA_XSTREAM_MATERIALIZATION_ADAPTER,
            OaXStreamMaterializationAdapter(owner_root),
        )
    return registry


__all__ = ["builtin_workflow_registry"]
