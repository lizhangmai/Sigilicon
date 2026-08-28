"""Private worker for one exact, already-authorized Flow Plan."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import signal
from typing import Any, Sequence

from sigilicon.artifacts import read_nofollow_text
from sigilicon.flow import ExecutionEnvironment, FlowProgress, load_execution_environment
from sigilicon.paths import ProjectContext
from sigilicon.workflows.agentic_read import AgenticReadInterface
from sigilicon.workflows.agentic_runs import (
    AGENTIC_RUN_AUDIT_KIND,
    AgenticRunStore,
    RUNNING_STATUSES,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--flow", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--environment", type=Path)
    parser.add_argument("--environment-sha256")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    read = AgenticReadInterface.from_project_context(
        ProjectContext.from_project_root(args.project_root)
    )
    store = AgenticRunStore(read.repository.project.artifact_root, read.project_id)
    paths = store.paths(
        owner=args.owner,
        flow=args.flow,
        target=args.target,
        run_id=args.run_id,
    )
    request = store.read_request(paths)
    for field, expected in (
        ("owner", args.owner),
        ("flow", args.flow),
        ("target", args.target),
        ("run_id", args.run_id),
    ):
        if request[field] != expected:
            raise ValueError("worker arguments disagree with the authorized request")
    resolved = read.resolve_plan_identity(request["plan_identity"])
    plan = resolved.plan
    if (
        plan.spec.owner != request["owner"]
        or plan.spec.flow_id != request["flow"]
        or plan.target.target_id != request["target"]
        or plan.profile.profile_id != request["profile"]
        or len(plan.nodes) != request["total_nodes"]
    ):
        raise ValueError("worker Flow Plan identity drift")

    environment: ExecutionEnvironment
    if args.environment is None:
        if args.environment_sha256 is not None or request["environment_identity"] != "empty-environment":
            raise ValueError("worker execution environment identity drift")
        environment = ExecutionEnvironment()
    else:
        if args.environment_sha256 is None:
            raise ValueError("worker environment hash is missing")
        text = read_nofollow_text(args.environment.resolve())
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != args.environment_sha256:
            raise ValueError("worker execution environment content drift")
        if request["environment_identity"] != f"environment-{args.environment_sha256[:24]}":
            raise ValueError("worker execution environment identity drift")
        environment = load_execution_environment(args.environment)

    started_at = _now()
    state = store.read_state(paths)
    if state["status"] != "queued":
        raise ValueError("worker requires one queued managed run")
    store.write_state(
        paths,
        {
            **state,
            "started_at": started_at,
        },
    )
    budget_expired = False

    def interrupt(_signal_number: int, _frame: Any) -> None:
        nonlocal budget_expired
        if _signal_number == signal.SIGALRM:
            budget_expired = True
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGALRM, interrupt)
    signal.setitimer(signal.ITIMER_REAL, request["budget"]["maximum_seconds"])

    def progress(value: FlowProgress) -> None:
        if value.status != "running":
            return
        current = store.read_state(paths)
        if current["status"] not in RUNNING_STATUSES:
            raise ValueError("worker progress attempted to rewrite terminal state")
        store.write_state(
            paths,
            {
                **current,
                "status": "running",
                "started_at": current["started_at"] or started_at,
                "completed_nodes": value.completed_nodes,
                "current_node": value.current_node,
            },
        )

    flow_result = None
    terminal_status = "failed"
    error_code = None
    try:
        flow_result = resolved.engine.run(
            plan,
            artifact_root=read.repository.project.artifact_root,
            environment=environment,
            run_id=request["run_id"],
            progress=progress,
        )
        terminal_status = (
            "budget-exhausted"
            if budget_expired
            else "cancelled"
            if flow_result.interrupted
            else flow_result.status
        )
        error_code = "time-budget-exhausted" if budget_expired else None
    except KeyboardInterrupt:
        terminal_status = "budget-exhausted" if budget_expired else "cancelled"
        error_code = "time-budget-exhausted" if budget_expired else "cancel-requested"
    except BaseException:
        terminal_status = "failed"
        error_code = "flow-worker-failed"
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)

    current = store.read_state(paths)
    finished_at = _now()
    store.write_state(
        paths,
        {
            **current,
            "status": terminal_status,
            "finished_at": finished_at,
            "current_node": None,
            "completed_nodes": (
                request["total_nodes"]
                if flow_result is not None
                else current["completed_nodes"]
            ),
            "error_code": error_code,
        },
    )
    store.write_audit(
        paths,
        {
            "schema": 1,
            "contract_kind": AGENTIC_RUN_AUDIT_KIND,
            "project_id": request["project_id"],
            "owner": request["owner"],
            "flow": request["flow"],
            "target": request["target"],
            "run_id": request["run_id"],
            "plan_identity": request["plan_identity"],
            "grant_identity": request["grant_identity"],
            "principal": request["principal"],
            "role": request["role"],
            "approval": request["approval"],
            "required_capabilities": request["required_capabilities"],
            "budget": request["budget"],
            "environment_identity": request["environment_identity"],
            "submitted_at": request["submitted_at"],
            "started_at": started_at,
            "finished_at": finished_at,
            "terminal_status": terminal_status,
            "flow_status": None if flow_result is None else flow_result.status,
            "error_code": error_code,
        },
    )
    return 0 if flow_result is not None or terminal_status in {"cancelled", "budget-exhausted"} else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
