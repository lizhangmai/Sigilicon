from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from sigilicon.cli.agentic_execute import main as agentic_execute_cli_main
from sigilicon.domain.agentic_execution import (
    AGENTIC_EXECUTION_GRANT_KIND,
    AgenticExecutionBudget,
    AgenticExecutionCapability,
    AgenticExecutionGrant,
    agentic_execution_grant_from_json,
)
from sigilicon.workflows.agentic_execution import AgenticExecutionInterface
from sigilicon.workflows.agentic_read import AgenticReadInterface
from sigilicon.workflows.agentic_runs import AgenticRunStore

from test_agentic_read_interface import write_read_only_flow_project


def _plan_identity(root: Path) -> str:
    return AgenticReadInterface.from_project_root(root).plan_flow(
        owner="example",
        flow="pipeline",
        target="all",
        profile="offline",
    )["data"]["plan_identity"]


def _grant(plan_identity: str, *, approval: str = "phase3-test-approval") -> AgenticExecutionGrant:
    return AgenticExecutionGrant(
        principal="test-operator",
        role="design-operator",
        capabilities=(AgenticExecutionCapability.EXECUTE_DERIVED,),
        approved_plan_sha256=(plan_identity,),
        approval=approval,
        expires_at="2099-01-01T00:00:00+00:00",
    )


def _write_wait_flow(root: Path) -> None:
    write_read_only_flow_project(root)
    flow_root = root / "ip/example/configs/flows"
    (flow_root / "pipeline.toml").write_text(
        '''schema = 1
contract_kind = "flow"
path_scope = "owner"
owner = "example"
name = "pipeline"

[[nodes]]
id = "wait"
action = "fake.wait"
config = { seconds = 10 }

[[targets]]
name = "all"
goals = ["wait"]
''',
        encoding="utf-8",
    )
    (flow_root / "profiles/offline.toml").write_text(
        '''schema = 1
contract_kind = "execution-profile"
path_scope = "owner"
owner = "example"
name = "offline"

[actions."fake.wait"]
adapter = "fake-wait"
''',
        encoding="utf-8",
    )


def test_execution_grant_is_canonical_strict_and_time_bounded(tmp_path: Path) -> None:
    write_read_only_flow_project(tmp_path)
    plan_identity = _plan_identity(tmp_path)
    grant = _grant(plan_identity)

    assert grant.contract_kind == AGENTIC_EXECUTION_GRANT_KIND
    assert agentic_execution_grant_from_json(grant.canonical_json()) == grant
    assert grant.identity == agentic_execution_grant_from_json(
        grant.canonical_json()
    ).identity

    payload = json.loads(grant.canonical_json())
    payload["generic_command"] = "touch owned"
    with pytest.raises(ValueError, match="unknown"):
        agentic_execution_grant_from_json(
            json.dumps(payload, indent=2, sort_keys=True) + "\n"
        )

    expired = AgenticExecutionGrant(
        principal="test-operator",
        role="design-operator",
        capabilities=(AgenticExecutionCapability.EXECUTE_DERIVED,),
        approved_plan_sha256=(plan_identity,),
        approval="expired-test-approval",
        expires_at="2020-01-01T00:00:00+00:00",
    )
    assert not expired.valid_at(datetime.now(timezone.utc))


def test_python_and_cli_execute_the_same_durable_plan(tmp_path: Path, capsys) -> None:
    write_read_only_flow_project(tmp_path)
    plan_identity = _plan_identity(tmp_path)
    grant = _grant(plan_identity)
    grant_path = tmp_path / "grant.json"
    grant_path.write_text(grant.canonical_json(), encoding="utf-8")
    budget = AgenticExecutionBudget(maximum_seconds=30, maximum_nodes=1)

    interface = AgenticExecutionInterface.from_project_root(
        tmp_path,
        grant=grant,
    )
    result = interface.run_flow(
        plan_identity=plan_identity,
        budget=budget,
        wait=True,
    )

    assert result["operation"] == "flow.run"
    assert result["authority"] == "recorded-flow-result"
    assert result["data"]["management"]["status"] == "accepted"
    assert result["data"]["result"]["status"] == "accepted"
    assert result["data"]["management"]["completed_nodes"] == 1
    assert len(result["data"]["management"]["run_id"]) == 32

    duplicate = interface.run_flow(
        plan_identity=plan_identity,
        budget=budget,
        wait=True,
    )
    assert duplicate == result

    assert agentic_execute_cli_main(
        [
            "--project-root",
            str(tmp_path),
            "--grant",
            str(grant_path),
            "flow-run",
            plan_identity,
            "--maximum-seconds",
            "30",
            "--maximum-nodes",
            "1",
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out) == result

    audit_paths = tuple((tmp_path / "artifacts").rglob("audit.json"))
    assert len(audit_paths) == 1
    audit = json.loads(audit_paths[0].read_text(encoding="utf-8"))
    assert audit["principal"] == "test-operator"
    assert audit["approval"] == "phase3-test-approval"
    assert audit["terminal_status"] == "accepted"
    assert str(tmp_path) not in json.dumps(result)


def test_execution_rejects_unapproved_plan_and_insufficient_budget(tmp_path: Path) -> None:
    write_read_only_flow_project(tmp_path)
    plan_identity = _plan_identity(tmp_path)

    unauthorized = AgenticExecutionInterface.from_project_root(
        tmp_path,
        grant=_grant("f" * 64),
    )
    with pytest.raises(ValueError, match="approved"):
        unauthorized.run_flow(
            plan_identity=plan_identity,
            budget=AgenticExecutionBudget(maximum_seconds=30, maximum_nodes=1),
            wait=False,
        )

    interface = AgenticExecutionInterface.from_project_root(
        tmp_path,
        grant=_grant(plan_identity),
    )
    with pytest.raises(ValueError, match="node budget"):
        interface.run_flow(
            plan_identity=plan_identity,
            budget=AgenticExecutionBudget(maximum_seconds=30, maximum_nodes=0),
            wait=False,
        )
    assert not (tmp_path / "artifacts").exists()


def test_run_cancel_is_owner_bound_and_writes_cancelled_terminal_state(tmp_path: Path) -> None:
    _write_wait_flow(tmp_path)
    plan_identity = _plan_identity(tmp_path)
    interface = AgenticExecutionInterface.from_project_root(
        tmp_path,
        grant=_grant(plan_identity),
    )
    submitted = interface.run_flow(
        plan_identity=plan_identity,
        budget=AgenticExecutionBudget(maximum_seconds=30, maximum_nodes=1),
        wait=False,
    )
    run_id = submitted["data"]["management"]["run_id"]

    interface.wait_until_running(run_id, timeout_seconds=5)
    original_grant = interface.grant
    interface.grant = _grant(plan_identity, approval="different-grant")
    with pytest.raises(ValueError, match="grant"):
        interface.cancel_run(run_id=run_id)
    interface.grant = original_grant
    cancelled = interface.cancel_run(run_id=run_id)

    assert cancelled["operation"] == "run.cancel"
    assert cancelled["conclusion"] == "cancelled"
    assert cancelled["data"]["management"]["status"] == "cancelled"
    inspected = interface.inspect_run(run_id=run_id)
    assert inspected["data"]["management"]["status"] == "cancelled"
    assert inspected["data"]["result"]["interrupted"] is True
    assert inspected["data"]["result"]["nodes"]["wait"]["execution_status"] == "cancelled"

    other = AgenticExecutionInterface.from_project_root(
        tmp_path,
        grant=AgenticExecutionGrant(
            principal="other-operator",
            role="design-operator",
            capabilities=(AgenticExecutionCapability.EXECUTE_DERIVED,),
            approved_plan_sha256=(plan_identity,),
            approval="other-approval",
            expires_at="2099-01-01T00:00:00+00:00",
        ),
    )
    with pytest.raises(ValueError, match="principal"):
        other.cancel_run(run_id=run_id)


def test_run_locator_never_traverses_symlinked_namespace(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    namespace = artifact_root / "system/agentic-flow-runs"
    outside = tmp_path / "outside/example/all/pipeline" / ("f" * 32)
    (outside / "control").mkdir(parents=True)
    namespace.mkdir(parents=True)
    (namespace / "example").symlink_to(tmp_path / "outside/example", target_is_directory=True)
    store = AgenticRunStore(artifact_root, "a" * 64)

    with pytest.raises(ValueError, match="unknown"):
        store.locate("f" * 32)
