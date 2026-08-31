"""Agent-facing facade for bounded, durable Design Campaigns.

The single-attempt agentic interfaces own one target plan and run. This module
owns bounded multi-round campaigns and their durable proposal/resume state.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from sigilicon.canonical import canonical_json
from sigilicon.domain.agentic_execution import (
    AgenticExecutionCapability,
    AgenticExecutionGrant,
)
from sigilicon.campaigns.store import (
    CAMPAIGN_REQUEST_KIND,
    DesignCampaignStore,
)
from sigilicon.campaigns.design import (
    DesignCampaign,
    DesignCampaignPhase,
    DesignCampaignResult,
    DesignCampaignRunner,
    ProjectDesignCampaignPlan,
    resolve_project_design_campaign,
)
from sigilicon.campaigns.repair import design_repair_proposal_from_json
from sigilicon.workflows.agentic_execution import AgenticExecutionInterface
from sigilicon.workflows.agentic_read import AgenticReadInterface


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DesignCampaignReadInterface(AgenticReadInterface):
    """Read facade for compiling one bounded Design Campaign."""

    def plan_campaign(self, *, campaign_json: str) -> dict[str, Any]:
        resolved = resolve_project_design_campaign(self.project, campaign_json)
        campaign = resolved.campaign
        return self.response(
            operation="campaign.plan",
            authority="plan",
            conclusion="planned",
            summary=(
                f"Compiled bounded Design Campaign {campaign.campaign_id!r} with "
                "one explicit baseline attempt; no backend was executed."
            ),
            data={
                "campaign_identity": resolved.identity,
                "plan": resolved.record,
            },
            resources=[
                self.project_resource_uri,
                self.owner_resource_uri(campaign.owner),
            ],
            allowed_next_actions=["review-campaign", "campaign.run"],
        )


class DesignCampaignExecutionInterface(AgenticExecutionInterface):
    """Authorized facade for durable Campaign start/resume."""

    def __init__(
        self,
        read: AgenticReadInterface,
        *,
        grant: AgenticExecutionGrant,
        environment_contract: Path | None = None,
    ) -> None:
        super().__init__(
            read,
            grant=grant,
            environment_contract=environment_contract,
        )
        self.campaign_store = DesignCampaignStore(
            read.project.artifact_root,
            read.project_id,
        )

    def run_campaign(
        self,
        *,
        campaign_json: str | None = None,
        campaign_identity: str | None = None,
        run_id: str | None = None,
        proposal_json: str | None = None,
    ) -> dict[str, Any]:
        """Start or resume one durable Design Campaign."""

        starting = run_id is None and proposal_json is None
        resuming = (
            run_id is not None
            and proposal_json is not None
            and campaign_json is None
            and campaign_identity is None
        )
        if not (starting or resuming):
            raise ValueError(
                "campaign.run requires either Campaign input or run/proposal input"
            )
        if starting:
            if campaign_json is None or campaign_identity is None:
                raise ValueError(
                    "campaign.run start requires Campaign JSON and identity"
                )
            resolved = resolve_project_design_campaign(
                self.read.project,
                campaign_json,
            )
            if campaign_identity != resolved.identity:
                raise ValueError("Design Campaign identity drift")
            required = self._campaign_capabilities(resolved.campaign)
            self.grant.authorize(
                campaign_identity,
                resolved.record,
                required,
                instant=datetime.now(timezone.utc),
            )
            selected_run_id = (
                f"campaign-{resolved.campaign.owner}-{resolved.campaign.campaign_id}-"
                f"{self.grant.approval}-{self.environment_identity}"
            )
            paths = self.campaign_store.paths(
                owner=resolved.campaign.owner,
                campaign_id=resolved.campaign.campaign_id,
                run_id=selected_run_id,
            )
            stored_request = (
                self.campaign_store.read_request_if_present(paths)
                if paths.root.exists()
                else None
            )
            request = {
                "schema": 1,
                "contract_kind": CAMPAIGN_REQUEST_KIND,
                "project_id": self.read.project_id,
                "owner": resolved.campaign.owner,
                "campaign_id": resolved.campaign.campaign_id,
                "campaign_identity": campaign_identity,
                "run_id": selected_run_id,
                "grant_identity": self.grant.identity,
                "grant_json": self.grant.canonical_json(),
                "principal": self.grant.principal,
                "role": self.grant.role,
                "approval": self.grant.approval,
                "required_capabilities": [item.value for item in required],
                "environment_identity": self.environment_identity,
                "environment_record_json": self.environment_record_json,
                "campaign_plan_record_json": canonical_json(resolved.record),
                "submitted_at": (
                    _now()
                    if stored_request is None
                    else stored_request["submitted_at"]
                ),
            }
            self.campaign_store.create(
                paths,
                request=request,
                campaign_json=campaign_json,
            )
            located = self.campaign_store.read(paths)
            if located.request != request or located.campaign_json != campaign_json:
                raise ValueError("idempotent Design Campaign request record drift")
            state = located.state
            if state is None:
                state = self._start_campaign_transition(paths, resolved)
        else:
            assert run_id is not None and proposal_json is not None
            located = self.campaign_store.locate(run_id)
            if (
                located.request["principal"] != self.grant.principal
                or located.request["grant_identity"] != self.grant.identity
                or located.request["grant_json"] != self.grant.canonical_json()
                or located.request["role"] != self.grant.role
                or located.request["project_id"] != self.read.project_id
                or located.request["environment_identity"] != self.environment_identity
                or located.request["environment_record_json"]
                != self.environment_record_json
            ):
                raise ValueError("Design Campaign Run belongs to a different grant")
            resolved = resolve_project_design_campaign(
                self.read.project,
                located.campaign_json,
            )
            if (
                located.request["campaign_identity"] != resolved.identity
                or located.request["campaign_plan_record_json"]
                != canonical_json(resolved.record)
            ):
                raise ValueError("Design Campaign persisted plan record drift")
            required = self._campaign_capabilities(resolved.campaign)
            self.grant.authorize(
                resolved.identity,
                resolved.record,
                required,
                instant=datetime.now(timezone.utc),
            )
            if located.state is None:
                raise ValueError("Design Campaign has no recoverable baseline state")
            paths = located.paths
            selected_run_id = run_id
            state = self._resume_campaign_transition(
                paths,
                resolved,
                proposal_json,
            )
        if state.phase is DesignCampaignPhase.COMPLETED:
            self.campaign_store.finalize(paths, state)
        result = DesignCampaignResult(
            state.owner,
            state.campaign_id,
            state.campaign_identity,
            state.termination,
            state.iterations,
            None if not state.iterations else state.iterations[-1].quality,
            None if not state.iterations else state.iterations[-1].decision,
            state.message,
        )
        return self.read.response(
            operation="campaign.run",
            authority="recorded-design-campaign-state",
            conclusion=(
                state.phase.value
                if state.phase is DesignCampaignPhase.PROPOSAL_REQUIRED
                else state.termination.value
            ),
            summary=(
                f"Design Campaign {resolved.campaign.campaign_id!r} reached durable "
                f"state {state.phase.value!r}/{state.termination.value!r}; "
                "no source was promoted."
            ),
            data={
                "management": {
                    "run_id": selected_run_id,
                    "campaign_identity": resolved.identity,
                    "grant_identity": self.grant.identity,
                    "status": (
                        state.phase.value
                        if state.phase is DesignCampaignPhase.PROPOSAL_REQUIRED
                        else state.termination.value
                    ),
                },
                "state": json.loads(state.canonical_json()),
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
            allowed_next_actions=(
                ["campaign.run"]
                if state.phase is DesignCampaignPhase.PROPOSAL_REQUIRED
                else ["review-campaign", "candidate.promotion_plan"]
            ),
        )

    @staticmethod
    def _campaign_capabilities(
        campaign: DesignCampaign,
    ) -> tuple[AgenticExecutionCapability, ...]:
        plans = [campaign.baseline.plan]
        if campaign.continuation is not None:
            plans.append(campaign.continuation.plan)
        return tuple(
            sorted(
                {
                    AgenticExecutionCapability(node.execution_capability)
                    for plan in plans
                    for node in plan.nodes
                },
                key=lambda item: item.value,
            )
        )

    def _campaign_runner(
        self,
        resolved: ProjectDesignCampaignPlan,
    ) -> DesignCampaignRunner:
        return resolved.runner(
            environment=self.execution_environment,
            execution_context_identity=(
                f"{self.grant.approval}-{self.environment_identity}"
            ),
        )

    def _start_campaign_transition(
        self,
        paths: Any,
        resolved: ProjectDesignCampaignPlan,
    ):
        with self.campaign_store.exclusive(paths):
            current = self.campaign_store.read(paths)
            if current.state is not None:
                return current.state
            state = self._campaign_runner(resolved).start(
                resolved.campaign,
                elapsed_seconds=self._campaign_elapsed_seconds(current.request),
            )
            self.campaign_store.append(
                paths,
                state,
                operation="start",
                recorded_at=_now(),
                proposal_identity=None,
            )
            return state

    def _resume_campaign_transition(
        self,
        paths: Any,
        resolved: ProjectDesignCampaignPlan,
        proposal_json: str,
    ):
        proposal = design_repair_proposal_from_json(proposal_json)
        with self.campaign_store.exclusive(paths):
            current = self.campaign_store.read(paths)
            if current.state is None:
                raise ValueError("Design Campaign has no recoverable baseline state")
            if current.state.last_proposal_identity == proposal.identity:
                if current.last_proposal_json != proposal_json:
                    raise ValueError(
                        "same Design Repair Proposal ID has different typed record"
                    )
                return current.state
            state = self._campaign_runner(resolved).resume(
                resolved.campaign,
                current.state,
                proposal,
                elapsed_seconds=self._campaign_elapsed_seconds(current.request),
            )
            self.campaign_store.append(
                paths,
                state,
                operation="resume",
                recorded_at=_now(),
                proposal_identity=proposal.identity,
                proposal_json=proposal_json,
            )
            return state

    @staticmethod
    def _campaign_elapsed_seconds(request: dict[str, Any]) -> int:
        try:
            submitted = datetime.fromisoformat(request["submitted_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Design Campaign submission time is invalid") from exc
        if submitted.tzinfo is None or submitted.utcoffset() is None:
            raise ValueError("Design Campaign submission time lacks a timezone")
        return max(
            0,
            int((datetime.now(timezone.utc) - submitted).total_seconds()),
        )


__all__ = [
    "DesignCampaignExecutionInterface",
    "DesignCampaignReadInterface",
]
