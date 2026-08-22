"""Public command entry points."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from sigilicon.flow.model import ExecutionEnvironment, ExecutionProfile
from sigilicon.flow.registry import FlowRegistry


def flow_main(
    argv: Sequence[str] | None = None,
    *,
    registry_factory: Callable[[Path | None], FlowRegistry],
    environment_factory: Callable[[ExecutionProfile], ExecutionEnvironment] = (
        lambda _profile: ExecutionEnvironment()
    ),
) -> int:
    """Run the typed Flow CLI with an explicit dependency assembly."""

    from sigilicon.cli.flow_core import main

    return main(
        argv,
        registry_factory=registry_factory,
        environment_factory=environment_factory,
    )


__all__ = ["flow_main"]
