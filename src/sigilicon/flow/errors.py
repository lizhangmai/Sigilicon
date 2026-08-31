"""Low-level errors shared by Flow model and typed evidence values."""

from __future__ import annotations


class FlowContractError(ValueError):
    """A source Flow or one of its typed interfaces is invalid."""


class FlowExecutionError(RuntimeError):
    """A planned Action could not produce a valid result."""


class FactContractError(FlowContractError):
    """A Fact schema or policy expectation is invalid."""


class FactValueError(FlowExecutionError):
    """A collected or restored fact violates its declared schema."""


__all__ = [
    "FactContractError",
    "FactValueError",
    "FlowContractError",
    "FlowExecutionError",
]
