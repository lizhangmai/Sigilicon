"""Planning and execution seam for trusted tool adapters."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from sigilicon.execution.model import (
    ContractError,
    ExecutionPlan,
    PreflightCheck,
    ResourceBinding,
    Resources,
    Source,
    Step,
    StepContext,
    StepResult,
    adapter_identity,
)


class OwnerView(Protocol):
    root: Path


class PlanningProject(Protocol):
    project_root: Path

    def owner(self, name: str) -> OwnerView: ...

    def owner_for(self, path: Path) -> OwnerView | None: ...


@runtime_checkable
class Adapter(Protocol):
    """Trusted implementation of one planned tool invocation."""

    name: str

    def plan(
        self,
        project: PlanningProject,
        step: Step,
        resources: Resources,
    ) -> Step: ...

    def preflight(
        self,
        step: Step,
        resources: Resources,
    ) -> tuple[PreflightCheck, ...]: ...

    def run(self, context: StepContext, step: Step) -> StepResult: ...


class DirectAdapter:
    """Base for requests whose manifest already contains their source closure."""

    def plan(
        self,
        _project: PlanningProject,
        step: Step,
        _resources: Resources,
    ) -> Step:
        return step


class AdapterRegistry(Mapping[str, Adapter]):
    """Immutable set of package-owned adapters."""

    def __init__(self, values: Iterable[Adapter] = ()) -> None:
        selected: dict[str, Adapter] = {}
        for adapter in values:
            name = getattr(adapter, "name", None)
            try:
                name = adapter_identity(name)
            except ContractError as exc:
                raise ContractError("adapter must expose a canonical identity") from exc
            if not isinstance(adapter, Adapter):
                raise ContractError(
                    f"adapter {name!r} does not satisfy the execution interface"
                )
            if name in selected:
                raise ContractError(f"duplicate adapter identity: {name!r}")
            selected[name] = adapter
        self._values = MappingProxyType(selected)

    def __getitem__(self, name: str) -> Adapter:
        return self._values[name]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


def plan_execution(
    draft: ExecutionPlan,
    *,
    project: PlanningProject,
    adapters: AdapterRegistry,
    resources: Resources,
    authority: object,
) -> ExecutionPlan:
    """Resolve every step to one complete, immutable input closure."""

    if not isinstance(resources, Resources):
        raise TypeError("plan_execution resources must be Resources")

    owner_root = project.owner(draft.owner).root.resolve()
    project_root = project.project_root.resolve()
    captured = {(source.root, source.path): source for source in draft.sources}
    captured_names = {source.path: source.root for source in draft.sources}
    captured_resources: dict[str, ResourceBinding] = {}
    planned_steps: list[Step] = []

    for step in draft.steps:
        adapter = adapters.get(step.uses)
        planned = step if adapter is None else adapter.plan(project, step, resources)
        if not isinstance(planned, Step):
            raise ContractError(f"adapter {step.uses!r} produced an invalid step")
        if (
            planned.id,
            planned.uses,
            planned.needs,
            planned.evidence,
        ) != (step.id, step.uses, step.needs, step.evidence):
            raise ContractError(f"adapter {step.uses!r} rewrote operation structure")
        if planned.sources[: len(step.sources)] != step.sources:
            raise ContractError(
                f"adapter {step.uses!r} removed or reordered declared sources"
            )

        for source in planned._source_snapshots:
            path = source.location
            source_owner = project.owner_for(path)
            if source_owner is not None and source_owner.root.resolve() != owner_root:
                raise ContractError(
                    f"adapter {step.uses!r} selected source owned outside "
                    f"{draft.owner!r}: {path}"
                )
            if source_owner is not None:
                expected_root, expected_scope = owner_root, "owner"
            elif path.is_relative_to(project_root):
                expected_root, expected_scope = project_root, "project"
            else:
                raise ContractError(
                    f"adapter {step.uses!r} selected source outside the project: {path}"
                )
            if source.root != expected_root or source.scope != expected_scope:
                raise ContractError(
                    f"adapter {step.uses!r} selected source with the wrong scope: {path}"
                )
            if not source.current():
                raise ContractError(f"source changed during planning: {source.path}")
            previous_root = captured_names.get(source.path)
            if previous_root is not None and previous_root != source.root:
                raise ContractError("source paths collide across scopes")
            previous = captured.get((source.root, source.path))
            if previous is not None and previous.record != source.record:
                raise ContractError(f"source snapshot collision: {source.path}")
            captured[(source.root, source.path)] = source
            captured_names[source.path] = source.root

        declared_resources = tuple(
            dict.fromkeys(
                (
                    *planned.resources,
                    *planned.runtime.tools.values(),
                    *planned.runtime.files.values(),
                    *planned.runtime.directories.values(),
                    *planned.runtime.values.values(),
                )
            )
        )
        step_resources = {
            binding.identity: binding for binding in planned._resource_bindings
        }
        for identity in declared_resources:
            if identity not in step_resources:
                step_resources[identity] = resources.capture(identity)
        planned = replace(
            planned,
            resources=declared_resources,
            _resource_bindings=tuple(
                step_resources[identity] for identity in declared_resources
            ),
        )

        for resource in planned._resource_bindings:
            if not resources.matches(resource):
                raise ContractError(
                    f"external resource changed during planning: {resource.identity}"
                )
            previous = captured_resources.get(resource.identity)
            if previous is not None and previous.record != resource.record:
                raise ContractError(
                    f"external resource identity collision: {resource.identity}"
                )
            captured_resources[resource.identity] = resource
        planned_steps.append(planned)

    return ExecutionPlan(
        project_identity=draft.project_identity,
        owner=draft.owner,
        operation=draft.operation,
        variant=draft.variant,
        steps=tuple(planned_steps),
        sources=tuple(
            source
            for _key, source in sorted(
                captured.items(), key=lambda item: (str(item[0][0]), item[0][1])
            )
        ),
        resources=tuple(
            captured_resources[name] for name in sorted(captured_resources)
        ),
        _authority=authority,
    )


__all__ = ["Adapter", "AdapterRegistry", "DirectAdapter", "plan_execution"]
