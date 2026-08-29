"""Explicit Action, Adapter and Policy registration for one FlowEngine."""

from __future__ import annotations

from dataclasses import replace
from typing import Protocol

from sigilicon.flow.model import (
    ActionContext,
    ActionContract,
    AdapterExecution,
    CollectedActionResult,
    FlowContractError,
    SourceMember,
    identifier,
)


class ToolAdapter(Protocol):
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
        self._implementation_sources: dict[str, SourceMember] = {}

    def bind_implementation_source(self, source: SourceMember) -> None:
        """Bind exact project-owned Adapter code to plans built by this registry."""

        if source.path in self._implementation_sources:
            raise FlowContractError(
                f"Flow implementation source {source.path!r} is already bound"
            )
        self._implementation_sources[source.path] = source

    @property
    def implementation_sources(self) -> tuple[SourceMember, ...]:
        """Return exact owner implementation records in stable path order."""

        return tuple(
            self._implementation_sources[path]
            for path in sorted(self._implementation_sources)
        )

    def register_action(self, contract: ActionContract) -> None:
        if contract.kind in self._actions:
            raise FlowContractError(f"Action {contract.kind!r} is already registered")
        self._actions[contract.kind] = contract

    def register_adapter(self, name: str, adapter: ToolAdapter) -> None:
        identity = identifier(name, "Adapter name")
        if identity in self._adapters:
            raise FlowContractError(f"Adapter {identity!r} is already registered")
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

    def register_action_adapter(
        self,
        action_kind: str,
        name: str,
        adapter: ToolAdapter,
    ) -> None:
        """Register one owner implementation of an extensible Action seam."""

        contract = self.action(action_kind)
        if not contract.adapter_extensible:
            raise FlowContractError(
                f"Action {action_kind!r} does not accept Adapter extensions"
            )
        identity = identifier(name, "Adapter name")
        if identity in contract.adapters:
            raise FlowContractError(
                f"Adapter {identity!r} is already allowed by Action {action_kind!r}"
            )
        self.register_adapter(identity, adapter)
        self._actions[action_kind] = replace(
            contract,
            adapters=(*contract.adapters, identity),
        )

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
