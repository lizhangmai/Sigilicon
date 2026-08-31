"""Immutable authorization and budget values for managed agentic execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import json
import re

from sigilicon.canonical import (
    canonical_digest,
    canonical_from_exact_json,
    canonical_json,
)
from sigilicon.identifiers import bounded_identity


AGENTIC_EXECUTION_SCHEMA = 1
AGENTIC_EXECUTION_GRANT_KIND = "agentic.execution-grant.v1"
_SEMANTIC_NAME = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*\Z")


class AgenticExecutionCapability(str, Enum):
    EXECUTE_DERIVED = "execute-derived"
    MUTATE_WORKSPACE = "mutate-workspace"


def _semantic_name(value: str, label: str) -> None:
    if not isinstance(value, str) or _SEMANTIC_NAME.fullmatch(value) is None:
        raise ValueError(f"invalid {label}: {value!r}")


@dataclass(frozen=True)
class AgenticExecutionBudget:
    maximum_seconds: int
    maximum_nodes: int

    def __post_init__(self) -> None:
        if type(self.maximum_seconds) is not int or not 1 <= self.maximum_seconds <= 86_400:
            raise ValueError("execution time budget must be between 1 and 86400 seconds")
        if type(self.maximum_nodes) is not int or not 1 <= self.maximum_nodes <= 10_000:
            raise ValueError("execution node budget must be between 1 and 10000 nodes")


@dataclass(frozen=True)
class AgenticPlanApproval:
    """One immutable plan digest bound to its exact canonical plan record."""

    plan_identity: str
    plan_record_json: str

    def __post_init__(self) -> None:
        bounded_identity(self.plan_identity, "approved plan")
        try:
            record = json.loads(self.plan_record_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("approved plan record must be canonical JSON") from exc
        if not isinstance(record, dict) or canonical_json(record) != self.plan_record_json:
            raise ValueError("approved plan record must be an exact canonical JSON object")
        if self.plan_identity != canonical_digest(record):
            raise ValueError("approved plan identity must equal its canonical record digest")


@dataclass(frozen=True)
class AgenticExecutionGrant:
    """One launcher-bound, time-bounded human approval for exact Flow plans."""

    principal: str
    role: str
    capabilities: tuple[AgenticExecutionCapability, ...]
    approved_plans: tuple[AgenticPlanApproval, ...]
    approval: str
    expires_at: str
    schema: int = AGENTIC_EXECUTION_SCHEMA
    contract_kind: str = AGENTIC_EXECUTION_GRANT_KIND

    def __post_init__(self) -> None:
        if type(self.schema) is not int or self.schema != AGENTIC_EXECUTION_SCHEMA:
            raise ValueError(f"execution grant schema must be {AGENTIC_EXECUTION_SCHEMA}")
        if self.contract_kind != AGENTIC_EXECUTION_GRANT_KIND:
            raise ValueError(
                f"execution grant kind must be {AGENTIC_EXECUTION_GRANT_KIND!r}"
            )
        _semantic_name(self.principal, "execution principal")
        _semantic_name(self.role, "execution role")
        _semantic_name(self.approval, "approval identity")
        if not self.capabilities:
            raise ValueError("execution grant must contain at least one capability")
        if any(not isinstance(item, AgenticExecutionCapability) for item in self.capabilities):
            raise ValueError("execution grant capabilities must be typed")
        capability_values = tuple(item.value for item in self.capabilities)
        if capability_values != tuple(sorted(set(capability_values))):
            raise ValueError("execution grant capabilities must be unique and sorted")
        if not self.approved_plans:
            raise ValueError("execution grant must approve at least one plan")
        if any(not isinstance(item, AgenticPlanApproval) for item in self.approved_plans):
            raise ValueError("execution grant approved plans must be typed")
        plan_identities = tuple(item.plan_identity for item in self.approved_plans)
        if plan_identities != tuple(sorted(set(plan_identities))):
            raise ValueError("approved plans must have unique sorted identities")
        try:
            expiry = datetime.fromisoformat(self.expires_at)
        except ValueError as exc:
            raise ValueError("execution grant expiry must be an ISO-8601 timestamp") from exc
        if expiry.tzinfo is None or expiry.utcoffset() is None:
            raise ValueError("execution grant expiry must include a timezone")
        if expiry.isoformat() != self.expires_at:
            raise ValueError("execution grant expiry must use canonical ISO-8601 text")

    @property
    def identity(self) -> str:
        return self.approval

    def canonical_json(self) -> str:
        return canonical_json(self)

    def valid_at(self, instant: datetime) -> bool:
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("authorization time must include a timezone")
        return instant <= datetime.fromisoformat(self.expires_at)

    def authorize(
        self,
        plan_identity: str,
        plan_record: object,
        required: tuple[AgenticExecutionCapability, ...],
        *,
        instant: datetime,
    ) -> None:
        bounded_identity(plan_identity, "Target Operation Plan")
        if not self.valid_at(instant):
            raise ValueError("execution grant has expired")
        approval = self.approved_plan(plan_identity)
        if canonical_json(plan_record) != approval.plan_record_json:
            raise ValueError("Target Operation Plan record changed after execution approval")
        missing = tuple(item for item in required if item not in self.capabilities)
        if missing:
            raise ValueError(
                "execution grant lacks required capabilities: "
                + ", ".join(item.value for item in missing)
            )

    def approved_plan(self, plan_identity: str) -> AgenticPlanApproval:
        """Return the exact approved record selected by an immutable digest."""

        bounded_identity(plan_identity, "Target Operation Plan")
        approval = next(
            (
                item
                for item in self.approved_plans
                if item.plan_identity == plan_identity
            ),
            None,
        )
        if approval is None:
            raise ValueError(
                "Target Operation Plan identity is not approved by the execution grant"
            )
        return approval


def agentic_execution_grant_from_json(text: str) -> AgenticExecutionGrant:
    return canonical_from_exact_json(text, AgenticExecutionGrant)


__all__ = [
    "AGENTIC_EXECUTION_GRANT_KIND",
    "AGENTIC_EXECUTION_SCHEMA",
    "AgenticExecutionBudget",
    "AgenticExecutionCapability",
    "AgenticExecutionGrant",
    "AgenticPlanApproval",
    "agentic_execution_grant_from_json",
]
