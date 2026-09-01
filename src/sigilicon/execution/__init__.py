"""Typed planning, backend execution, and durable run records."""

from sigilicon.execution.backend import Backend, Backends
from sigilicon.execution.engine import preflight, run
from sigilicon.execution.model import (
    Artifact,
    ContractError,
    Evidence,
    ExecutionError,
    ExecutionPlan,
    PreflightCheck,
    PreflightResult,
    Resources,
    RunFailure,
    RunResult,
    Source,
    Step,
    StepContext,
    StepOutcome,
    StepResult,
)
from sigilicon.execution.runs import RunStore, RunStoreError


__all__ = [
    "Artifact",
    "Backend",
    "Backends",
    "ContractError",
    "Evidence",
    "ExecutionError",
    "ExecutionPlan",
    "PreflightCheck",
    "PreflightResult",
    "Resources",
    "RunFailure",
    "RunResult",
    "RunStore",
    "RunStoreError",
    "Source",
    "Step",
    "StepContext",
    "StepOutcome",
    "StepResult",
    "preflight",
    "run",
]
