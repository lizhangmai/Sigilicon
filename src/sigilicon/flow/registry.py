"""Explicit Action, Adapter and Policy registration for one FlowEngine."""

from __future__ import annotations

from typing import Protocol

from sigilicon.flow.model import (
    ActionContext,
    ActionContract,
    AdapterExecution,
    CollectedActionResult,
    FlowContractError,
    identifier,
)


class ToolAdapter(Protocol):
    version: str

    def validate_inputs(self, context: ActionContext) -> tuple[str, ...]: ...

    def prepare(self, context: ActionContext) -> None: ...

    def execute(self, context: ActionContext) -> AdapterExecution: ...

    def collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult: ...


class FlowRegistry:
    """The explicit dependencies accepted by a FlowEngine instance."""

    def __init__(self) -> None:
        self._actions: dict[str, ActionContract] = {}
        self._adapters: dict[str, ToolAdapter] = {}

    def register_action(self, contract: ActionContract) -> None:
        if contract.kind in self._actions:
            raise FlowContractError(f"Action {contract.kind!r} is already registered")
        self._actions[contract.kind] = contract

    def register_adapter(self, name: str, adapter: ToolAdapter) -> None:
        identity = identifier(name, "Adapter name")
        if identity in self._adapters:
            raise FlowContractError(f"Adapter {identity!r} is already registered")
        version = getattr(adapter, "version", None)
        if not isinstance(version, str) or not version:
            raise FlowContractError(f"Adapter {identity!r} has no version identity")
        for operation in (
            "validate_inputs",
            "prepare",
            "execute",
            "collect_result",
        ):
            if not callable(getattr(adapter, operation, None)):
                raise FlowContractError(
                    f"Adapter {identity!r} has no {operation} operation"
                )
        self._adapters[identity] = adapter

    def action(self, kind: str) -> ActionContract:
        try:
            return self._actions[kind]
        except KeyError as exc:
            raise FlowContractError(f"unknown Action kind: {kind!r}") from exc

    def adapter(self, name: str) -> ToolAdapter:
        try:
            return self._adapters[name]
        except KeyError as exc:
            raise FlowContractError(f"unknown Adapter: {name!r}") from exc

    def has_adapter(self, name: str) -> bool:
        return name in self._adapters
