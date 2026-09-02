"""The one real variability seam in managed execution."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from sigilicon.execution.model import (
    BoundExecution,
    ContractError,
    ExternalResource,
    PreflightCheck,
    Operation,
    Step,
    Resources,
    StepContext,
    StepResult,
    ExecutionPlan,
    Source,
    backend_identity,
)


@runtime_checkable
class Backend(Protocol):
    """Trusted package code selected by a Step's ``uses`` identity.

    A Backend is not a sandboxed owner plugin.  Implementations that launch an
    external tool must use Sigilicon's held-fd process and output primitives.
    """

    name: str

    def prepare(
        self,
        project: Any,
        step: Operation,
        resources: Resources,
    ) -> "Preparation": ...

    def preflight(
        self,
        step: Step,
        resources: Resources,
    ) -> tuple[PreflightCheck, ...]: ...

    def run(self, context: StepContext, step: Step) -> StepResult: ...


@dataclass(frozen=True)
class Preparation:
    """Step plus exact Project inputs discovered by one Backend."""

    step: Step
    sources: tuple[Source, ...] = ()
    resources: tuple[ExternalResource, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.step, Step):
            raise ContractError("backend preparation must contain a Step")
        if not isinstance(self.sources, tuple) or any(
            not isinstance(source, Source) for source in self.sources
        ):
            raise ContractError("backend preparation sources must be Source values")
        if not isinstance(self.resources, tuple) or any(
            not isinstance(resource, ExternalResource) for resource in self.resources
        ):
            raise ContractError(
                "backend preparation resources must be ExternalResource values"
            )


class BackendRegistry(Mapping[str, Backend]):
    """Immutable explicit backend set; it performs no global registration."""

    def __init__(self, values: Iterable[Backend] = ()) -> None:
        selected: dict[str, Backend] = {}
        for backend in values:
            name = getattr(backend, "name", None)
            try:
                name = backend_identity(name)
            except ContractError as exc:
                raise ContractError("backend must expose a canonical identity") from exc
            if not isinstance(backend, Backend):
                raise ContractError(f"backend {name!r} does not satisfy the Backend interface")
            if name in selected:
                raise ContractError(f"duplicate backend identity: {name!r}")
            selected[name] = backend
        self._values = MappingProxyType(selected)

    def __getitem__(self, name: str) -> Backend:
        return self._values[name]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


def bind_execution(
    plan: ExecutionPlan,
    *,
    project: Any,
    backends: BackendRegistry,
    resources: Resources,
) -> BoundExecution:
    """Bind a portable plan to exact runtime resources for one invocation."""

    if not isinstance(resources, Resources):
        raise TypeError("bind_execution resources must be Resources")

    owner_root = project.owner(plan.owner).root.resolve()
    project_root = project.project_root.resolve()
    bound_sources = tuple(
        reference.bind({"owner": owner_root, "project": project_root})
        for reference in plan.sources
    )
    captured = {(source.root, source.path): source for source in bound_sources}
    captured_names = {source.path: source.root for source in bound_sources}
    captured_resources: dict[str, ExternalResource] = {}
    steps = []
    for portable_step in plan.steps:
        step = Operation(
            portable_step.id,
            portable_step.uses,
            portable_step.request,
            portable_step.needs,
            portable_step.sources,
            portable_step.evidence,
        )
        try:
            backend = backends[step.uses]
        except KeyError as exc:
            raise ContractError(f"unknown trusted backend: {step.uses!r}") from exc
        preparation = backend.prepare(project, step, resources)
        if not isinstance(preparation, Preparation):
            raise ContractError(
                f"backend {step.uses!r} produced an invalid preparation"
            )
        prepared = preparation.step
        if (
            prepared.id,
            prepared.uses,
            prepared.needs,
            prepared.evidence,
        ) != (step.id, step.uses, step.needs, step.evidence):
            raise ContractError(f"backend {step.uses!r} rewrote operation structure")
        if prepared.sources[: len(step.sources)] != step.sources:
            raise ContractError(
                f"backend {step.uses!r} removed or reordered operation sources"
            )
        changed_during_preparation = tuple(
            source.path for source in captured.values() if not source.current()
        )
        if changed_during_preparation:
            raise ContractError(
                "operation source changed during backend preparation: "
                + ", ".join(sorted(set(changed_during_preparation)))
            )
        source_names = list(step.sources)
        for source in sorted(
            preparation.sources, key=lambda item: (str(item.root), item.path)
        ):
            path = source.location
            source_owner = project.owner_for(source.location)
            if source_owner is not None and source_owner.name != plan.owner:
                raise ContractError(
                    f"backend {step.uses!r} prepared source owned by "
                    f"{source_owner.name!r}: {path}"
                )
            if source_owner is not None:
                expected_root, expected_scope = owner_root, "owner"
            elif path.is_relative_to(project_root):
                expected_root, expected_scope = project_root, "project"
            else:
                raise ContractError(
                    f"backend {step.uses!r} prepared a source outside the Project: {path}"
                )
            if source.root != expected_root or source.scope != expected_scope:
                raise ContractError(
                    f"backend {step.uses!r} prepared a source with the wrong scope: {path}"
                )
            current = Source.capture(path, root=expected_root, scope=expected_scope)
            if current.sha256 != source.sha256 or not source.current():
                raise ContractError(f"backend source changed during preparation: {source.path}")
            previous = captured.get((source.root, source.path))
            if previous is not None and not previous.current():
                raise ContractError(
                    f"source changed between operation compilation and backend preparation: "
                    f"{source.path}"
                )
            previous_root = captured_names.get(source.path)
            if previous_root is not None and previous_root != source.root:
                raise ContractError("backend source paths collide across scopes")
            if previous is None:
                captured[(source.root, source.path)] = source
            captured_names[source.path] = source.root
            if source.path not in source_names:
                source_names.append(source.path)
        discovered = {source.path for source in preparation.sources}
        unexplained = set(prepared.sources) - set(step.sources) - discovered
        omitted = discovered - set(prepared.sources)
        if unexplained:
            raise ContractError(
                f"backend {step.uses!r} prepared unexplained sources: "
                + ", ".join(sorted(unexplained))
            )
        if omitted:
            raise ContractError(
                f"backend {step.uses!r} omitted prepared sources: "
                + ", ".join(sorted(omitted))
            )
        discovered_resources = {
            resource.identity for resource in preparation.resources
        }
        if set(prepared.resources) != discovered_resources:
            raise ContractError(
                f"backend {step.uses!r} external resource closure disagrees "
                "with its prepared step"
            )
        for resource in preparation.resources:
            if not resource.current():
                raise ContractError(
                    "external resource changed during preparation: "
                    f"{resource.identity}"
                )
            previous = captured_resources.get(resource.identity)
            if previous is not None and previous.sha256 != resource.sha256:
                raise ContractError(
                    "external resource identity collision: "
                    f"{resource.identity}"
                )
            captured_resources[resource.identity] = resource
        steps.append(replace(prepared, sources=tuple(source_names)))
    return BoundExecution(
        plan=plan,
        steps=tuple(steps),
        sources=tuple(captured.values()),
        resources_identity=resources.identity,
        resources=tuple(
            captured_resources[identity] for identity in sorted(captured_resources)
        ),
    )


__all__ = ["Backend", "BackendRegistry", "Preparation", "bind_execution"]
