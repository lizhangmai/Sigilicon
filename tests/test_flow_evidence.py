from __future__ import annotations

import copy
import math

import pytest

from sigilicon.flow import (
    FactContractError,
    FactKind,
    FactSchema,
    FactSet,
    FactSource,
    FactSpec,
    FactValueError,
    PolicyCheck,
    PolicySpec,
    bind_policy,
)


def schema() -> FactSchema:
    return FactSchema(
        "test.observe",
        (
            FactSpec("passed", FactKind.BOOLEAN),
            FactSpec("count", FactKind.INTEGER),
            FactSpec("slack", FactKind.REAL, unit="ns"),
            FactSpec("status", FactKind.TEXT, enum_values=("clean", "failed")),
            FactSpec("notes", FactKind.TEXT_LIST, required=False),
        ),
    )


def source() -> FactSource:
    return FactSource("test.observe", "node")


def test_fact_set_projects_and_round_trips_strictly() -> None:
    facts = FactSet(
        schema(),
        {
            "passed": True,
            "count": 2,
            "slack": 0.25,
            "status": "clean",
            "notes": ["report", "parsed"],
        },
        source(),
    )

    assert facts["passed"] is True
    assert facts["notes"] == ("report", "parsed")
    assert FactSet.from_json(facts.to_json(), schema()) == facts


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("passed", 1, "boolean"),
        ("count", True, "integer"),
        ("slack", math.inf, "finite real"),
        ("status", "unknown", "one of"),
        ("notes", [1], "text list"),
    ],
)
def test_fact_set_rejects_wrong_types_and_values(
    name: str,
    value: object,
    message: str,
) -> None:
    values: dict[str, object] = {
        "passed": True,
        "count": 2,
        "slack": 0.25,
        "status": "clean",
    }
    values[name] = value
    with pytest.raises(FactValueError, match=message):
        FactSet(schema(), values, source())


def test_fact_set_rejects_missing_unknown_and_source_drift() -> None:
    values: dict[str, object] = {
        "passed": True,
        "count": 2,
        "slack": 0.25,
        "status": "clean",
    }
    with pytest.raises(FactValueError, match="required"):
        FactSet(schema(), {"passed": True}, source())
    unknown = dict(values)
    unknown["extra"] = True
    with pytest.raises(FactValueError, match="undeclared"):
        FactSet(schema(), unknown, source())
    with pytest.raises(FactValueError, match="source action"):
        FactSet(schema(), values, FactSource("test.other", "node"))


def test_fact_set_json_tampering_is_rejected() -> None:
    facts = FactSet(
        schema(),
        {"passed": True, "count": 2, "slack": 0.25, "status": "clean"},
        source(),
    )
    payload = facts.to_json()

    tampered_schema = copy.deepcopy(payload)
    tampered_schema["schema"]["fields"][1]["kind"] = "real"  # type: ignore[index]
    with pytest.raises(FactValueError, match="schema drift"):
        FactSet.from_json(tampered_schema, schema())

    tampered_source = copy.deepcopy(payload)
    tampered_source["source"]["node_id"] = "other"  # type: ignore[index]
    with pytest.raises(FactValueError, match="source drift"):
        FactSet.from_json(tampered_source, schema(), source=source())

    tampered_values = copy.deepcopy(payload)
    tampered_values["values"]["count"] = True  # type: ignore[index]
    with pytest.raises(FactValueError, match="integer"):
        FactSet.from_json(tampered_values, schema())


def test_fact_schema_rejects_invalid_units_and_enums() -> None:
    with pytest.raises(FactContractError, match="numeric"):
        FactSpec("status", FactKind.TEXT, unit="mode")
    with pytest.raises(FactContractError, match="enum"):
        FactSpec("count", FactKind.INTEGER, enum_values=("one",))


def test_policy_binds_fact_and_expected_types_before_execution() -> None:
    policy = PolicySpec(
        "quality",
        (
            PolicyCheck("passed", "passed", "equals", True),
            PolicyCheck("minimum", "count", "at_least", 1),
        ),
    )
    bound = bind_policy(policy, schema())
    assert bound.schema == schema()
    assert bound.checks[0].fact.name == "passed"

    with pytest.raises(FactContractError, match="unknown fact"):
        bind_policy(
            PolicySpec("bad", (PolicyCheck("x", "missing", "exists"),)),
            schema(),
        )
    with pytest.raises(FactContractError, match="non-numeric"):
        bind_policy(
            PolicySpec("bad", (PolicyCheck("x", "status", "at_least", 1),)),
            schema(),
        )
    with pytest.raises(FactContractError, match="expects an integer"):
        bind_policy(
            PolicySpec("bad", (PolicyCheck("x", "count", "at_least", 1.0),)),
            schema(),
        )
