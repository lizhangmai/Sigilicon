"""Cross-domain dependency assembly for every public Flow client."""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module

from sigilicon.flow.circuit_design import register_circuit_design_actions
from sigilicon.flow.layout import register_layout_actions
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
from sigilicon.flow.post_layout import CALIBRE_XRC_PEX_ADAPTER, PEX_ACTION
from sigilicon.flow.circuit_design import PHYSICAL_DESIGN_OBSERVATION_ADAPTER
from sigilicon.flow.native import (
    register_native_actions,
)


_LAZY_ADAPTERS = {
    "synopsys-dc": ("sigilicon.workflows.synopsys.dc", "SynopsysDCAdapter"),
    "synopsys-fc": ("sigilicon.workflows.synopsys.fc", "SynopsysFCAdapter"),
    "synopsys-hspice": (
        "sigilicon.workflows.synopsys.hspice",
        "SynopsysHSpiceAdapter",
    ),
    "synopsys-structural-link": (
        "sigilicon.workflows.synopsys.structural_link",
        "SynopsysStructuralLinkAdapter",
    ),
    "synopsys-vcs": ("sigilicon.workflows.synopsys.vcs", "SynopsysVCSAdapter"),
    REFERENCE_PNR_ADAPTER: (
        "sigilicon.workflows.physical_design",
        "ReferencePhysicalDesignAdapter",
    ),
    REFERENCE_MATERIALIZATION_ADAPTER: (
        "sigilicon.workflows.physical_design",
        "MaterializationPlanAdapter",
    ),
    CALIBRE_PHYSICAL_VERIFICATION_ADAPTER: (
        "sigilicon.workflows.layout_verification",
        "CalibrePhysicalVerificationAdapter",
    ),
    RECEIPT_BOUND_VERIFICATION_SOURCE_ADAPTER: (
        "sigilicon.workflows.physical_verification",
        "ReceiptBoundVerificationSourceAdapter",
    ),
    PHYSICAL_DESIGN_OBSERVATION_ADAPTER: (
        "sigilicon.workflows.design_physical",
        "PhysicalDesignObservationAdapter",
    ),
    OA_XSTREAM_MATERIALIZATION_ADAPTER: (
        "sigilicon.workflows.oa_materialization",
        "OaXStreamMaterializationAdapter",
    ),
}

_CALIBRE_XRC_PEX = (
    "sigilicon.workflows.calibre_pex",
    "CalibreXrcPexAdapter",
)

def _adapter_factory(
    module_name: str,
    class_name: str,
) -> Callable[[], ToolAdapter]:
    def create() -> ToolAdapter:
        adapter_type = getattr(import_module(module_name), class_name)
        return adapter_type()

    return create


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
    register_layout_actions(registry)
    register_native_actions(registry)
    registry.register_adapter("source-assets", SourceAssetsAdapter())
    for adapter_name, reference in _LAZY_ADAPTERS.items():
        if (
            adapter_name == OA_XSTREAM_MATERIALIZATION_ADAPTER
            and materialization_adapter is not None
        ):
            continue
        registry.register_adapter_factory(
            adapter_name,
            _adapter_factory(*reference),
        )
    registry.register_action_adapter_factory(
        PEX_ACTION,
        CALIBRE_XRC_PEX_ADAPTER,
        _adapter_factory(*_CALIBRE_XRC_PEX),
    )
    if materialization_adapter is not None:
        registry.register_adapter(
            OA_XSTREAM_MATERIALIZATION_ADAPTER,
            materialization_adapter,
        )
    return registry


__all__ = ["build_flow_registry"]
