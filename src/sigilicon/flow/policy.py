"""Compilation and evaluation of typed Action facts against one policy."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from sigilicon.flow.evidence import FactKind, FactSchema, FactSet
from sigilicon.flow.errors import FactContractError, FlowExecutionError
from sigilicon.flow.model import (
    BoundPolicyCheck,
    BoundPolicySpec,
    PolicySpec,
)


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


def bind_policy(policy: PolicySpec, schema: FactSchema) -> BoundPolicySpec:
    """Bind raw recipe policy checks to one concrete Action fact schema."""

    if not isinstance(policy, PolicySpec):
        raise FactContractError("policy must be a PolicySpec")
    if not isinstance(schema, FactSchema):
        raise FactContractError("policy schema must be a FactSchema")
    checks: list[BoundPolicyCheck] = []
    for check in policy.checks:
        try:
            fact = schema.field(check.fact)
        except FactContractError as exc:
            raise FactContractError(
                f"policy {policy.policy_id!r} references unknown fact "
                f"{check.fact!r} in Action {schema.action_kind!r}"
            ) from exc
        expected = check.expected
        if check.operator in {"at_least", "at_most"}:
            if fact.kind not in {FactKind.INTEGER, FactKind.REAL}:
                raise FactContractError(
                    f"policy check {check.check_id!r} uses {check.operator} "
                    f"with non-numeric fact {fact.name!r}"
                )
            if type(expected) not in {int, float} or isinstance(expected, bool):
                raise FactContractError(
                    f"policy check {check.check_id!r} expects a numeric value"
                )
            if isinstance(expected, float) and not math.isfinite(expected):
                raise FactContractError(
                    f"policy check {check.check_id!r} expects a finite value"
                )
            if fact.kind is FactKind.INTEGER and type(expected) is not int:
                raise FactContractError(
                    f"policy check {check.check_id!r} expects an integer threshold"
                )
            typed_expected = expected
        elif check.operator == "equals":
            if expected is None:
                raise FactContractError(
                    f"policy check {check.check_id!r} requires an expected value"
                )
            typed_expected = fact.validate(
                expected,
                f"policy check {check.check_id!r} expectation",
                contract=True,
            )
        else:
            typed_expected = None
        checks.append(
            BoundPolicyCheck(
                check_id=check.check_id,
                fact=fact,
                operator=check.operator,
                expected=typed_expected,
            )
        )
    return BoundPolicySpec(policy.policy_id, schema, tuple(checks))


def evaluate_policy(
    policy: BoundPolicySpec | None,
    facts: FactSet,
) -> EvaluatedPolicy:
    if policy is None:
        return EvaluatedPolicy(None, "accepted", ())
    if not isinstance(policy, BoundPolicySpec):
        raise FlowExecutionError("policy must be bound to an Action fact schema")
    if not isinstance(facts, FactSet):
        raise FlowExecutionError("policy facts must be a FactSet")
    if facts.schema != policy.schema:
        raise FlowExecutionError("policy facts schema does not match the bound policy")
    checks: list[EvaluatedCheck] = []
    for check in policy.checks:
        present = check.fact.name in facts
        actual = facts.get(check.fact.name)
        if check.operator == "exists":
            passed = present
        elif check.operator == "equals":
            passed = present and actual == check.expected
        elif check.operator == "at_least":
            passed = present and actual >= check.expected
        else:
            passed = present and actual <= check.expected
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
