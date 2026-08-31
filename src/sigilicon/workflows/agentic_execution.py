"""Authorized durable execution above the single-attempt deterministic FlowEngine."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

from sigilicon.canonical import canonical_json
from sigilicon.domain.agentic_execution import (
    AgenticExecutionBudget,
    AgenticExecutionCapability,
    AgenticExecutionGrant,
)
from sigilicon.external_tools import (
    ManagedBackgroundProcess,
    spawn_agentic_flow_worker,
)
from sigilicon.flow import (
    ExecutionEnvironment,
    load_execution_environment_contract,
)
from sigilicon.workflows.agentic_read import AgenticReadInterface
from sigilicon.workflows.run_read import RunReadInterface
from sigilicon.workflows.project_runner import ProjectRunner
from sigilicon.workflows.agentic_runs import (
    AGENTIC_RUN_AUDIT_KIND,
    AGENTIC_RUN_REQUEST_KIND,
    AGENTIC_RUN_STATE_KIND,
    AgenticRunStore,
    RUNNING_STATUSES,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _worker_environment() -> dict[str, str]:
    environment = dict(os.environ)
    pythonpath = environment.get("PYTHONPATH")
    if pythonpath:
        environment["PYTHONPATH"] = os.pathsep.join(
            str(Path(item).resolve()) if item else item
            for item in pythonpath.split(os.pathsep)
        )
    return environment


class AgenticExecutionInterface:
    """Deep execution seam shared by Python, CLI, and MCP clients."""

    def __init__(
        self,
        read: AgenticReadInterface,
        *,
        grant: AgenticExecutionGrant,
        environment_contract: Path | None = None,
    ) -> None:
        self.read = read
        self.grant = grant
        self.environment_contract = (
            None if environment_contract is None else Path(environment_contract).resolve()
        )
        if self.environment_contract is None:
            self.environment_identity = "environment-empty"
            self.environment_record_json = canonical_json(
                {
                    "schema": 1,
                    "contract_kind": "execution-environment-binding",
                    "contract_path": None,
                    "contract": None,
                }
            )
            self.execution_environment = ExecutionEnvironment()
        else:
            binding = load_execution_environment_contract(self.environment_contract)
            self.environment_identity = binding.environment_id
            self.environment_record_json = binding.record_json
            self.execution_environment = binding.environment
        self.store = AgenticRunStore(
            read.project.context,
            read.project_id,
        )
        self.runs = RunReadInterface.from_project(read.project)
        self._active: dict[str, ManagedBackgroundProcess] = {}

    def run_target(
        self,
        *,
        plan_identity: str,
        budget: AgenticExecutionBudget,
        wait: bool,
    ) -> dict[str, Any]:
        approval = self.grant.approved_plan(plan_identity)
        approved_record = json.loads(approval.plan_record_json)
        if (
            approved_record.get("contract_kind") != "resolved-flow-plan"
            or not isinstance(approved_record.get("owner"), str)
            or not isinstance(approved_record.get("flow"), str)
            or not isinstance(approved_record.get("target"), str)
        ):
            raise ValueError("approved Target Operation Plan record is invalid")
        resolved = ProjectRunner(
            self.read.project,
            approved_record["owner"],
        ).plan(approved_record["flow"], approved_record["target"])
        if resolved.plan_identity != plan_identity:
            raise ValueError("Target Operation Plan changed after execution approval")
        required = tuple(
            sorted(
                {
                    AgenticExecutionCapability(capability)
                    for capability in resolved.execution_capabilities
                },
                key=lambda item: item.value,
            )
        )
        if resolved.node_count > budget.maximum_nodes:
            raise ValueError(
                f"Target operation Plan needs {resolved.node_count} nodes but the node budget "
                f"allows {budget.maximum_nodes}"
            )
        self.grant.authorize(
            plan_identity,
            resolved.record,
            required,
            instant=datetime.now(timezone.utc),
        )
        run_id = (
            f"target-{resolved.owner}-{resolved.target}-{resolved.operation}-"
            f"{self.grant.approval}-"
            f"{budget.maximum_seconds}-{budget.maximum_nodes}"
        )
        paths = self.store.paths(
            owner=resolved.owner,
            target=resolved.target,
            operation=resolved.operation,
            run_id=run_id,
        )
        state_path = paths.role("control") / "state.json"
        if state_path.exists():
            request = self.store.read_request(paths)
            if (
                request["grant_identity"] != self.grant.identity
                or request["plan_identity"] != plan_identity
                or request["plan_record_json"]
                != canonical_json(resolved.record)
                or request["grant_json"] != self.grant.canonical_json()
                or request["environment_record_json"] != self.environment_record_json
                or request["budget"] != asdict(budget)
            ):
                raise ValueError("idempotent Target Run identity drift")
        else:
            stored_request = self.store.read_request_if_present(paths)
            submitted_at = (
                _now() if stored_request is None else stored_request["submitted_at"]
            )
            common = {
                "schema": 1,
                "project_id": self.read.project_id,
                "owner": resolved.owner,
                "target": resolved.target,
                "operation": resolved.operation,
                "plan_identity": plan_identity,
                "plan_record_json": canonical_json(resolved.record),
                "run_id": run_id,
                "grant_identity": self.grant.identity,
                "grant_json": self.grant.canonical_json(),
                "required_capabilities": [item.value for item in required],
                "budget": asdict(budget),
                "submitted_at": submitted_at,
                "total_nodes": resolved.node_count,
            }
            request = {
                **common,
                "contract_kind": AGENTIC_RUN_REQUEST_KIND,
                "principal": self.grant.principal,
                "role": self.grant.role,
                "approval": self.grant.approval,
                "environment_identity": self.environment_identity,
                "environment_record_json": self.environment_record_json,
            }
            state = {
                **common,
                "contract_kind": AGENTIC_RUN_STATE_KIND,
                "status": "queued",
                "started_at": None,
                "finished_at": None,
                "completed_nodes": 0,
                "current_node": None,
                "error_code": None,
            }
            self.store.create(paths, request=request, state=state)
            try:
                arguments = [
                    "--project-root",
                    str(self.read.project.project_root),
                    "--owner",
                    resolved.owner,
                    "--target",
                    resolved.target,
                    "--operation",
                    resolved.operation,
                    "--run-id",
                    run_id,
                ]
                if self.environment_contract is not None:
                    assert self.environment_identity is not None
                    arguments.extend(
                        (
                            "--environment",
                            str(self.environment_contract),
                            "--environment-identity",
                            self.environment_identity,
                        )
                    )
                self._active[run_id] = spawn_agentic_flow_worker(
                    arguments,
                    cwd=self.read.project.project_root,
                    env=_worker_environment(),
                )
            except BaseException:
                self._terminalize_without_worker(
                    paths,
                    status="uncertain",
                    error_code="worker-launch-failed",
                )
                raise
        if wait:
            self.wait_run(run_id)
        return self._operation_response(run_id, operation="target.run")

    def wait_until_running(self, run_id: str, *, timeout_seconds: int) -> None:
        deadline = time.monotonic() + timeout_seconds
        while True:
            located = self.store.locate(run_id)
            self._reconcile(run_id, located.paths)
            state = self.store.read_state(located.paths)
            if state["status"] == "running" and state["current_node"] is not None:
                return
            if state["status"] not in RUNNING_STATUSES:
                raise ValueError(
                    f"managed Target Run became {state['status']!r} before running"
                )
            if time.monotonic() >= deadline:
                raise ValueError("managed Target Run did not start before the timeout")
            time.sleep(0.02)

    def wait_run(self, run_id: str) -> dict[str, Any]:
        located = self.store.locate(run_id)
        state = located.state
        deadline = time.monotonic() + state["budget"]["maximum_seconds"] + 20
        while state["status"] in RUNNING_STATUSES or run_id in self._active:
            self._reconcile(run_id, located.paths)
            state = self.store.read_state(located.paths)
            if time.monotonic() >= deadline:
                raise ValueError("managed Target Run did not reach a terminal artifact")
            if state["status"] in RUNNING_STATUSES or run_id in self._active:
                time.sleep(0.02)
        return self.inspect_run(run_id=run_id)

    def inspect_run(self, *, run_id: str) -> dict[str, Any]:
        located = self.store.locate(run_id)
        self._reconcile(run_id, located.paths)
        state = self.store.read_state(located.paths)
        return self.runs.inspect(
            owner=state["owner"],
            target=state["target"],
            operation=state["operation"],
            run_id=state["run_id"],
        )

    def cancel_run(self, *, run_id: str) -> dict[str, Any]:
        located = self.store.locate(run_id)
        request = self.store.read_request(located.paths)
        if request["principal"] != self.grant.principal:
            raise ValueError("managed Target Run belongs to a different principal")
        if request["grant_identity"] != self.grant.identity:
            raise ValueError("managed Target Run belongs to a different execution grant")
        self.grant.authorize(
            request["plan_identity"],
            json.loads(request["plan_record_json"]),
            tuple(
                AgenticExecutionCapability(value)
                for value in request["required_capabilities"]
            ),
            instant=datetime.now(timezone.utc),
        )
        state = self.store.read_state(located.paths)
        if state["status"] in RUNNING_STATUSES:
            handle = self._active.get(run_id)
            if handle is None:
                raise ValueError(
                    "exact cancellation authority is unavailable for this managed run"
                )
            handle.cancel()
            self._reconcile(run_id, located.paths)
            state = self.store.read_state(located.paths)
        if state["status"] not in {"cancelled", "budget-exhausted"}:
            raise ValueError(f"managed Target Run is already {state['status']!r}")
        return self._operation_response(run_id, operation="run.cancel")

    def _operation_response(self, run_id: str, *, operation: str) -> dict[str, Any]:
        payload = self.inspect_run(run_id=run_id)
        payload = dict(payload)
        payload["operation"] = operation
        if operation == "target.run" and payload["data"]["management"]["status"] in RUNNING_STATUSES:
            payload["conclusion"] = "submitted"
            payload["summary"] = (
                f"Submitted managed Target Run {run_id}; inspect or cancel its durable identity."
            )
        elif operation == "run.cancel":
            payload["conclusion"] = payload["data"]["management"]["status"]
            payload["summary"] = (
                f"Managed Target Run {run_id} reached terminal cancellation state."
            )
        return payload

    def _reconcile(self, run_id: str, paths: Any) -> None:
        handle = self._active.get(run_id)
        if handle is None:
            return
        terminal = handle.poll()
        if terminal is None:
            return
        self._active.pop(run_id, None)
        state = self.store.read_state(paths)
        if state["status"] in RUNNING_STATUSES:
            self._terminalize_without_worker(
                paths,
                status="uncertain" if terminal.returncode == 0 else "failed",
                error_code=(
                    "worker-missing-terminal-state"
                    if terminal.returncode == 0
                    else "worker-process-failed"
                ),
            )

    def _terminalize_without_worker(
        self,
        paths: Any,
        *,
        status: str,
        error_code: str,
    ) -> None:
        state = self.store.read_state(paths)
        if state["status"] not in RUNNING_STATUSES:
            return
        terminal = {
            **state,
            "status": status,
            "finished_at": _now(),
            "current_node": None,
            "error_code": error_code,
        }
        self.store.write_state(paths, terminal)
        request = self.store.read_request(paths)
        self.store.write_audit(
            paths,
            {
                "schema": 1,
                "contract_kind": AGENTIC_RUN_AUDIT_KIND,
                "project_id": request["project_id"],
                "owner": request["owner"],
                "target": request["target"],
                "operation": request["operation"],
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
                "started_at": terminal["started_at"],
                "finished_at": terminal["finished_at"],
                "terminal_status": terminal["status"],
                "result_status": None,
                "error_code": error_code,
            },
        )


__all__ = ["AgenticExecutionBudget", "AgenticExecutionInterface"]
