"""Fail-closed confirmation boundary for stateful bridge calls."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar


T = TypeVar("T")


def require_bridge_confirmation(
    operation: Any,
    label: str,
    action: Callable[[], T],
) -> T:
    """Mark state uncertain when a bridge call raises before confirming completion."""

    try:
        return action()
    except BaseException as exc:
        if getattr(operation, "uncertain_reason", None) is None:
            operation.mark_uncertain(
                f"lost completion confirmation during {label}: "
                f"{type(exc).__name__}: {exc}"
            )
        raise
