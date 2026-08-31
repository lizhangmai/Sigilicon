"""Explicit Action, Adapter and Policy registration for one FlowEngine."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol, runtime_checkable

from sigilicon.flow.model import (
    ActionPlan,
    ActionContext,
    ActionContract,
    AdapterResult,
    FlowContractError,
    FlowNode,
    SourceMember,
    identifier,
)


@runtime_checkable
class ToolAdapter(Protocol):
    """A tool-specific implementation behind one complete invocation seam."""

    def run(self, context: ActionContext) -> AdapterResult: ...


def _tool_adapter(
    implementation: object,
) -> ToolAdapter:
    if isinstance(implementation, ToolAdapter) and callable(
        getattr(implementation, "run", None)
    ):
        return implementation
    raise FlowContractError("Adapter must provide run(context)")


@dataclass(frozen=True)
class AdapterProvider:
    """A side-effect-free description and lazy factory for one Adapter."""

    factory: Callable[[], ToolAdapter]
    accepted_extensions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not callable(self.factory):
            raise FlowContractError("Adapter factory must be callable")
        for name in self.accepted_extensions:
            identifier(name, "Adapter extension")
        if len(self.accepted_extensions) != len(set(self.accepted_extensions)):
            raise FlowContractError("duplicate Adapter extensions")


class FlowRegistry:
    """The explicit dependencies accepted by a FlowEngine instance."""

    def __init__(self) -> None:
        self._actions: dict[str, ActionContract] = {}
        self._adapters: dict[str, ToolAdapter] = {}
        self._adapter_providers: dict[str, AdapterProvider] = {}
        self._action_planners: dict[str, Callable[[FlowNode], ActionPlan]] = {}
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

    def register_action_planner(
        self,
        action_kind: str,
        planner: Callable[[FlowNode], ActionPlan],
    ) -> None:
        """Bind one typed domain planner to its declared Action Plan input."""

        contract = self.action(action_kind)
        if contract.plan_input_kind is None:
            raise FlowContractError(
                f"Action {action_kind!r} does not accept a domain Plan input"
            )
        if action_kind in self._action_planners:
            raise FlowContractError(
                f"Action {action_kind!r} already has a domain planner"
            )
        if not callable(planner):
            raise FlowContractError("Action planner must be callable")
        self._action_planners[action_kind] = planner

    def compile_action_plan(self, node: FlowNode) -> ActionPlan | None:
        """Compile the typed Plan required by one resolved Flow node."""

        if not isinstance(node, FlowNode):
            raise FlowContractError("Action planning requires a FlowNode")
        contract = self.action(node.action_kind)
        planner = self._action_planners.get(node.action_kind)
        if contract.plan_input_kind is None:
            if planner is not None:  # pragma: no cover - registration rejects this
                raise FlowContractError(
                    f"Action {node.action_kind!r} has an unexpected domain planner"
                )
            return None
        if planner is None:
            raise FlowContractError(
                f"Action {node.action_kind!r} has no registered domain planner"
            )
        planned = planner(node)
        if not isinstance(planned, ActionPlan):
            raise FlowContractError(
                f"Action planner for {node.action_kind!r} did not return ActionPlan"
            )
        if planned.kind != contract.plan_input_kind:
            raise FlowContractError(
                f"Action planner for {node.action_kind!r} returned {planned.kind!r}; "
                f"expected {contract.plan_input_kind!r}"
            )
        return planned

    def register_adapter(
        self,
        name: str,
        adapter: ToolAdapter,
    ) -> None:
        identity = identifier(name, "Adapter name")
        if identity in self._adapter_providers:
            raise FlowContractError(f"Adapter {identity!r} is already registered")
        implementation = _tool_adapter(adapter)
        accepted_extensions = getattr(implementation, "accepted_extensions", ())
        self._adapter_providers[identity] = AdapterProvider(
            factory=lambda: implementation,
            accepted_extensions=accepted_extensions,
        )
        self._adapters[identity] = implementation

    def register_adapter_factory(
        self,
        name: str,
        factory: Callable[[], ToolAdapter],
        *,
        accepted_extensions: tuple[str, ...] = (),
    ) -> None:
        """Register a provider without importing or creating its tool Adapter."""

        identity = identifier(name, "Adapter name")
        if identity in self._adapter_providers:
            raise FlowContractError(f"Adapter {identity!r} is already registered")
        self._adapter_providers[identity] = AdapterProvider(
            factory=factory,
            accepted_extensions=accepted_extensions,
        )

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

    def register_action_adapter_factory(
        self,
        action_kind: str,
        name: str,
        factory: Callable[[], ToolAdapter],
        *,
        accepted_extensions: tuple[str, ...] = (),
    ) -> None:
        """Register one lazy implementation of an extensible Action seam."""

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
        self.register_adapter_factory(
            identity,
            factory,
            accepted_extensions=accepted_extensions,
        )
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
            provider = self._adapter_providers[name]
        except KeyError as exc:
            raise FlowContractError(f"unknown Adapter: {name!r}") from exc
        implementation = self._adapters.get(name)
        if implementation is None:
            try:
                implementation = _tool_adapter(provider.factory())
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                raise FlowContractError(
                    f"cannot materialize Adapter {name!r}: {exc}"
                ) from exc
            self._adapters[name] = implementation
        return implementation

    def adapter_extensions(self, name: str) -> tuple[str, ...]:
        """Return plan-time Adapter metadata without materializing it."""

        try:
            return self._adapter_providers[name].accepted_extensions
        except KeyError as exc:
            raise FlowContractError(f"unknown Adapter: {name!r}") from exc

    def has_adapter(self, name: str) -> bool:
        return name in self._adapter_providers
