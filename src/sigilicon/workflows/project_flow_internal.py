"""Internal transfer seam for workflows that compile above one Flow round."""

from __future__ import annotations

from dataclasses import dataclass, field

from sigilicon.flow import FlowEngine, FlowPlan
from sigilicon.workflows.project_flow import ProjectFlowPlan


@dataclass(frozen=True)
class ProjectFlowExecutionBinding:
    """Exact Engine/Plan pair consumed only by higher-order workflows."""

    engine: FlowEngine = field(repr=False, compare=False)
    plan: FlowPlan


def bind_project_flow_execution(
    planned: ProjectFlowPlan,
) -> ProjectFlowExecutionBinding:
    """Transfer a checked project plan without exposing it to project authors."""

    return ProjectFlowExecutionBinding(planned._engine, planned._plan)


__all__ = ["ProjectFlowExecutionBinding", "bind_project_flow_execution"]
