"""The one real variability seam in managed execution."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from sigilicon.execution.model import (
    ContractError,
    PreflightCheck,
    Resources,
    Step,
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

    def preflight(
        self,
        step: Step,
        resources: Resources,
    ) -> tuple[PreflightCheck, ...]: ...

    def run(self, context: StepContext) -> StepResult: ...


class Backends(Mapping[str, Backend]):
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


def bind_plan(
    plan: ExecutionPlan,
    *,
    project: Any,
    backends: Backends,
) -> ExecutionPlan:
    """Bind every Step once to package-owned implementation and domain data."""

    selected: dict[str, Backend] = {}
    captured = {(source.root, source.path): source for source in plan.sources}
    captured_names = {source.path: source.root for source in plan.sources}
    steps = []
    owner_root = project.owner(plan.owner).root.resolve()
    project_root = project.project_root.resolve()
    for step in plan.steps:
        try:
            backend = backends[step.uses]
        except KeyError as exc:
            raise ContractError(f"unknown trusted backend: {step.uses!r}") from exc
        binder = getattr(backend, "bind", None)
        bound = backend if binder is None else binder(project, step)
        if not isinstance(bound, Backend) or bound.name != step.uses:
            raise ContractError(
                f"backend {step.uses!r} produced an invalid Step binding"
            )
        selected[step.id] = bound
        source_names = list(step.sources)
        bindings = getattr(bound, "binding_sources", {})
        if not isinstance(bindings, Mapping) or any(
            not isinstance(path, Path) or not isinstance(digest, str)
            for path, digest in bindings.items()
        ):
            raise ContractError(
                f"backend {step.uses!r} produced invalid source bindings"
            )
        for raw_path, digest in sorted(
            bindings.items(), key=lambda item: str(item[0])
        ):
            path = raw_path.absolute()
            if path != path.resolve():
                raise ContractError(
                    f"backend {step.uses!r} bound a symlinked source: {path}"
                )
            source_owner = project.owner_for(path)
            if source_owner is not None and source_owner.name != plan.owner:
                raise ContractError(
                    f"backend {step.uses!r} bound source owned by "
                    f"{source_owner.name!r}: {path}"
                )
            if source_owner is not None:
                root, scope = owner_root, "owner"
            elif path.is_relative_to(project_root):
                root, scope = project_root, "project"
            else:
                raise ContractError(
                    f"backend {step.uses!r} bound a source outside the Project: {path}"
                )
            source = Source.capture(path, root=root, scope=scope)
            if source.sha256 != digest:
                raise ContractError(
                    f"backend {step.uses!r} source changed during binding: {source.path}"
                )
            previous_root = captured_names.get(source.path)
            if previous_root is not None and previous_root != source.root:
                raise ContractError("backend source paths collide across scopes")
            captured[(source.root, source.path)] = source
            captured_names[source.path] = source.root
            if source.path not in source_names:
                source_names.append(source.path)
        steps.append(replace(step, sources=tuple(source_names)))
    return replace(
        plan,
        steps=tuple(steps),
        sources=tuple(captured.values()),
        _backends=selected,
    )


__all__ = ["Backend", "Backends", "bind_plan"]
