"""Explicit opt-in registry for experimental implementations."""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from typing import Any

from sigilicon.experimental.reference_pnr.flow import (
    REFERENCE_PNR_ADAPTER,
    REFERENCE_PHYSICAL_DESIGN_ACTION,
    register_reference_physical_design_action,
)
from sigilicon.experimental.workflows.oa_materialization import (
    EXPERIMENTAL_OA_XSTREAM_MATERIALIZATION_ADAPTER,
)
from sigilicon.flow.layout import LAYOUT_VERIFICATION_ACTION
from sigilicon.flow.physical_design import PHYSICAL_MATERIALIZATION_EXECUTION_ACTION
from sigilicon.flow.registry import FlowRegistry
from sigilicon.flow.registry import ToolAdapter
from sigilicon.workflows.builtin import build_flow_registry


EXPERIMENTAL_LAYOUT_VERIFICATION_ADAPTER = "experimental-layout-verification"


def _lazy_adapter_factory(module_name: str, class_name: str) -> Callable[[], ToolAdapter]:
    """Create an experimental provider without importing its backend eagerly."""

    def create() -> ToolAdapter:
        adapter_type = getattr(import_module(module_name), class_name)
        return adapter_type()

    return create


def _experimental_oa_xstream_adapter() -> ToolAdapter:
    adapter_type = getattr(
        import_module("sigilicon.experimental.workflows.oa_materialization"),
        "ExperimentalOaXStreamMaterializationAdapter",
    )
    return adapter_type()


def _experimental_layout_verification_adapter(
    client_factory: Callable[[], Any] | None = None,
) -> ToolAdapter:
    from sigilicon.virtuoso.client import get_client
    from sigilicon.experimental.workflows.layout_flow import (
        ExperimentalLayoutActionAdapter,
    )

    return ExperimentalLayoutActionAdapter(client_factory=client_factory or get_client)


def register_experimental_physical_backends(
    registry: FlowRegistry,
    *,
    materialization_adapter: ToolAdapter | None = None,
    client_factory: Callable[[], Any] | None = None,
) -> None:
    """Extend an already assembled registry with opt-in OA/layout pilots.

    The caller owns the registry and its stable Actions.  This helper only
    adds the explicitly requested experimental Adapter extensions in place.
    """

    if materialization_adapter is None:
        registry.register_action_adapter_factory(
            PHYSICAL_MATERIALIZATION_EXECUTION_ACTION,
            EXPERIMENTAL_OA_XSTREAM_MATERIALIZATION_ADAPTER,
            _experimental_oa_xstream_adapter,
        )
    else:
        registry.register_action_adapter(
            PHYSICAL_MATERIALIZATION_EXECUTION_ACTION,
            EXPERIMENTAL_OA_XSTREAM_MATERIALIZATION_ADAPTER,
            materialization_adapter,
        )
    registry.register_action_adapter_factory(
        LAYOUT_VERIFICATION_ACTION,
        EXPERIMENTAL_LAYOUT_VERIFICATION_ADAPTER,
        lambda: _experimental_layout_verification_adapter(client_factory),
    )
    register_reference_physical_design_action(registry)
    registry.register_adapter_factory(
        REFERENCE_PNR_ADAPTER,
        _lazy_adapter_factory(
            "sigilicon.experimental.workflows.reference_physical_design",
            "ReferencePhysicalDesignAdapter",
        ),
    )


def build_experimental_flow_registry(
    *,
    materialization_adapter: ToolAdapter | None = None,
    client_factory: Callable[[], Any] | None = None,
) -> FlowRegistry:
    """Build the stable registry and explicitly add experimental support."""

    registry = build_flow_registry()
    register_experimental_physical_backends(
        registry,
        materialization_adapter=materialization_adapter,
        client_factory=client_factory,
    )
    return registry


__all__ = [
    "EXPERIMENTAL_LAYOUT_VERIFICATION_ADAPTER",
    "EXPERIMENTAL_OA_XSTREAM_MATERIALIZATION_ADAPTER",
    "build_experimental_flow_registry",
    "register_experimental_physical_backends",
]
