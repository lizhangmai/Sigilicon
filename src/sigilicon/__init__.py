"""Reusable EDA flow models, safety adapters, and orchestration."""

from typing import Any

from sigilicon.paths import ProjectContext


__all__ = ["Project", "ProjectContext", "ProjectFlow", "ProjectOaWorkflow"]


def __getattr__(name: str) -> Any:
    if name in {"Project", "ProjectFlow", "ProjectOaWorkflow"}:
        from sigilicon import project

        return getattr(project, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
