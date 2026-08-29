from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from sigilicon.cli.agentic_execute import main as execute_cli_main
from sigilicon.cli.agentic_read import main as read_cli_main
from sigilicon.domain.agentic_execution import (
    AgenticExecutionCapability,
    AgenticExecutionGrant,
    AgenticPlanApproval,
)
from sigilicon.domain.circuit_design import (
    CIRCUIT_TOPOLOGY_KIND,
    DESIGN_CANDIDATE_KIND,
    DESIGN_EVIDENCE_KIND,
    EvidenceConclusion,
    EvidenceLevel,
    EvidenceRole,
)
from sigilicon.flow import ActionContract, AdapterExecution, ArtifactPort, FlowRegistry
from sigilicon.workflows import agentic_read as agentic_read_module
from sigilicon.workflows import agentic_campaigns as agentic_campaigns_module
from sigilicon.workflows.agentic_execution import AgenticExecutionInterface
from sigilicon.workflows.agentic_read import AgenticReadInterface
from sigilicon.workflows.design_campaign import (
    DesignArtifactBinding,
    DesignCampaignAttemptSpec,
    DesignCampaignBudget,
    DesignCampaignContinuationSpec,
    DesignCampaignScope,
    DesignCampaignSpec,
    DesignStage,
    DesignStageBinding,
    design_campaign_state_from_json,
)
from sigilicon.canonical import canonical_json
from sigilicon.domain.circuit_design import ProposalProvenance, TopologyOrigin
from sigilicon.workflows.design_repair import (
    DesignRepairProposal,
    TopologyRepairPolicy,
)

from test_agentic_read_interface import write_read_only_flow_project
from test_design_campaign import (
    ACTION,
    POLICY,
    AttemptAdapter,
    FeedbackDrivenAttemptAdapter,
    _topology,
)
from test_design_promotion import _inputs as promotion_inputs


def _write_campaign_project(root: Path) -> None:
    write_read_only_flow_project(root)
    flow_root = root / "ip/example/configs/flows"
    (flow_root / "pipeline.toml").write_text(
        '''schema = 1
contract_kind = "flow"
path_scope = "owner"
owner = "example"
name = "pipeline"

[[nodes]]
id = "attempt"
action = "design.attempt"

[[targets]]
name = "all"
goals = ["attempt"]
''',
        encoding="utf-8",
    )
    (flow_root / "profiles/offline.toml").write_text(
        '''schema = 1
contract_kind = "execution-profile"
path_scope = "owner"
owner = "example"
name = "offline"

[actions."design.attempt"]
adapter = "typed-attempt"
''',
        encoding="utf-8",
    )


def _registry(original, owner_root: Path | None) -> FlowRegistry:
    registry = original(owner_root)
    registry.register_action(
        ActionContract(
            ACTION,
            outputs=(
                ArtifactPort("candidate", DESIGN_CANDIDATE_KIND),
                ArtifactPort("topology", CIRCUIT_TOPOLOGY_KIND),
                ArtifactPort("l0-evidence", DESIGN_EVIDENCE_KIND),
            ),
            adapters=("typed-attempt",),
            accepts_design_campaign_iteration=True,
        )
    )
    from sigilicon.domain.circuit_design import EvidenceConclusion

    registry.register_adapter(
        "typed-attempt",
        AttemptAdapter(EvidenceConclusion.SATISFIED),
    )
    return registry


def _feedback_registry(original, owner_root: Path | None) -> FlowRegistry:
    registry = original(owner_root)
    registry.register_action(
        ActionContract(
            ACTION,
            outputs=(
                ArtifactPort("candidate", DESIGN_CANDIDATE_KIND),
                ArtifactPort("topology", CIRCUIT_TOPOLOGY_KIND),
                ArtifactPort("l0-evidence", DESIGN_EVIDENCE_KIND),
            ),
            adapters=("typed-attempt",),
            accepts_design_campaign_iteration=True,
        )
    )
    registry.register_adapter("typed-attempt", FeedbackDrivenAttemptAdapter())
    return registry


def _failing_registry(original, owner_root: Path | None) -> FlowRegistry:
    registry = original(owner_root)
    registry.register_action(
        ActionContract(
            ACTION,
            outputs=(
                ArtifactPort("candidate", DESIGN_CANDIDATE_KIND),
                ArtifactPort("topology", CIRCUIT_TOPOLOGY_KIND),
                ArtifactPort("l0-evidence", DESIGN_EVIDENCE_KIND),
            ),
            adapters=("typed-attempt",),
        )
    )
    adapter = AttemptAdapter(EvidenceConclusion.SATISFIED)
    adapter.execute = lambda context: AdapterExecution("failed", 7)  # type: ignore[method-assign]
    registry.register_adapter("typed-attempt", adapter)
    return registry


def _patch_project_registry(
    monkeypatch: pytest.MonkeyPatch,
    extension=_registry,
) -> None:
    original = agentic_read_module.project_workflow_registry

    def assemble(project, owner_root):
        return extension(
            lambda selected_root: original(project, selected_root),
            owner_root,
        )

    monkeypatch.setattr(
        agentic_read_module,
        "project_workflow_registry",
        assemble,
    )


def _campaign() -> DesignCampaignSpec:
    return DesignCampaignSpec(
        "example",
        "pilot-campaign",
        DesignCampaignAttemptSpec(
            "baseline",
            "pipeline",
            "all",
            "offline",
            DesignArtifactBinding("candidate", "attempt", "candidate"),
            (
                DesignArtifactBinding("topology", "attempt", "topology"),
                DesignArtifactBinding("l0", "attempt", "l0-evidence"),
            ),
            (DesignStageBinding(DesignStage.L0, "l0"),),
        ),
        DesignCampaignBudget(1, 1, 1),
        DesignCampaignScope(
            (DesignStage.L0,),
            EvidenceRole.DIAGNOSTIC,
            EvidenceLevel.L0,
            ("functional",),
            POLICY,
        ),
    )


def _feedback_campaign() -> DesignCampaignSpec:
    baseline = _campaign()
    return DesignCampaignSpec(
        baseline.owner,
        "feedback-campaign",
        baseline.baseline,
        DesignCampaignBudget(2, 2, 2),
        baseline.scope,
        DesignCampaignContinuationSpec(
            "pipeline",
            "all",
            "offline",
            baseline.baseline.candidate,
            baseline.baseline.artifacts,
            baseline.baseline.stages,
            "attempt",
            TopologyRepairPolicy(
                "example",
                "topology-policy",
                ("BUF",),
                1,
                ("l0-functional",),
            ),
        ),
    )


def _grant(campaign_identity: str, plan_record: dict[str, object]) -> AgenticExecutionGrant:
    return AgenticExecutionGrant(
        principal="test-operator",
        role="design-operator",
        capabilities=(AgenticExecutionCapability.EXECUTE_DERIVED,),
        approved_plans=(
            AgenticPlanApproval(campaign_identity, canonical_json(plan_record)),
        ),
        approval="campaign-test-approval",
        expires_at="2099-01-01T00:00:00+00:00",
    )


def test_campaign_python_cli_share_plan_execution_and_immutable_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_campaign_project(tmp_path)
    _patch_project_registry(monkeypatch)
    source = _campaign()
    campaign_path = tmp_path / "campaign.json"
    campaign_path.write_text(source.canonical_json(), encoding="utf-8")
    read = AgenticReadInterface.from_project_root(tmp_path)
    planned = read.plan_campaign(campaign_json=source.canonical_json())
    campaign_identity = planned["data"]["campaign_identity"]

    assert read_cli_main(
        [
            "--project-root",
            str(tmp_path),
            "campaign-plan",
            "--campaign",
            str(campaign_path),
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out) == planned

    grant = _grant(campaign_identity, planned["data"]["plan"])
    grant_path = tmp_path / "grant.json"
    grant_path.write_text(grant.canonical_json(), encoding="utf-8")
    execution = AgenticExecutionInterface(read, grant=grant)
    result = execution.run_campaign(
        campaign_json=source.canonical_json(),
        campaign_identity=campaign_identity,
    )

    assert result["operation"] == "campaign.run"
    assert result["conclusion"] == "passed"
    assert result["data"]["result"]["termination"] == "passed"
    assert result["data"]["result"]["final_decision"]["role"] == "diagnostic"
    assert str(tmp_path) not in json.dumps(result)
    assert execution.run_campaign(
        campaign_json=source.canonical_json(),
        campaign_identity=campaign_identity,
    ) == result

    assert execute_cli_main(
        [
            "--project-root",
            str(tmp_path),
            "--grant",
            str(grant_path),
            "campaign-run",
            campaign_identity,
            "--campaign",
            str(campaign_path),
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out) == result
    audits = tuple((tmp_path / "artifacts").rglob("audit.json"))
    assert len(audits) == 1
    audit = json.loads(audits[0].read_text(encoding="utf-8"))
    assert audit["campaign_identity"] == campaign_identity
    assert audit["approval"] == "campaign-test-approval"
    assert audit["terminal_status"] == "passed"


def test_campaign_run_identity_includes_execution_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_campaign_project(tmp_path)
    _patch_project_registry(monkeypatch)
    source = _campaign()
    read = AgenticReadInterface.from_project_root(tmp_path)
    planned = read.plan_campaign(campaign_json=source.canonical_json())
    campaign_identity = planned["data"]["campaign_identity"]
    grant = _grant(campaign_identity, planned["data"]["plan"])

    def environment(name: str) -> Path:
        path = tmp_path / f"{name}.toml"
        path.write_text(
            f'''schema = 1
contract_kind = "execution-environment"
path_scope = "site"
owner = "fixture-site"
name = "{name}"
''',
            encoding="utf-8",
        )
        return path

    first = AgenticExecutionInterface(
        read,
        grant=grant,
        environment_contract=environment("first-environment"),
    ).run_campaign(
        campaign_json=source.canonical_json(),
        campaign_identity=campaign_identity,
    )
    second = AgenticExecutionInterface(
        read,
        grant=grant,
        environment_contract=environment("second-environment"),
    ).run_campaign(
        campaign_json=source.canonical_json(),
        campaign_identity=campaign_identity,
    )

    assert first["data"]["management"]["run_id"] != second["data"]["management"]["run_id"]
    assert (
        first["data"]["state"]["iterations"][0]["provenance"]["run_id"]
        != second["data"]["state"]["iterations"][0]["provenance"]["run_id"]
    )


def test_same_environment_id_with_changed_path_or_record_cannot_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_campaign_project(tmp_path)
    _patch_project_registry(monkeypatch)
    source = _campaign()
    read = AgenticReadInterface.from_project_root(tmp_path)
    planned = read.plan_campaign(campaign_json=source.canonical_json())
    campaign_identity = planned["data"]["campaign_identity"]
    grant = _grant(campaign_identity, planned["data"]["plan"])

    first_path = tmp_path / "site-a/environment.toml"
    second_path = tmp_path / "site-b/environment.toml"
    first_path.parent.mkdir()
    second_path.parent.mkdir()
    common = '''schema = 1
contract_kind = "execution-environment"
path_scope = "site"
owner = "fixture-site"
name = "shared-environment"
'''
    first_path.write_text(common, encoding="utf-8")
    second_path.write_text(
        common
        + '''
[capabilities.contract-probe]
identity = "different-site-contract"
''',
        encoding="utf-8",
    )
    AgenticExecutionInterface(
        read,
        grant=grant,
        environment_contract=first_path,
    ).run_campaign(
        campaign_json=source.canonical_json(),
        campaign_identity=campaign_identity,
    )

    with pytest.raises(ValueError, match="partial request conflict"):
        AgenticExecutionInterface(
            AgenticReadInterface.from_project_root(tmp_path),
            grant=grant,
            environment_contract=second_path,
        ).run_campaign(
            campaign_json=source.canonical_json(),
            campaign_identity=campaign_identity,
        )


def test_campaign_run_requires_exact_identity_and_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_campaign_project(tmp_path)
    _patch_project_registry(monkeypatch)
    source = _campaign()
    read = AgenticReadInterface.from_project_root(tmp_path)
    planned = read.plan_campaign(campaign_json=source.canonical_json())
    campaign_identity = planned["data"]["campaign_identity"]
    execution = AgenticExecutionInterface(
        read,
        grant=_grant("forged-campaign", planned["data"]["plan"]),
    )

    with pytest.raises(ValueError, match="approved"):
        execution.run_campaign(
            campaign_json=source.canonical_json(),
            campaign_identity=campaign_identity,
        )
    with pytest.raises(ValueError, match="identity drift"):
        AgenticExecutionInterface(
            read,
            grant=_grant("forged-campaign", planned["data"]["plan"]),
        ).run_campaign(
            campaign_json=source.canonical_json(),
            campaign_identity="forged-campaign",
        )
    assert not (tmp_path / "artifacts").exists()


def test_failed_baseline_is_a_durable_terminal_campaign_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_campaign_project(tmp_path)
    _patch_project_registry(monkeypatch, _failing_registry)
    source = _campaign()
    read = AgenticReadInterface.from_project_root(tmp_path)
    planned = read.plan_campaign(campaign_json=source.canonical_json())
    campaign_identity = planned["data"]["campaign_identity"]
    interface = AgenticExecutionInterface(
        read,
        grant=_grant(campaign_identity, planned["data"]["plan"]),
    )

    failed = interface.run_campaign(
        campaign_json=source.canonical_json(),
        campaign_identity=campaign_identity,
    )

    assert failed["conclusion"] == "execution_failed"
    assert failed["data"]["state"]["iterations"] == []
    assert interface.run_campaign(
        campaign_json=source.canonical_json(),
        campaign_identity=campaign_identity,
    ) == failed
    assert len(tuple((tmp_path / "artifacts").rglob("event-*.json"))) == 1
    assert len(tuple((tmp_path / "artifacts").rglob("audit.json"))) == 1


def test_partial_campaign_create_is_completed_without_replacing_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_campaign_project(tmp_path)
    _patch_project_registry(monkeypatch)
    source = _campaign()
    read = AgenticReadInterface.from_project_root(tmp_path)
    planned = read.plan_campaign(campaign_json=source.canonical_json())
    grant = _grant(
        planned["data"]["campaign_identity"],
        planned["data"]["plan"],
    )
    original_write = agentic_campaigns_module.write_immutable_text
    failed = False

    def interrupt_campaign_write(path: Path, text: str) -> None:
        nonlocal failed
        if path.name == "campaign.json" and not failed:
            failed = True
            raise OSError("injected partial create")
        original_write(path, text)

    monkeypatch.setattr(
        agentic_campaigns_module,
        "write_immutable_text",
        interrupt_campaign_write,
    )
    interface = AgenticExecutionInterface(read, grant=grant)
    with pytest.raises(OSError, match="partial create"):
        interface.run_campaign(
            campaign_json=source.canonical_json(),
            campaign_identity=planned["data"]["campaign_identity"],
        )
    request_path = next((tmp_path / "artifacts").rglob("inputs/request.json"))
    request_text = request_path.read_text(encoding="utf-8")
    monkeypatch.setattr(
        agentic_campaigns_module,
        "write_immutable_text",
        original_write,
    )

    completed = interface.run_campaign(
        campaign_json=source.canonical_json(),
        campaign_identity=planned["data"]["campaign_identity"],
    )

    assert completed["conclusion"] == "passed"
    assert request_path.read_text(encoding="utf-8") == request_text


def test_campaign_resumes_across_processes_without_preenumerated_second_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_campaign_project(tmp_path)
    _patch_project_registry(monkeypatch, _feedback_registry)
    source = _feedback_campaign()
    read = AgenticReadInterface.from_project_root(tmp_path)
    planned = read.plan_campaign(campaign_json=source.canonical_json())
    campaign_identity = planned["data"]["campaign_identity"]
    grant = _grant(campaign_identity, planned["data"]["plan"])

    started = AgenticExecutionInterface(read, grant=grant).run_campaign(
        campaign_json=source.canonical_json(),
        campaign_identity=campaign_identity,
    )

    assert started["conclusion"] == "proposal_required"
    run_id = started["data"]["management"]["run_id"]
    state = design_campaign_state_from_json(
        canonical_json(started["data"]["state"])
    )
    assert state.attribution is not None
    proposal = DesignRepairProposal(
        "example:proposal:feedback-round-2",
        "example",
        campaign_identity,
        state.iterations[-1].candidate.identity,
        state.attribution.identity,
        tuple(item.identity for item in state.attribution.evidence),
        _topology(master="BUF", origin=TopologyOrigin.PROPOSED),
        None,
        None,
        ("l0-functional",),
        ProposalProvenance(
            "test-semantic-client",
            "1",
            (state.iterations[-1].candidate.identity,),
        ),
    )
    baseline_event = next((tmp_path / "artifacts").rglob("event-0001.json"))
    baseline_pointer = next((tmp_path / "artifacts").rglob("control/state.json"))
    baseline_event.unlink()
    baseline_pointer.unlink()
    recovered = AgenticExecutionInterface(
        AgenticReadInterface.from_project_root(tmp_path),
        grant=grant,
    ).run_campaign(
        campaign_json=source.canonical_json(),
        campaign_identity=campaign_identity,
    )
    assert recovered == started
    assert baseline_event.is_file()
    assert baseline_pointer.is_file()
    event_text = baseline_event.read_text(encoding="utf-8")
    sequence_gap = baseline_event.with_name("event-0003.json")
    sequence_gap.write_text(event_text, encoding="utf-8")
    with pytest.raises(ValueError, match="sequence is incomplete"):
        AgenticExecutionInterface(
            AgenticReadInterface.from_project_root(tmp_path),
            grant=grant,
        ).campaign_store.locate(run_id)
    sequence_gap.unlink()
    baseline_event.write_text("{\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="cannot read Design Campaign Event"):
        AgenticExecutionInterface(
            AgenticReadInterface.from_project_root(tmp_path),
            grant=grant,
        ).campaign_store.locate(run_id)
    baseline_event.write_text(event_text, encoding="utf-8")
    rejected = replace(
        proposal,
        proposed_topology=_topology(
            master="UNAPPROVED",
            origin=TopologyOrigin.PROPOSED,
        ),
    )
    with pytest.raises(ValueError, match="Proposal rejected"):
        AgenticExecutionInterface(
            AgenticReadInterface.from_project_root(tmp_path),
            grant=grant,
        ).run_campaign(
            run_id=run_id,
            proposal_json=rejected.canonical_json(),
        )
    assert len(tuple((tmp_path / "artifacts").rglob("event-*.json"))) == 1
    state_pointer = next((tmp_path / "artifacts").rglob("control/state.json"))
    state_pointer.unlink()

    resumed_interface = AgenticExecutionInterface(
        AgenticReadInterface.from_project_root(tmp_path),
        grant=grant,
    )
    located = resumed_interface.campaign_store.locate(run_id)
    with resumed_interface.campaign_store.exclusive(located.paths):
        with pytest.raises(ValueError, match="already in progress"):
            AgenticExecutionInterface(
                AgenticReadInterface.from_project_root(tmp_path),
                grant=grant,
            ).run_campaign(
                run_id=run_id,
                proposal_json=proposal.canonical_json(),
            )
    completed = resumed_interface.run_campaign(
        run_id=run_id,
        proposal_json=proposal.canonical_json(),
    )

    assert completed["conclusion"] == "passed"
    assert state_pointer.is_file()
    assert len(completed["data"]["state"]["iterations"]) == 2
    assert source.baseline.iteration_id == "baseline"
    assert resumed_interface.run_campaign(
        run_id=run_id,
        proposal_json=proposal.canonical_json(),
    ) == completed
    with pytest.raises(ValueError, match="same Design Repair Proposal ID"):
        resumed_interface.run_campaign(
            run_id=run_id,
            proposal_json=replace(
                proposal,
                required_regressions=("different-regression",),
            ).canonical_json(),
        )
    grant_path = tmp_path / "grant.json"
    grant_path.write_text(grant.canonical_json(), encoding="utf-8")
    proposal_path = tmp_path / "proposal.json"
    proposal_path.write_text(proposal.canonical_json(), encoding="utf-8")
    assert execute_cli_main(
        [
            "--project-root",
            str(tmp_path),
            "--grant",
            str(grant_path),
            "campaign-run",
            "--run-id",
            run_id,
            "--proposal",
            str(proposal_path),
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out) == completed
    immutable_events = tuple((tmp_path / "artifacts").rglob("event-*.json"))
    assert len(immutable_events) == 2


def test_promotion_plan_python_and_cli_share_non_writing_interface(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_campaign_project(tmp_path)
    candidate, topology, evidence, decision, request = promotion_inputs()
    inputs = {
        "candidate.json": candidate.canonical_json(),
        "topology.json": topology.canonical_json(),
        "evidence.json": evidence.canonical_json(),
        "decision.json": decision.canonical_json(),
        "promotion-request.json": request.canonical_json(),
    }
    for name, value in inputs.items():
        (tmp_path / name).write_text(value, encoding="utf-8")
    source_before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in (tmp_path / "ip").rglob("*")
        if path.is_file()
    }
    interface = AgenticReadInterface.from_project_root(tmp_path)
    expected = interface.plan_candidate_promotion(
        owner="example",
        candidate_json=inputs["candidate.json"],
        artifact_json=(inputs["topology.json"], inputs["evidence.json"]),
        decision_json=inputs["decision.json"],
        request_json=inputs["promotion-request.json"],
    )

    assert read_cli_main(
        [
            "--project-root",
            str(tmp_path),
            "candidate-promotion-plan",
            "example",
            "--candidate",
            str(tmp_path / "candidate.json"),
            "--artifact",
            str(tmp_path / "topology.json"),
            "--artifact",
            str(tmp_path / "evidence.json"),
            "--decision",
            str(tmp_path / "decision.json"),
            "--request",
            str(tmp_path / "promotion-request.json"),
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out) == expected
    assert expected["data"]["human_approval_required"] is True
    assert expected["data"]["writes_canonical_source"] is False
    assert source_before == {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in (tmp_path / "ip").rglob("*")
        if path.is_file()
    }
