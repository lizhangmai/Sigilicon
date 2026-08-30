"""Cross-domain dependency assembly for every public Flow client."""

from __future__ import annotations

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


def _synopsys_adapter(name: str) -> ToolAdapter:
    from sigilicon.workflows.synopsys import (
        SynopsysDCAdapter,
        SynopsysFCAdapter,
        SynopsysHSpiceAdapter,
        SynopsysStructuralLinkAdapter,
        SynopsysVCSAdapter,
    )

    factories = {
        "dc": SynopsysDCAdapter,
        "fc": SynopsysFCAdapter,
        "hspice": SynopsysHSpiceAdapter,
        "structural-link": SynopsysStructuralLinkAdapter,
        "vcs": SynopsysVCSAdapter,
    }
    return factories[name]()


def _reference_physical_design_adapter() -> ToolAdapter:
    from sigilicon.workflows.physical_design import ReferencePhysicalDesignAdapter

    return ReferencePhysicalDesignAdapter()


def _materialization_plan_adapter() -> ToolAdapter:
    from sigilicon.workflows.physical_design import MaterializationPlanAdapter

    return MaterializationPlanAdapter()


def _calibre_physical_verification_adapter() -> ToolAdapter:
    from sigilicon.workflows.layout_verification import (
        CalibrePhysicalVerificationAdapter,
    )

    return CalibrePhysicalVerificationAdapter()


def _receipt_bound_verification_source_adapter() -> ToolAdapter:
    from sigilicon.workflows.physical_verification import (
        ReceiptBoundVerificationSourceAdapter,
    )

    return ReceiptBoundVerificationSourceAdapter()


def _physical_design_observation_adapter() -> ToolAdapter:
    from sigilicon.workflows.design_physical import PhysicalDesignObservationAdapter

    return PhysicalDesignObservationAdapter()


def _calibre_xrc_pex_adapter() -> ToolAdapter:
    from sigilicon.workflows.calibre_pex import CalibreXrcPexAdapter

    return CalibreXrcPexAdapter()


def _oa_xstream_materialization_adapter() -> ToolAdapter:
    from sigilicon.workflows.oa_materialization import OaXStreamMaterializationAdapter

    return OaXStreamMaterializationAdapter()


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
    registry.register_adapter_factory(
        "synopsys-dc", lambda: _synopsys_adapter("dc")
    )
    registry.register_adapter_factory(
        "synopsys-fc", lambda: _synopsys_adapter("fc")
    )
    registry.register_adapter_factory(
        "synopsys-hspice", lambda: _synopsys_adapter("hspice")
    )
    registry.register_adapter_factory(
        "synopsys-structural-link",
        lambda: _synopsys_adapter("structural-link"),
    )
    registry.register_adapter_factory(
        "synopsys-vcs", lambda: _synopsys_adapter("vcs")
    )
    registry.register_adapter_factory(
        REFERENCE_PNR_ADAPTER,
        _reference_physical_design_adapter,
    )
    registry.register_adapter_factory(
        REFERENCE_MATERIALIZATION_ADAPTER,
        _materialization_plan_adapter,
    )
    registry.register_adapter_factory(
        CALIBRE_PHYSICAL_VERIFICATION_ADAPTER,
        _calibre_physical_verification_adapter,
    )
    registry.register_adapter_factory(
        RECEIPT_BOUND_VERIFICATION_SOURCE_ADAPTER,
        _receipt_bound_verification_source_adapter,
    )
    registry.register_adapter_factory(
        PHYSICAL_DESIGN_OBSERVATION_ADAPTER,
        _physical_design_observation_adapter,
    )
    registry.register_action_adapter_factory(
        PEX_ACTION,
        CALIBRE_XRC_PEX_ADAPTER,
        _calibre_xrc_pex_adapter,
    )
    if materialization_adapter is None:
        registry.register_adapter_factory(
            OA_XSTREAM_MATERIALIZATION_ADAPTER,
            _oa_xstream_materialization_adapter,
        )
    else:
        registry.register_adapter(
            OA_XSTREAM_MATERIALIZATION_ADAPTER,
            materialization_adapter,
        )
    return registry


__all__ = ["build_flow_registry"]
