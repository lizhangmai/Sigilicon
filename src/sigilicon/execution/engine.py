"""Deterministic preflight and managed execution for typed operation plans."""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import os
from pathlib import Path
import stat
from typing import Any

from sigilicon.artifacts import ArtifactRecord, new_identity, read_nofollow_text
from sigilicon.execution.model import (
    Artifact,
    ContractError,
    ExecutionError,
    ExecutionPlan,
    PreflightCheck,
    PreflightResult,
    Resources,
    RunResult,
    StepContext,
    StepOutcome,
    StepResult,
    json_value,
)
from sigilicon.external_tools import process_group_cleanup_uncertainty
from sigilicon.paths import ArtifactLayout


Progress = Callable[[str, str], None]


def preflight(
    plan: ExecutionPlan,
    resources: Resources,
) -> PreflightResult:
    """Check exact sources and only the backends selected by this plan."""

    checks: list[PreflightCheck] = []
    seen_sources: set[tuple[Path, str]] = set()
    for source in (
        *plan.sources,
    ):
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
    for step in plan.steps:
        try:
            backend = plan._backend_for(step)
        except ContractError:
            checks.append(
                PreflightCheck(
                    "backend",
                    step.uses,
                    "blocked",
                    f"selected by step {step.id!r} but not provided",
                )
            )
            continue
        checks.append(PreflightCheck("backend", step.uses, "ready", step.id))
        try:
            backend_checks = backend.preflight(step, resources)
            if not isinstance(backend_checks, tuple) or any(
                not isinstance(check, PreflightCheck) for check in backend_checks
            ):
                raise TypeError("backend preflight must return PreflightCheck values")
            checks.extend(backend_checks)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            checks.append(
                PreflightCheck(
                    "backend-preflight",
                    step.uses,
                    "blocked",
                    f"{type(exc).__name__}: {exc}",
                )
            )
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
            f"backend published an unsafe or missing artifact: {artifact.path}"
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
            f"backend published an unsafe or missing artifact: {artifact.path}"
        )


def _seal_sources(record: ArtifactRecord, plan: ExecutionPlan) -> Path:
    """Materialize the plan closure once; backends consume only these copies."""

    root = record.directory("inputs", "sources")
    for source in plan.sources:
        components = ("sources", *Path(source.path).parts)
        path = record.write_text("inputs", components, source.text)
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            os.fchmod(descriptor, 0o555 if source.executable else 0o444)
        finally:
            os.close(descriptor)
        record.add_file("inputs", path)
    for directory in sorted(
        (path for path in root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        directory.chmod(0o555)
    root.chmod(0o555)
    return root


def _register_tree(record: ArtifactRecord, role: str, root: Path) -> None:
    """Close the owned inventory without accepting symlinks or path replacement."""

    if root.resolve() != root.absolute() or not root.is_dir() or root.is_symlink():
        raise ExecutionError(f"managed {role} root was replaced during backend execution")
    for path in sorted(root.rglob("*"), key=lambda item: (len(item.parts), str(item))):
        if path.is_symlink() or path.resolve() != path.absolute():
            raise ExecutionError(f"backend created an unsafe managed path: {path}")
        record.add_file(role, path)


def run(
    plan: ExecutionPlan,
    resources: Resources,
    *,
    artifact_root: Path,
    project_root: Path,
    owner_root: Path,
    workspace_root: Path,
    run_id: str | None = None,
    progress: Progress | None = None,
) -> RunResult:
    """Run a preflighted plan once and persist a closed immutable result."""

    checked = preflight(plan, resources)
    if not checked.ready:
        blocked = "; ".join(
            f"{check.kind}:{check.subject}: {check.detail}"
            for check in checked.checks
            if check.status == "blocked"
        )
        raise ExecutionError(f"operation preflight is blocked: {blocked}")
    identity = new_identity() if run_id is None else run_id
    operation_id = new_identity()
    paths = ArtifactLayout(Path(artifact_root).resolve()).execution(
        owner=plan.owner,
        target=plan.target,
        flow=plan.operation,
        variant="default",
        identity=identity,
        artifact_kind="execution-run",
        identity_kind="run_id",
    )
    record = ArtifactRecord.begin(
        paths,
        entities={"owner": plan.owner, "target": plan.target},
        operation=plan.operation,
        backend="sigilicon.execution",
        source={"plan_identity": plan.identity},
    )
    outcomes: list[StepOutcome] = []
    by_id: dict[str, StepResult] = {}
    with record.failure_boundary(
        partial_failure=lambda: (
            {
                "completed_steps": [outcome.step for outcome in outcomes],
                "plan_identity": plan.identity,
            }
            if outcomes
            else None
        )
    ):
        record.bind_operation(operation_id)
        record.write_json("inputs", ("execution-plan.json",), plan.record)
        record.write_json("inputs", ("preflight.json",), checked.record)
        changed_at_seal = tuple(
            source.path for source in plan.sources if not source.current()
        )
        if changed_at_seal:
            raise ExecutionError(
                "operation source changed immediately before backend input sealing: "
                + ", ".join(sorted(set(changed_at_seal)))
            )
        source_root = _seal_sources(record, plan)
        for step in plan.steps:
            if progress is not None:
                progress(step.id, "running")
            dependencies = {name: by_id[name] for name in step.needs}
            failed_dependencies = tuple(
                name for name, result in dependencies.items() if result.status != "succeeded"
            )
            if failed_dependencies:
                result = StepResult(
                    "blocked",
                    message=f"dependencies did not succeed: {', '.join(failed_dependencies)}",
                )
            else:
                changed = tuple(
                    source.path
                    for source in plan.sources
                    if not source.current()
                )
                if changed:
                    raise ExecutionError(
                        "operation source changed immediately before backend start: "
                        + ", ".join(sorted(set(changed)))
                    )
                work_root = record.directory("work", step.id)
                output_root = record.directory("outputs", step.id)
                record.write_json(
                    "inputs",
                    (f"step-{step.id}-request.json",),
                    {
                        "schema": 1,
                        "contract_kind": "step-request",
                        "run_id": identity,
                        "operation_id": operation_id,
                        "plan_identity": plan.identity,
                        "step": step.record,
                    },
                )
                context = StepContext(
                    plan_identity=plan.identity,
                    step=step,
                    run_id=identity,
                    operation_id=operation_id,
                    work_root=work_root,
                    output_root=output_root,
                    source_root=source_root,
                    resources=resources,
                    dependencies=dependencies,
                    project_root=project_root,
                    owner_root=owner_root,
                    workspace_root=workspace_root,
                    source_scopes={
                        source.path: source.scope
                        for source in plan.sources
                        if source.path in step.sources
                    },
                    _register_operation=(
                        lambda operation: operation.register_artifact(record)
                    ),
                )
                backend = plan._backend_for(step)
                try:
                    result = backend.run(context)
                    if not isinstance(result, StepResult):
                        raise TypeError("backend run must return StepResult")
                    for artifact in result.artifacts:
                        _validate_artifact(
                            artifact,
                            output_root=output_root,
                            run_root=paths.root,
                        )
                        record.add_file("outputs", artifact.path)
                except (KeyboardInterrupt, SystemExit):
                    raise
                except Exception as exc:
                    uncertainty = process_group_cleanup_uncertainty(exc)
                    result = (
                        StepResult.uncertain(uncertainty)
                        if uncertainty is not None
                        else StepResult.failed(f"{type(exc).__name__}: {exc}")
                    )
                published = {artifact.path for artifact in result.artifacts}
                actual_outputs = {
                    path.absolute()
                    for path in output_root.rglob("*")
                    if path.is_file() and not path.is_symlink()
                }
                if result.status == "succeeded" and actual_outputs != published:
                    raise ExecutionError(
                        f"backend {step.uses!r} output inventory does not match "
                        "its published artifacts"
                    )
                _register_tree(record, "work", work_root)
                _register_tree(record, "outputs", output_root)
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
                                "path": artifact.path.relative_to(paths.root).as_posix(),
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
            plan.target,
            plan.operation,
            identity,
            operation_id,
            plan.identity,
            status,
            tuple(outcomes),
            paths.root,
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


__all__ = ["Progress", "preflight", "run"]
