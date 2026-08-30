"""Cross-domain dependency assembly for every public Flow client."""

from __future__ import annotations

from sigilicon.flow.circuit_design import register_circuit_design_actions
from sigilicon.flow.physical_design import register_physical_design_actions
from sigilicon.flow.physical_design import (
    OA_XSTREAM_MATERIALIZATION_ADAPTER,
    REFERENCE_MATERIALIZATION_ADAPTER,
    REFERENCE_PNR_ADAPTER,
)
from sigilicon.flow.physical_verification import (
    CALIBRE_PHYSICAL_VERIFICATION_ADAPTER,
    RECEIPT_BOUND_VERIFICATION_SOURCE_ADAPTER,
    register_physical_verification_actions,
)
from sigilicon.flow.post_layout import register_post_layout_actions
from sigilicon.flow.registry import FlowRegistry, ToolAdapter
from sigilicon.flow.source_assets import SourceAssetsAdapter
from sigilicon.flow.standard_asic import register_standard_asic_actions
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
from sigilicon.flow.native import (
    register_native_actions,
)
from sigilicon.workflows.design_physical import PhysicalDesignObservationAdapter
from sigilicon.workflows.synopsys import (
    SynopsysDCAdapter,
    SynopsysFCAdapter,
    SynopsysHSpiceAdapter,
    SynopsysVCSAdapter,
)


def build_flow_registry(
    *,
    materialization_adapter: ToolAdapter | None = None,
) -> FlowRegistry:
    registry = FlowRegistry()
    register_standard_asic_actions(registry)
    register_physical_design_actions(registry)
    register_physical_verification_actions(registry)
    register_post_layout_actions(registry)
    register_circuit_design_actions(registry)
    register_native_actions(registry)
    registry.register_adapter("source-assets", SourceAssetsAdapter())
    registry.register_adapter("synopsys-dc", SynopsysDCAdapter())
    registry.register_adapter("synopsys-fc", SynopsysFCAdapter())
    registry.register_adapter("synopsys-hspice", SynopsysHSpiceAdapter())
    registry.register_adapter("synopsys-vcs", SynopsysVCSAdapter())
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
    registry.register_adapter(
        OA_XSTREAM_MATERIALIZATION_ADAPTER,
        (
            OaXStreamMaterializationAdapter()
            if materialization_adapter is None
            else materialization_adapter
        ),
    )
    return registry


__all__ = ["build_flow_registry"]
