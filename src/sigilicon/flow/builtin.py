"""Built-in dependency assembly for public Flow planning and local execution."""

from __future__ import annotations

from pathlib import Path

from sigilicon.flow.fake import fake_registry
from sigilicon.flow.physical_design import register_physical_design_actions
from sigilicon.flow.physical_verification import (
    register_physical_verification_actions,
)
from sigilicon.flow.registry import FlowRegistry
from sigilicon.flow.source_assets import SourceAssetsAdapter
from sigilicon.flow.standard_asic import register_standard_asic_actions
from sigilicon.flow.synopsys import (
    SynopsysDCAdapter,
    SynopsysFCAdapter,
    SynopsysHSpiceAdapter,
    SynopsysVCSAdapter,
)


def builtin_registry(owner_root: Path | None = None) -> FlowRegistry:
    """Assemble implemented package Adapters without inventing site capability."""

    registry = fake_registry()
    register_standard_asic_actions(registry)
    register_physical_design_actions(registry)
    register_physical_verification_actions(registry)
    registry.register_adapter("source-assets", SourceAssetsAdapter())
    if owner_root is not None:
        registry.register_adapter("synopsys-dc", SynopsysDCAdapter(owner_root))
        registry.register_adapter("synopsys-fc", SynopsysFCAdapter(owner_root))
        registry.register_adapter(
            "synopsys-hspice",
            SynopsysHSpiceAdapter(owner_root),
        )
        registry.register_adapter("synopsys-vcs", SynopsysVCSAdapter(owner_root))
    return registry


__all__ = ["builtin_registry"]
