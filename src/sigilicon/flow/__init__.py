"""Public typed Flow interfaces.

The external seam is :class:`FlowEngine`; model values and explicit registry
dependencies are re-exported so callers and tests cross the same interface.
"""

from sigilicon.flow.contracts import (
    load_catalog_selection,
    load_execution_profile,
    load_flow_catalog,
    load_flow_contract,
)
from sigilicon.flow.builtin import builtin_registry
from sigilicon.flow.engine import FlowEngine
from sigilicon.flow.environment import load_execution_environment
from sigilicon.flow.fake import fake_profile, fake_registry
from sigilicon.flow.model import (
    ActionArtifact,
    ActionContext,
    ActionContract,
    AdapterExecution,
    AdapterSelection,
    ArtifactBinding,
    ArtifactPort,
    CollectedActionResult,
    ExecutionEnvironment,
    ExecutionProfile,
    FlowCatalog,
    FlowCatalogEntry,
    FlowContractError,
    FlowExecutionError,
    FlowNode,
    FlowPlan,
    FlowResult,
    FlowSpec,
    FlowTarget,
    InputArtifact,
    NodeOutcome,
    PolicyCheck,
    PolicySpec,
    PlatformAssetRequirement,
    PreflightCheck,
    PreflightResult,
    ProducedArtifact,
    ResolvedPlatformAsset,
    ResolvedCapability,
    ResolvedPlatformAssetMember,
    RunArtifactReference,
    GitSource,
    SourceArtifact,
    SourceAssets,
    SourceMember,
)
from sigilicon.flow.registry import FlowRegistry, ToolAdapter
from sigilicon.flow.standard_asic import register_standard_asic_actions
from sigilicon.flow.synopsys import (
    SynopsysDCAdapter,
    SynopsysFCAdapter,
    SynopsysHSpiceAdapter,
    SynopsysVCSAdapter,
)
from sigilicon.flow.source_assets import (
    SourceAssetsAdapter,
    load_source_assets,
    source_assets_payload,
)
from sigilicon.flow.run_artifacts import (
    load_run_artifact_reference,
    run_artifact_reference_payload,
    validate_durable_artifact,
)


__all__ = [
    "ActionArtifact",
    "ActionContext",
    "ActionContract",
    "AdapterExecution",
    "AdapterSelection",
    "ArtifactBinding",
    "ArtifactPort",
    "CollectedActionResult",
    "ExecutionEnvironment",
    "ExecutionProfile",
    "FlowCatalog",
    "FlowCatalogEntry",
    "FlowContractError",
    "FlowEngine",
    "FlowExecutionError",
    "FlowNode",
    "FlowPlan",
    "FlowRegistry",
    "FlowResult",
    "FlowSpec",
    "FlowTarget",
    "InputArtifact",
    "NodeOutcome",
    "PolicyCheck",
    "PolicySpec",
    "PlatformAssetRequirement",
    "PreflightCheck",
    "PreflightResult",
    "ProducedArtifact",
    "ResolvedPlatformAsset",
    "ResolvedCapability",
    "ResolvedPlatformAssetMember",
    "RunArtifactReference",
    "GitSource",
    "SourceArtifact",
    "SourceAssets",
    "SourceMember",
    "SourceAssetsAdapter",
    "ToolAdapter",
    "load_flow_contract",
    "load_execution_profile",
    "load_flow_catalog",
    "load_catalog_selection",
    "load_execution_environment",
    "fake_registry",
    "fake_profile",
    "register_standard_asic_actions",
    "SynopsysDCAdapter",
    "SynopsysFCAdapter",
    "SynopsysHSpiceAdapter",
    "SynopsysVCSAdapter",
    "builtin_registry",
    "load_source_assets",
    "load_run_artifact_reference",
    "run_artifact_reference_payload",
    "validate_durable_artifact",
    "source_assets_payload",
]
