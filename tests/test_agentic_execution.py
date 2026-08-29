from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

import sigilicon.workflows.agentic_runs as agentic_runs_module

from sigilicon.cli.agentic_execute import main as agentic_execute_cli_main
from sigilicon.domain.agentic_execution import (
    AGENTIC_EXECUTION_GRANT_KIND,
    AgenticExecutionBudget,
    AgenticExecutionCapability,
    AgenticExecutionGrant,
    AgenticPlanApproval,
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


def _grant(
    root: Path,
    plan_identity: str,
    *,
    approval: str = "phase3-test-approval",
) -> AgenticExecutionGrant:
    record = AgenticReadInterface.from_project_root(root).plan_flow(
        owner="example",
        flow="pipeline",
        target="all",
        profile="offline",
    )["data"]["plan"]
    return AgenticExecutionGrant(
        principal="test-operator",
        role="design-operator",
        capabilities=(AgenticExecutionCapability.EXECUTE_DERIVED,),
        approved_plans=(
            AgenticPlanApproval(plan_identity, json.dumps(record, indent=2, sort_keys=True) + "\n"),
        ),
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
    grant = _grant(tmp_path, plan_identity)

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
        approved_plans=(
            AgenticPlanApproval(
                plan_identity,
                _grant(tmp_path, plan_identity).approved_plans[0].plan_record_json,
            ),
        ),
        approval="expired-test-approval",
        expires_at="2020-01-01T00:00:00+00:00",
    )
    assert not expired.valid_at(datetime.now(timezone.utc))


def test_python_and_cli_execute_the_same_durable_plan(tmp_path: Path, capsys) -> None:
    write_read_only_flow_project(tmp_path)
    plan_identity = _plan_identity(tmp_path)
    grant = _grant(tmp_path, plan_identity)
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
    assert result["data"]["management"]["run_id"].startswith("flow-example-pipeline")

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
        grant=_grant(tmp_path, "forged-plan"),
    )
    with pytest.raises(ValueError, match="approved"):
        unauthorized.run_flow(
            plan_identity=plan_identity,
            budget=AgenticExecutionBudget(maximum_seconds=30, maximum_nodes=1),
            wait=False,
        )

    interface = AgenticExecutionInterface.from_project_root(
        tmp_path,
        grant=_grant(tmp_path, plan_identity),
    )
    with pytest.raises(ValueError, match="node budget"):
        interface.run_flow(
            plan_identity=plan_identity,
            budget=AgenticExecutionBudget(maximum_seconds=30, maximum_nodes=0),
            wait=False,
        )
    assert not (tmp_path / "artifacts").exists()


def test_grant_rejects_plan_config_changed_after_approval(tmp_path: Path) -> None:
    write_read_only_flow_project(tmp_path)
    plan_identity = _plan_identity(tmp_path)
    grant = _grant(tmp_path, plan_identity)
    contract = tmp_path / "ip/example/configs/flows/pipeline.toml"
    contract.write_text(
        contract.read_text(encoding="utf-8").replace(
            'config = { text = "hello" }',
            'config = { text = "changed-after-approval" }',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="changed after execution approval"):
        AgenticExecutionInterface.from_project_root(
            tmp_path,
            grant=grant,
        ).run_flow(
            plan_identity=plan_identity,
            budget=AgenticExecutionBudget(maximum_seconds=30, maximum_nodes=1),
            wait=False,
        )
    assert not (tmp_path / "artifacts").exists()


def test_run_cancel_is_owner_bound_and_writes_cancelled_terminal_state(tmp_path: Path) -> None:
    _write_wait_flow(tmp_path)
    plan_identity = _plan_identity(tmp_path)
    interface = AgenticExecutionInterface.from_project_root(
        tmp_path,
        grant=_grant(tmp_path, plan_identity),
    )
    submitted = interface.run_flow(
        plan_identity=plan_identity,
        budget=AgenticExecutionBudget(maximum_seconds=30, maximum_nodes=1),
        wait=False,
    )
    run_id = submitted["data"]["management"]["run_id"]

    interface.wait_until_running(run_id, timeout_seconds=5)
    assert interface.store.locate(run_id).state["current_node"] == "wait"
    original_grant = interface.grant
    interface.grant = _grant(tmp_path, plan_identity, approval="different-grant")
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
            approved_plans=_grant(tmp_path, plan_identity).approved_plans,
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
    store = AgenticRunStore(artifact_root, "run-a")

    with pytest.raises(ValueError, match="unknown"):
        store.locate("f" * 32)


def test_partial_flow_run_create_recovers_without_replacing_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_read_only_flow_project(tmp_path)
    plan_identity = _plan_identity(tmp_path)
    interface = AgenticExecutionInterface.from_project_root(
        tmp_path,
        grant=_grant(tmp_path, plan_identity),
    )
    original_write = agentic_runs_module.atomic_write_json
    failed = False

    def interrupt_state_write(path: Path, value: dict[str, object]) -> None:
        nonlocal failed
        if path.name == "state.json" and not failed:
            failed = True
            raise OSError("injected partial Flow Run create")
        original_write(path, value)

    monkeypatch.setattr(
        agentic_runs_module,
        "atomic_write_json",
        interrupt_state_write,
    )
    with pytest.raises(OSError, match="partial Flow Run create"):
        interface.run_flow(
            plan_identity=plan_identity,
            budget=AgenticExecutionBudget(maximum_seconds=30, maximum_nodes=1),
            wait=True,
        )
    request_path = next((tmp_path / "artifacts").rglob("control/request.json"))
    request_text = request_path.read_text(encoding="utf-8")
    monkeypatch.setattr(
        agentic_runs_module,
        "atomic_write_json",
        original_write,
    )

    completed = interface.run_flow(
        plan_identity=plan_identity,
        budget=AgenticExecutionBudget(maximum_seconds=30, maximum_nodes=1),
        wait=True,
    )

    assert completed["data"]["management"]["status"] == "accepted"
    assert request_path.read_text(encoding="utf-8") == request_text


def test_agentic_run_audit_and_physical_path_identity_are_strict(tmp_path: Path) -> None:
    write_read_only_flow_project(tmp_path)
    plan_identity = _plan_identity(tmp_path)
    interface = AgenticExecutionInterface.from_project_root(
        tmp_path,
        grant=_grant(tmp_path, plan_identity),
    )
    completed = interface.run_flow(
        plan_identity=plan_identity,
        budget=AgenticExecutionBudget(maximum_seconds=30, maximum_nodes=1),
        wait=True,
    )
    run_id = completed["data"]["management"]["run_id"]
    located = interface.store.locate(run_id)
    audit_path = located.paths.role("audit") / "audit.json"
    valid_audit = json.loads(audit_path.read_text(encoding="utf-8"))
    audit_path.unlink()
    with pytest.raises(ValueError, match="fields do not match"):
        interface.store.write_audit(located.paths, {"schema": 1})
    with pytest.raises(ValueError, match="Flow status"):
        interface.store.write_audit(
            located.paths,
            {**valid_audit, "flow_status": "failed"},
        )

    state_path = located.paths.role("control") / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["owner"] = "forged-owner"
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="path identity drift"):
        interface.store.locate(run_id)
