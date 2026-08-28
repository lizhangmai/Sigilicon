"""Immutable authorization and budget values for managed agentic execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import re

from sigilicon.canonical import (
    canonical_from_exact_json,
    canonical_json,
    canonical_sha256,
)


AGENTIC_EXECUTION_SCHEMA = 1
AGENTIC_EXECUTION_GRANT_KIND = "agentic.execution-grant.v1"
_IDENTITY = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class AgenticExecutionCapability(str, Enum):
    EXECUTE_DERIVED = "execute-derived"
    MUTATE_WORKSPACE = "mutate-workspace"


def _identity(value: str, label: str) -> None:
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        raise ValueError(f"invalid {label}: {value!r}")


def _sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be a SHA-256 identity")


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
class AgenticExecutionGrant:
    """One launcher-bound, time-bounded human approval for exact Flow plans."""

    principal: str
    role: str
    capabilities: tuple[AgenticExecutionCapability, ...]
    approved_plan_sha256: tuple[str, ...]
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
        _identity(self.principal, "execution principal")
        _identity(self.role, "execution role")
        _identity(self.approval, "approval identity")
        if not self.capabilities:
            raise ValueError("execution grant must contain at least one capability")
        if any(not isinstance(item, AgenticExecutionCapability) for item in self.capabilities):
            raise ValueError("execution grant capabilities must be typed")
        capability_values = tuple(item.value for item in self.capabilities)
        if capability_values != tuple(sorted(set(capability_values))):
            raise ValueError("execution grant capabilities must be unique and sorted")
        if not self.approved_plan_sha256:
            raise ValueError("execution grant must approve at least one plan")
        for identity in self.approved_plan_sha256:
            _sha256(identity, "approved plan")
        if self.approved_plan_sha256 != tuple(sorted(set(self.approved_plan_sha256))):
            raise ValueError("approved plan identities must be unique and sorted")
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
        return canonical_sha256(self)

    def canonical_json(self) -> str:
        return canonical_json(self)

    def valid_at(self, instant: datetime) -> bool:
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("authorization time must include a timezone")
        return instant <= datetime.fromisoformat(self.expires_at)

    def authorize(
        self,
        plan_identity: str,
        required: tuple[AgenticExecutionCapability, ...],
        *,
        instant: datetime,
    ) -> None:
        _sha256(plan_identity, "Flow Plan")
        if not self.valid_at(instant):
            raise ValueError("execution grant has expired")
        if plan_identity not in self.approved_plan_sha256:
            raise ValueError("Flow Plan identity is not approved by the execution grant")
        missing = tuple(item for item in required if item not in self.capabilities)
        if missing:
            raise ValueError(
                "execution grant lacks required capabilities: "
                + ", ".join(item.value for item in missing)
            )


def agentic_execution_grant_from_json(text: str) -> AgenticExecutionGrant:
    return canonical_from_exact_json(text, AgenticExecutionGrant)


__all__ = [
    "AGENTIC_EXECUTION_GRANT_KIND",
    "AGENTIC_EXECUTION_SCHEMA",
    "AgenticExecutionBudget",
    "AgenticExecutionCapability",
    "AgenticExecutionGrant",
    "agentic_execution_grant_from_json",
]
