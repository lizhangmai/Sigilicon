"""Deterministic preflight and managed execution for typed operation plans."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import ExitStack, contextmanager
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any, Iterator

from sigilicon.artifacts import RunRecord, SafeTree, new_identity, read_nofollow_text
from sigilicon.execution._model import (
    Artifact,
    ContractError,
    ExecutionPlan,
    ExecutionError,
    PreflightCheck,
    PreflightResult,
    ResourceBinding,
    Resources,
    RunResult,
    Step,
    ExecutionIO,
    StepOutcome,
    StepResult,
    json_value,
)
from sigilicon.execution.adapter import AdapterRegistry
from sigilicon.external_tools import (
    owned_input_closure,
    process_group_cleanup_uncertainty,
)
from sigilicon.paths import ArtifactLayout, RunPaths


Progress = Callable[[str, str], None]


class InputIntegrityError(ExecutionError):
    """A sealed execution input changed while an adapter could consume it."""


@contextmanager
def _held_step_inputs(
    step: Step,
    source_root: Path,
    resource_root: Path | None,
    bindings: Mapping[str, ResourceBinding],
) -> Iterator[None]:
    """Hold and monitor every file input visible to one adapter invocation."""

    source_paths = [source_root / source for source in step.sources]
    resource_paths: list[Path] = []
    resource_directories: list[Path] = []
    if resource_root is not None:
        for identity in step.resources:
            binding = bindings[identity]
            if binding.kind == "file":
                resource_paths.append(resource_root / binding.materialization_key)
            elif binding.kind == "directory":
                base = resource_root / binding.materialization_key
                resource_paths.extend(base / item.path for item in binding.files)
                resource_directories.extend(
                    (base, *(base / relative for relative in binding.directories))
                )

    monitor = ExitStack()
    try:
        monitor.enter_context(
            owned_input_closure(source_root, files=source_paths)
        )
        if resource_root is not None and (
            resource_paths or resource_directories
        ):
            monitor.enter_context(
                owned_input_closure(
                    resource_root,
                    files=resource_paths,
                    directories=resource_directories,
                )
            )
    except (OSError, RuntimeError) as exc:
        raise InputIntegrityError(
            "could not bind the sealed adapter input closure"
        ) from exc
    try:
        yield
    except BaseException as execution_error:
        try:
            monitor.close()
        except (OSError, RuntimeError):
            raise InputIntegrityError(
                "sealed adapter input changed during execution"
            ) from execution_error
        raise
    else:
        try:
            monitor.close()
        except (OSError, RuntimeError) as exc:
            raise InputIntegrityError(
                "sealed adapter input changed during execution"
            ) from exc


def _refresh_failure_inventory(record: RunRecord, paths: RunPaths) -> None:
    """Make a failed run's safe on-disk state readable by RunStore."""

    for role in ("inputs", "outputs", "logs"):
        _register_tree(record, role, paths.role(role))


def _preflight(
    plan: ExecutionPlan,
    resources: Resources,
    adapters: AdapterRegistry,
) -> PreflightResult:
    """Check exact sources and only the adapters selected by this plan."""

    checks: list[PreflightCheck] = []
    seen_sources: set[tuple[Path, str]] = set()
    for source in (*plan._composition_sources, *plan.sources):
        identity = (source.root, source.path)
        if identity in seen_sources:
            continue
        seen_sources.add(identity)
        current = source.current()
        checks.append(
            PreflightCheck(
                "source",
                source.path,
                "ready" if current else "blocked",
                "exact source snapshot" if current else "source changed after planning",
            )
        )
    for resource in plan.resources:
        current = resources.matches(resource)
        checks.append(
            PreflightCheck(
                "external-resource",
                resource.identity,
                "ready" if current else "blocked",
                "exact resource snapshot"
                if current
                else "resource changed after planning",
            )
        )
    for step in plan.steps:
        step.validate_action()
        try:
            adapter = adapters[step.uses]
        except KeyError:
            checks.append(
                PreflightCheck(
                    "adapter",
                    step.uses,
                    "blocked",
                    f"selected by step {step.id!r} but not provided",
                )
            )
            continue
        checks.append(PreflightCheck("adapter", step.uses, "ready", step.id))
        adapter_checks = adapter.preflight(step, resources)
        if not isinstance(adapter_checks, tuple) or any(
            not isinstance(check, PreflightCheck) for check in adapter_checks
        ):
            raise TypeError("adapter preflight must return PreflightCheck values")
        checks.extend(adapter_checks)
    return PreflightResult(plan.identity, tuple(checks))


def _validate_artifact(
    artifact: Artifact,
    *,
    output_root: Path,
    run_root: Path,
) -> None:
    path = artifact.path.absolute()
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ExecutionError(
            f"adapter published an unsafe or missing artifact: {artifact.path}"
        ) from exc
    if (
        path.resolve() != path
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or path.is_symlink()
        or not path.is_relative_to(output_root)
        or not path.is_relative_to(run_root)
    ):
        raise ExecutionError(
            f"adapter published an unsafe or missing artifact: {artifact.path}"
        )


def _validate_output_inventory(
    result: StepResult,
    *,
    output_root: Path,
    adapter: str,
) -> None:
    """Require the adapter to publish the exact regular-file output closure."""

    try:
        inventory = SafeTree(output_root).inventory(verify_content=False)
    except (OSError, RuntimeError) as exc:
        raise ExecutionError(
            f"adapter {adapter!r} output inventory is unsafe: {exc}"
        ) from exc
    published = {
        artifact.path.absolute().relative_to(output_root)
        for artifact in result.artifacts
    }
    required_directories = {
        parent
        for relative in published
        for parent in relative.parents
        if parent != Path(".")
    }
    if (
        set(inventory.files) != published
        or set(inventory.directories) != required_directories
    ):
        raise ExecutionError(
            f"adapter {adapter!r} output inventory does not match "
            "its published artifacts"
        )


def _seal_sources(record: RunRecord, plan: ExecutionPlan) -> Path:
    """Materialize the plan closure once; adapters consume only these copies."""

    root = record.directory("inputs", "sources")
    for source in plan.sources:
        components = ("sources", *Path(source.path).parts)
        path = record.copy_file(
            "inputs",
            components,
            source.location,
            expected_size=source.size,
            expected_sha256=source.sha256,
        )
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            os.fchmod(descriptor, 0o555 if source.executable else 0o444)
        finally:
            os.close(descriptor)
    for directory in sorted(
        (path for path in root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        directory.chmod(0o555)
    root.chmod(0o555)
    return root


def _seal_resources(record: RunRecord, plan: ExecutionPlan) -> Path | None:
    """Materialize host resources without persisting their original locations."""

    materialized = tuple(
        resource
        for resource in plan.resources
        if resource.kind in {"file", "directory"}
    )
    if not materialized:
        return None
    root = record.directory("inputs", "resources")
    for resource in materialized:
        components = ("resources", resource.materialization_key)
        if resource.kind == "file":
            item = resource.files[0]
            path = record.copy_file(
                "inputs",
                components,
                item.location,
                expected_size=item.size,
                expected_sha256=item.sha256,
            )
            path.chmod(0o555 if item.executable else 0o444)
            continue
        resource_root = record.directory("inputs", *components)
        record.add_file("inputs", resource_root)
        for relative in resource.directories:
            directory = record.directory(
                "inputs",
                *components,
                *PurePosixPath(relative).parts,
            )
            record.add_file("inputs", directory)
        for item in resource.files:
            path = record.copy_file(
                "inputs",
                (*components, *PurePosixPath(item.path).parts),
                item.location,
                expected_size=item.size,
                expected_sha256=item.sha256,
            )
            path.chmod(0o555 if item.executable else 0o444)
        for directory in sorted(
            (path for path in resource_root.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            directory.chmod(0o555)
        resource_root.chmod(0o555)
    root.chmod(0o555)
    return root


def _register_tree(record: RunRecord, role: str, root: Path) -> None:
    """Close the owned inventory without accepting symlinks or path replacement."""

    if root.resolve() != root.absolute() or not root.is_dir() or root.is_symlink():
        raise ExecutionError(f"managed {role} root was replaced during adapter execution")
    for path in sorted(root.rglob("*"), key=lambda item: (len(item.parts), str(item))):
        if path.is_symlink() or path.resolve() != path.absolute():
            raise ExecutionError(f"adapter created an unsafe managed path: {path}")
        record.add_file(role, path)


def _run(
    plan: ExecutionPlan,
    resources: Resources,
    adapters: AdapterRegistry,
    *,
    artifact_root: Path,
    run_id: str | None = None,
    progress: Progress | None = None,
) -> RunResult:
    """Run a preflighted plan once and persist a closed immutable result."""

    checked = _preflight(plan, resources, adapters)
    if not checked.ready:
        blocked = "; ".join(
            f"{check.kind}:{check.subject}: {check.detail}"
            for check in checked.checks
            if check.status == "blocked"
        )
        raise ExecutionError(f"operation preflight is blocked: {blocked}")
    identity = new_identity() if run_id is None else run_id
    operation_id = new_identity()
    plan_identity = plan.identity
    paths = ArtifactLayout(Path(artifact_root).resolve()).operation_run(
        owner=plan.owner,
        operation=plan.operation,
        variant=plan.variant,
        run_id=identity,
    )
    record = RunRecord.begin(
        paths,
        adapter="sigilicon.execution",
        source={"plan_identity": plan_identity},
    )
    outcomes: list[StepOutcome] = []
    by_id: dict[str, StepResult] = {}
    integrity_failure: list[str] = []
    with record.failure_boundary(
        uncertainty=lambda: integrity_failure[0] if integrity_failure else None,
        partial_failure=lambda: (
            {
                "completed_steps": [outcome.step for outcome in outcomes],
                "plan_identity": plan_identity,
            }
            if outcomes
            else None
        ),
        prepare_failure=lambda: _refresh_failure_inventory(record, paths),
    ):
        record.bind_operation(operation_id)
        record.write_json("inputs", ("execution-plan.json",), plan.record)
        record.write_json(
            "inputs",
            ("runtime-bindings.json",),
            {
                "schema": 6,
                "contract_kind": "runtime-bindings",
                "capabilities": sorted(resources.capabilities),
                "inherit_environment": list(resources.inherit_environment),
                "environment": resources.environment_record,
                "configuration": {
                    label: {
                        binding.identity: table[binding.identity]
                        for binding in sorted(
                            plan.resources,
                            key=lambda selected: selected.identity,
                        )
                        if binding.kind == kind
                        and binding.identity in table
                    }
                    for label, kind, table in (
                        ("tools", "tool", resources.tools),
                        ("files", "file", resources.files),
                        ("directories", "directory", resources.directories),
                        ("values", "value", resources.values),
                    )
                },
            },
        )
        source_root = _seal_sources(record, plan)
        resource_root = _seal_resources(record, plan)
        changed_composition = tuple(
            source.path
            for source in plan._composition_sources
            if not source.metadata_current()
        )
        if changed_composition:
            raise ExecutionError(
                "project composition changed after input sealing: "
                + ", ".join(sorted(set(changed_composition)))
            )
        sources = {source.path: source for source in plan.sources}
        bindings = {binding.identity: binding for binding in plan.resources}
        execution_resources = resources.for_execution(
            plan.resources,
            resource_root,
        )
        for step in plan.steps:
            if progress is not None:
                progress(step.id, "running")
            dependencies = {name: by_id[name] for name in step.needs}
            failed_dependencies = tuple(
                name
                for name, result in dependencies.items()
                if result.status != "succeeded"
            )
            if failed_dependencies:
                result = StepResult(
                    "blocked",
                    message=f"dependencies did not succeed: {', '.join(failed_dependencies)}",
                )
            else:
                record.directory("work", step.id)
                output_root = record.directory("outputs", step.id)
                record.add_file("outputs", output_root)
                context = ExecutionIO(
                    plan_identity=plan_identity,
                    step=step,
                    run_id=identity,
                    operation_id=operation_id,
                    _run_root=paths.root,
                    _resources=execution_resources,
                    _dependencies=dependencies,
                    _source_scopes={
                        name: sources[name].scope for name in step.sources
                    },
                    _resource_digests={
                        name: bindings[name].sha256 for name in step.resources
                    },
                    _resource_kinds={
                        name: bindings[name].kind for name in step.resources
                    },
                    _register_mutation=(
                        lambda operation: operation.register_artifact(record)
                    ),
                )
                try:
                    adapter = adapters[step.uses]
                except KeyError as exc:
                    raise ExecutionError(
                        f"trusted adapter is unavailable: {step.uses!r}"
                    ) from exc
                try:
                    with _held_step_inputs(
                        step,
                        source_root,
                        resource_root,
                        bindings,
                    ):
                        try:
                            result = adapter.run(context)
                            if not isinstance(result, StepResult):
                                raise TypeError("adapter run must return StepResult")
                            for artifact in result.artifacts:
                                _validate_artifact(
                                    artifact,
                                    output_root=output_root,
                                    run_root=paths.root,
                                )
                        except (KeyboardInterrupt, SystemExit):
                            raise
                        except Exception as exc:
                            uncertainty = process_group_cleanup_uncertainty(exc)
                            if uncertainty is None:
                                raise
                            result = StepResult.uncertain(uncertainty)
                except InputIntegrityError as exc:
                    integrity_failure.append(str(exc))
                    raise
                _validate_output_inventory(
                    result,
                    output_root=output_root,
                    adapter=step.uses,
                )
                for artifact in result.artifacts:
                    record.add_file("outputs", artifact.path)
            by_id[step.id] = result
            outcome = StepOutcome(step.id, step.uses, result)
            outcomes.append(outcome)
            record.write_json(
                "outputs",
                (f"step-{step.id}-result.json",),
                {
                    "schema": 1,
                    "contract_kind": "step-result",
                    "step": step.id,
                    "uses": step.uses,
                    "status": result.status,
                    "message": result.message,
                    "facts": json_value(result.facts),
                    "artifacts": [
                        {
                            "role": artifact.role,
                            "kind": artifact.kind,
                            "path": artifact.path.relative_to(
                                paths.root
                            ).as_posix(),
                            "qualifiers": json_value(artifact.qualifiers),
                        }
                        for artifact in result.artifacts
                    ],
                },
            )
            if progress is not None:
                progress(step.id, result.status)
        step_statuses = {outcome.result.status for outcome in outcomes}
        if step_statuses == {"succeeded"}:
            status = "succeeded"
        elif "uncertain" in step_statuses:
            status = "uncertain"
        elif "partial" in step_statuses:
            status = "partial"
        elif "cancelled" in step_statuses:
            status = "cancelled"
        else:
            status = "failed"
        result = RunResult(
            plan.owner,
            plan.operation,
            plan.variant,
            identity,
            operation_id,
            plan_identity,
            status,
            tuple(outcomes),
        )
        result_path = record.write_json(
            "outputs",
            ("run-result.json",),
            result.record,
        )
        terminal_arguments: dict[str, Any] = {}
        if result.status == "partial":
            terminal_arguments["partial_failure"] = {
                "completed_steps": [
                    outcome.step
                    for outcome in outcomes
                    if outcome.result.status == "succeeded"
                ],
                "step_statuses": {
                    outcome.step: outcome.result.status for outcome in outcomes
                },
            }
        elif result.status == "uncertain":
            terminal_arguments["uncertain_reason"] = "; ".join(
                outcome.result.message
                for outcome in outcomes
                if outcome.result.status == "uncertain"
            ) or "one or more execution steps have an uncertain outcome"
        record.complete(
            result.status,
            completion_evidence=(result_path,),
            details={
                "run_status": result.status,
                "result_sha256": hashlib.sha256(
                    read_nofollow_text(result_path).encode("utf-8")
                ).hexdigest(),
            },
            **terminal_arguments,
        )
        return result


__all__: list[str] = []
