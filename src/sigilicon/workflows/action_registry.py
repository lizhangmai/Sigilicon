"""Single cross-domain action registry for every public Flow client."""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from typing import Any

from sigilicon.flow.circuit_design import register_circuit_design_actions
from sigilicon.flow.layout import (
    LAYOUT_VERIFICATION_ACTION,
    XSTREAM_CALIBRE_LAYOUT_ADAPTER,
    register_layout_actions,
)
from sigilicon.flow.physical_design import (
    OA_XSTREAM_MATERIALIZATION_ADAPTER,
    PHYSICAL_MATERIALIZATION_EXECUTION_ACTION,
    register_physical_design_actions,
)
from sigilicon.flow.physical_design import (
    MATERIALIZATION_PLAN_ADAPTER,
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
    MATERIALIZATION_PLAN_ADAPTER: (
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
}

_CALIBRE_XRC_PEX = (
    "sigilicon.workflows.calibre_pex",
    "CalibreXrcPexAdapter",
)
_OA_XSTREAM_MATERIALIZATION = (
    "sigilicon.workflows.oa_materialization",
    "OaXStreamMaterializationAdapter",
)
_XSTREAM_CALIBRE_LAYOUT_MODULE = "sigilicon.workflows.custom_layout_verification"


def _adapter_factory(
    module_name: str,
    class_name: str,
) -> Callable[[], ToolAdapter]:
    def create() -> ToolAdapter:
        adapter_type = getattr(import_module(module_name), class_name)
        return adapter_type()

    return create


def _layout_adapter_factory(
    client_factory: Callable[[], Any] | None,
) -> ToolAdapter:
    create_adapter = getattr(
        import_module(_XSTREAM_CALIBRE_LAYOUT_MODULE),
        "create_xstream_calibre_layout_adapter",
    )
    return create_adapter(client_factory=client_factory)


def build_action_registry(
    *,
    materialization_adapter: ToolAdapter | None = None,
    client_factory: Callable[[], Any] | None = None,
) -> FlowRegistry:
    """Assemble every action; an owner recipe selects each implementation."""

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
        registry.register_adapter_factory(
            adapter_name,
            _adapter_factory(*reference),
        )
    registry.register_action_adapter_factory(
        PEX_ACTION,
        CALIBRE_XRC_PEX_ADAPTER,
        _adapter_factory(*_CALIBRE_XRC_PEX),
    )
    if materialization_adapter is None:
        registry.register_action_adapter_factory(
            PHYSICAL_MATERIALIZATION_EXECUTION_ACTION,
            OA_XSTREAM_MATERIALIZATION_ADAPTER,
            _adapter_factory(*_OA_XSTREAM_MATERIALIZATION),
        )
    else:
        registry.register_action_adapter(
            PHYSICAL_MATERIALIZATION_EXECUTION_ACTION,
            OA_XSTREAM_MATERIALIZATION_ADAPTER,
            materialization_adapter,
        )
    registry.register_action_adapter_factory(
        LAYOUT_VERIFICATION_ACTION,
        XSTREAM_CALIBRE_LAYOUT_ADAPTER,
        lambda: _layout_adapter_factory(client_factory),
    )
    return registry


__all__ = ["build_action_registry"]
