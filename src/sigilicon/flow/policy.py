"""Pure evaluation of portable Action Facts against one PolicySpec."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from sigilicon.flow.model import PolicySpec


@dataclass(frozen=True)
class EvaluatedCheck:
    check_id: str
    status: str
    actual: Any
    expected: Any


@dataclass(frozen=True)
class EvaluatedPolicy:
    policy_id: str | None
    status: str
    checks: tuple[EvaluatedCheck, ...]


def evaluate_policy(
    policy: PolicySpec | None,
    facts: Mapping[str, Any],
) -> EvaluatedPolicy:
    if policy is None:
        return EvaluatedPolicy(None, "accepted", ())
    checks: list[EvaluatedCheck] = []
    for check in policy.checks:
        present = check.fact in facts
        actual = facts.get(check.fact)
        try:
            if check.operator == "exists":
                passed = present
            elif check.operator == "equals":
                passed = present and actual == check.expected
            elif check.operator == "at_least":
                passed = present and actual >= check.expected
            else:
                passed = present and actual <= check.expected
        except TypeError:
            passed = False
        checks.append(
            EvaluatedCheck(
                check_id=check.check_id,
                status="accepted" if passed else "rejected",
                actual=actual,
                expected=check.expected,
            )
        )
    status = (
        "accepted"
        if all(check.status == "accepted" for check in checks)
        else "rejected"
    )
    return EvaluatedPolicy(policy.policy_id, status, tuple(checks))
