"""Authorized durable execution above the single-attempt deterministic FlowEngine."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

from sigilicon.artifacts import (
    atomic_write_json,
    read_json_object,
    read_nofollow_text,
    write_immutable_text,
)
from sigilicon.canonical import canonical_sha256
from sigilicon.domain.agentic_execution import (
    AgenticExecutionBudget,
    AgenticExecutionCapability,
    AgenticExecutionGrant,
    agentic_execution_grant_from_json,
)
from sigilicon.external_tools import (
    ManagedBackgroundProcess,
    spawn_agentic_flow_worker,
)
from sigilicon.flow import load_execution_environment
from sigilicon.paths import ArtifactLayout, ProjectContext
from sigilicon.workflows.agentic_read import AgenticReadInterface
from sigilicon.workflows.design_campaign import (
    DesignCampaignRunner,
    design_campaign_result_from_json,
)
from sigilicon.workflows.agentic_runs import (
    AGENTIC_RUN_AUDIT_KIND,
    AGENTIC_RUN_REQUEST_KIND,
    AGENTIC_RUN_STATE_KIND,
    AgenticRunStore,
    RUNNING_STATUSES,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _environment_identity(path: Path | None) -> tuple[str, str | None]:
    if path is None:
        return "empty-environment", None
    resolved = Path(path).resolve()
    load_execution_environment(resolved)
    digest = hashlib.sha256(
        read_nofollow_text(resolved).encode("utf-8")
    ).hexdigest()
    return f"environment-{digest[:24]}", digest


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
        self.environment_identity, self.environment_sha256 = _environment_identity(
            self.environment_contract
        )
        self.store = AgenticRunStore(
            read.repository.project.artifact_root,
            read.project_id,
        )
        self._active: dict[str, ManagedBackgroundProcess] = {}

    @classmethod
    def from_project_root(
        cls,
        project_root: Path | str,
        *,
        grant: AgenticExecutionGrant,
        environment_contract: Path | None = None,
    ) -> "AgenticExecutionInterface":
        project = ProjectContext.from_project_root(project_root)
        return cls(
            AgenticReadInterface.from_project_context(project),
            grant=grant,
            environment_contract=environment_contract,
        )

    @classmethod
    def from_launcher_contracts(
        cls,
        project_root: Path | str,
        *,
        grant_contract: Path,
        environment_contract: Path | None = None,
    ) -> "AgenticExecutionInterface":
        grant = agentic_execution_grant_from_json(
            read_nofollow_text(Path(grant_contract).resolve())
        )
        return cls.from_project_root(
            project_root,
            grant=grant,
            environment_contract=environment_contract,
        )

    def run_flow(
        self,
        *,
        plan_identity: str,
        budget: AgenticExecutionBudget,
        wait: bool,
    ) -> dict[str, Any]:
        resolved = self.read.resolve_plan_identity(plan_identity)
        required = tuple(
            sorted(
                {
                    AgenticExecutionCapability(node.execution_capability)
                    for node in resolved.plan.nodes
                },
                key=lambda item: item.value,
            )
        )
        if len(resolved.plan.nodes) > budget.maximum_nodes:
            raise ValueError(
                f"Flow Plan needs {len(resolved.plan.nodes)} nodes but the node budget "
                f"allows {budget.maximum_nodes}"
            )
        self.grant.authorize(
            plan_identity,
            required,
            instant=datetime.now(timezone.utc),
        )
        run_id = canonical_sha256(
            {
                "operation": "flow.run",
                "project_id": self.read.project_id,
                "grant_identity": self.grant.identity,
                "plan_identity": plan_identity,
                "budget": asdict(budget),
            }
        )[:32]
        plan = resolved.plan
        paths = self.store.paths(
            owner=plan.spec.owner,
            flow=plan.spec.flow_id,
            target=plan.target.target_id,
            run_id=run_id,
        )
        state_path = paths.role("control") / "state.json"
        if state_path.exists():
            request = self.store.read_request(paths)
            if (
                request["grant_identity"] != self.grant.identity
                or request["plan_identity"] != plan_identity
                or request["budget"] != asdict(budget)
            ):
                raise ValueError("idempotent Flow Run identity drift")
        else:
            submitted_at = _now()
            common = {
                "schema": 1,
                "project_id": self.read.project_id,
                "owner": plan.spec.owner,
                "flow": plan.spec.flow_id,
                "target": plan.target.target_id,
                "profile": plan.profile.profile_id,
                "plan_identity": plan_identity,
                "run_id": run_id,
                "grant_identity": self.grant.identity,
                "required_capabilities": [item.value for item in required],
                "budget": asdict(budget),
                "submitted_at": submitted_at,
                "total_nodes": len(plan.nodes),
            }
            request = {
                **common,
                "contract_kind": AGENTIC_RUN_REQUEST_KIND,
                "principal": self.grant.principal,
                "role": self.grant.role,
                "approval": self.grant.approval,
                "environment_identity": self.environment_identity,
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
                    str(self.read.repository.project_root),
                    "--owner",
                    plan.spec.owner,
                    "--flow",
                    plan.spec.flow_id,
                    "--target",
                    plan.target.target_id,
                    "--run-id",
                    run_id,
                ]
                if self.environment_contract is not None:
                    assert self.environment_sha256 is not None
                    arguments.extend(
                        (
                            "--environment",
                            str(self.environment_contract),
                            "--environment-sha256",
                            self.environment_sha256,
                        )
                    )
                self._active[run_id] = spawn_agentic_flow_worker(
                    arguments,
                    cwd=self.read.repository.project_root,
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
        return self._operation_response(run_id, operation="flow.run")

    def run_campaign(
        self,
        *,
        campaign_json: str,
        campaign_identity: str,
    ) -> dict[str, Any]:
        """Run one exact bounded Campaign and persist its immutable typed result."""

        resolved = self.read.resolve_campaign_plan(campaign_json)
        if campaign_identity != resolved.campaign_identity:
            raise ValueError("Design Campaign identity drift")
        required = tuple(
            sorted(
                {
                    AgenticExecutionCapability(node.execution_capability)
                    for attempt in resolved.campaign.attempts
                    for node in attempt.plan.nodes
                },
                key=lambda item: item.value,
            )
        )
        self.grant.authorize(
            campaign_identity,
            required,
            instant=datetime.now(timezone.utc),
        )
        run_id = canonical_sha256(
            {
                "operation": "campaign.run",
                "project_id": self.read.project_id,
                "grant_identity": self.grant.identity,
                "campaign_identity": campaign_identity,
            }
        )[:32]
        paths = ArtifactLayout(
            self.read.repository.project.artifact_root
        ).execution(
            owner=resolved.campaign.owner,
            target=resolved.campaign.campaign_id,
            flow="design-campaign",
            variant="default",
            identity=run_id,
            artifact_kind="design-campaign",
            identity_kind="run_id",
        )
        request_path = paths.role("inputs") / "request.json"
        source_path = paths.role("inputs") / "campaign.json"
        result_path = paths.role("outputs") / "campaign_result.json"
        audit_path = paths.role("logs") / "audit.json"
        request = {
            "schema": 1,
            "contract_kind": "agentic-campaign-run-request",
            "project_id": self.read.project_id,
            "owner": resolved.campaign.owner,
            "campaign_id": resolved.campaign.campaign_id,
            "campaign_identity": campaign_identity,
            "run_id": run_id,
            "grant_identity": self.grant.identity,
            "principal": self.grant.principal,
            "role": self.grant.role,
            "approval": self.grant.approval,
            "required_capabilities": [item.value for item in required],
            "environment_identity": self.environment_identity,
        }
        if paths.root.exists():
            recorded = read_json_object(request_path, "Design Campaign request")
            if recorded != request:
                raise ValueError("idempotent Design Campaign request identity drift")
            if not result_path.exists() or not audit_path.exists():
                raise ValueError("Design Campaign run has no immutable terminal result")
            result = design_campaign_result_from_json(read_nofollow_text(result_path))
        else:
            paths.create()
            atomic_write_json(request_path, request)
            atomic_write_json(
                source_path,
                json.loads(resolved.source.canonical_json()),
            )
            environment = (
                None
                if self.environment_contract is None
                else load_execution_environment(self.environment_contract)
            )
            started_at = _now()
            result = DesignCampaignRunner(
                resolved.engine,
                artifact_root=self.read.repository.project.artifact_root,
                environment=environment,
            ).run(resolved.campaign)
            write_immutable_text(result_path, result.canonical_json())
            finished_at = _now()
            atomic_write_json(
                audit_path,
                {
                    **request,
                    "contract_kind": "agentic-campaign-run-audit",
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "terminal_status": result.termination.value,
                    "iteration_count": len(result.iterations),
                    "result_sha256": hashlib.sha256(
                        result.canonical_json().encode("utf-8")
                    ).hexdigest(),
                },
            )
            atomic_write_json(
                paths.manifest,
                {
                    "schema": 1,
                    "contract_kind": "agentic-campaign-run-manifest",
                    "owner": resolved.campaign.owner,
                    "campaign_id": resolved.campaign.campaign_id,
                    "campaign_identity": campaign_identity,
                    "run_id": run_id,
                    "members": [
                        "inputs/campaign.json",
                        "inputs/request.json",
                        "logs/audit.json",
                        "outputs/campaign_result.json",
                    ],
                },
            )
        return self.read._response(
            operation="campaign.run",
            authority="recorded-design-campaign-result",
            conclusion=result.termination.value,
            summary=(
                f"Design Campaign {resolved.campaign.campaign_id!r} reached immutable "
                f"terminal state {result.termination.value!r}; no source was promoted."
            ),
            data={
                "management": {
                    "run_id": run_id,
                    "campaign_identity": campaign_identity,
                    "grant_identity": self.grant.identity,
                    "status": result.termination.value,
                },
                "result": json.loads(result.canonical_json()),
                "model_context": {
                    "record_text_trust": "untrusted",
                    "qualification_authority": "typed-evidence-only",
                },
            },
            resources=[
                self.read.project_resource_uri,
                self.read.owner_resource_uri(resolved.campaign.owner),
            ],
            allowed_next_actions=["review-campaign", "candidate.promotion_plan"],
        )

    def wait_until_running(self, run_id: str, *, timeout_seconds: int) -> None:
        deadline = time.monotonic() + timeout_seconds
        while True:
            located = self.store.locate(run_id)
            self._reconcile(run_id, located.paths)
            state = self.store.read_state(located.paths)
            if state["status"] == "running":
                return
            if state["status"] not in RUNNING_STATUSES:
                raise ValueError(
                    f"managed Flow Run became {state['status']!r} before running"
                )
            if time.monotonic() >= deadline:
                raise ValueError("managed Flow Run did not start before the timeout")
            time.sleep(0.02)

    def wait_run(self, run_id: str) -> dict[str, Any]:
        located = self.store.locate(run_id)
        state = located.state
        deadline = time.monotonic() + state["budget"]["maximum_seconds"] + 20
        while state["status"] in RUNNING_STATUSES:
            self._reconcile(run_id, located.paths)
            state = self.store.read_state(located.paths)
            if time.monotonic() >= deadline:
                raise ValueError("managed Flow Run did not reach a terminal artifact")
            if state["status"] in RUNNING_STATUSES:
                time.sleep(0.02)
        return self.inspect_run(run_id=run_id)

    def inspect_run(self, *, run_id: str) -> dict[str, Any]:
        located = self.store.locate(run_id)
        self._reconcile(run_id, located.paths)
        state = self.store.read_state(located.paths)
        return self.read.inspect_run(
            owner=state["owner"],
            flow=state["flow"],
            target=state["target"],
            run_id=state["run_id"],
        )

    def cancel_run(self, *, run_id: str) -> dict[str, Any]:
        located = self.store.locate(run_id)
        request = self.store.read_request(located.paths)
        if request["principal"] != self.grant.principal:
            raise ValueError("managed Flow Run belongs to a different principal")
        if request["grant_identity"] != self.grant.identity:
            raise ValueError("managed Flow Run belongs to a different execution grant")
        self.grant.authorize(
            request["plan_identity"],
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
            raise ValueError(f"managed Flow Run is already {state['status']!r}")
        return self._operation_response(run_id, operation="run.cancel")

    def _operation_response(self, run_id: str, *, operation: str) -> dict[str, Any]:
        payload = self.inspect_run(run_id=run_id)
        payload = dict(payload)
        payload["operation"] = operation
        if operation == "flow.run" and payload["data"]["management"]["status"] in RUNNING_STATUSES:
            payload["conclusion"] = "submitted"
            payload["summary"] = (
                f"Submitted managed Flow Run {run_id}; inspect or cancel its durable identity."
            )
        elif operation == "run.cancel":
            payload["conclusion"] = payload["data"]["management"]["status"]
            payload["summary"] = (
                f"Managed Flow Run {run_id} reached terminal cancellation state."
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
                "started_at": terminal["started_at"],
                "finished_at": terminal["finished_at"],
                "terminal_status": terminal["status"],
                "flow_status": None,
                "error_code": error_code,
            },
        )


__all__ = ["AgenticExecutionBudget", "AgenticExecutionInterface"]
