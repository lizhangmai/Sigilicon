"""Typed planning, backend execution, and durable run records."""

from sigilicon.execution.model import (
    Artifact,
    ContractError,
    Evidence,
    ExecutionError,
    ExecutionPlan,
    Operation,
    Step,
    PreflightCheck,
    PreflightResult,
    Resources,
    RunFailure,
    RunResult,
    Source,
    StepContext,
    StepOutcome,
    StepResult,
)
from sigilicon.execution.backend import Backend, BackendRegistry, Preparation
from sigilicon.execution.runs import RunStore, RunStoreError


__all__ = [
    "Artifact",
    "Backend",
    "BackendRegistry",
    "ContractError",
    "Evidence",
    "ExecutionError",
    "ExecutionPlan",
    "Operation",
    "Step",
    "PreflightCheck",
    "PreflightResult",
    "Preparation",
    "Resources",
    "RunFailure",
    "RunResult",
    "RunStore",
    "RunStoreError",
    "Source",
    "StepContext",
    "StepOutcome",
    "StepResult",
]
