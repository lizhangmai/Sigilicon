"""Opt-in OA physical implementations available for explicit recipe selection."""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from typing import Any

from sigilicon.flow.layout import LAYOUT_VERIFICATION_ACTION
from sigilicon.flow.physical_design import PHYSICAL_MATERIALIZATION_EXECUTION_ACTION
from sigilicon.flow.registry import FlowRegistry
from sigilicon.flow.registry import ToolAdapter


EXPERIMENTAL_LAYOUT_VERIFICATION_ADAPTER = "experimental-layout-verification"
EXPERIMENTAL_OA_XSTREAM_MATERIALIZATION_ADAPTER = (
    "experimental-oa-virtuoso-xstream-materialization"
)


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


def install_experimental_physical_actions(
    registry: FlowRegistry,
    *,
    materialization_adapter: ToolAdapter | None = None,
    client_factory: Callable[[], Any] | None = None,
) -> None:
    """Install opt-in physical implementations in the common action registry.

    The caller owns the registry and its stable Actions. Registration only
    makes named providers available; an owner recipe must select one explicitly.
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


__all__ = [
    "EXPERIMENTAL_LAYOUT_VERIFICATION_ADAPTER",
    "EXPERIMENTAL_OA_XSTREAM_MATERIALIZATION_ADAPTER",
    "install_experimental_physical_actions",
]
