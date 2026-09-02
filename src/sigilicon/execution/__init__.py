"""Typed planning, adapter execution, and durable run records."""

from sigilicon.execution.model import (
    Artifact,
    ContractError,
    Evidence,
    ExecutionError,
    ExecutionPlan,
    Step,
    PreflightCheck,
    PreflightResult,
    RunFailure,
    RunResult,
    Source,
    StepContext,
    StepOutcome,
    StepResult,
)
from sigilicon.execution.adapter import Adapter, AdapterRegistry
from sigilicon.execution.runs import RunStore, RunStoreError


__all__ = [
    "Artifact",
    "Adapter",
    "AdapterRegistry",
    "ContractError",
    "Evidence",
    "ExecutionError",
    "ExecutionPlan",
    "Step",
    "PreflightCheck",
    "PreflightResult",
    "RunFailure",
    "RunResult",
    "RunStore",
    "RunStoreError",
    "Source",
    "StepContext",
    "StepOutcome",
    "StepResult",
]
