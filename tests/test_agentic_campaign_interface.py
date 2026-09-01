from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from sigilicon.domain.agentic_execution import (
    AgenticExecutionCapability,
    AgenticExecutionGrant,
    AgenticPlanApproval,
)
from sigilicon.domain.circuit_design import EvidenceLevel, EvidenceRole
from sigilicon.domain.repository import Project
from sigilicon.campaigns import store as campaign_store_module
from sigilicon.campaigns.interface import (
    DesignCampaignExecutionInterface as AgenticExecutionInterface,
    DesignCampaignReadInterface,
)
from sigilicon.campaigns.design import (
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
from sigilicon.canonical import canonical_digest
from sigilicon.domain.circuit_design import ProposalProvenance, TopologyOrigin
from sigilicon.campaigns.repair import (
    DesignRepairProposal,
    TopologyRepairPolicy,
)

from test_agentic_read_interface import write_read_only_flow_project
from test_design_campaign import (
    POLICY,
    _topology,
)


def _read(root: Path) -> DesignCampaignReadInterface:
    return DesignCampaignReadInterface.from_project(Project.from_project_root(root))


def _write_campaign_project(root: Path) -> None:
    write_read_only_flow_project(root)
    flow_root = root / "ip/example/configs/flows"
    target_catalog = root / "ip/example/configs/targets.toml"
    target_catalog.write_text(
        target_catalog.read_text(encoding="utf-8").replace(
            'goals = ["source"]',
            'goals = ["attempt"]',
        ),
        encoding="utf-8",
    )
    (flow_root / "pipeline.toml").write_text(
        '''schema = 1
contract_kind = "execution-recipe"
path_scope = "owner"
owner = "example"
name = "pipeline"

[actions."design.attempt"]
adapter = "typed-attempt"

[[nodes]]
id = "attempt"
action = "design.attempt"
''',
        encoding="utf-8",
    )


def _write_campaign_extension(root: Path, *, mode: str = "satisfied") -> None:
    if mode not in {"satisfied", "feedback", "failing"}:
        raise ValueError(f"unsupported campaign test adapter mode: {mode}")
    accepted_extensions = (
        "()"
        if mode == "failing"
        else "(DESIGN_CAMPAIGN_ITERATION_EXTENSION,)"
    )
    adapter = (
        "FeedbackDrivenAttemptAdapter()"
        if mode == "feedback"
        else "AttemptAdapter(EvidenceConclusion.SATISFIED)"
    )
    failure = (
        '    adapter.execute = lambda context: AdapterExecution("failed", 7)\n'
        if mode == "failing"
        else ""
    )
    extension = root / "ip/example/tools/fake_action_module.py"
    extension.write_text(
        f'''from sigilicon.domain.circuit_design import (
    CIRCUIT_TOPOLOGY_KIND,
    DESIGN_CANDIDATE_KIND,
    DESIGN_EVIDENCE_KIND,
    EvidenceConclusion,
)
from sigilicon.flow import ActionContract, AdapterExecution, ArtifactPort
from sigilicon.campaigns.design import DESIGN_CAMPAIGN_ITERATION_EXTENSION
from test_design_campaign import AttemptAdapter, FeedbackDrivenAttemptAdapter


def register_action_modules(registry, project, owner):
    registry.register_action(
        ActionContract(
            "design.attempt",
            outputs=(
                ArtifactPort("candidate", DESIGN_CANDIDATE_KIND),
                ArtifactPort("topology", CIRCUIT_TOPOLOGY_KIND),
                ArtifactPort("l0-evidence", DESIGN_EVIDENCE_KIND),
            ),
            adapters=("typed-attempt",),
            accepted_extensions={accepted_extensions},
        )
    )
    adapter = {adapter}
{failure}    registry.register_adapter("typed-attempt", adapter)
''',
        encoding="utf-8",
    )


def _campaign() -> DesignCampaignSpec:
    return DesignCampaignSpec(
        "example",
        "pilot-campaign",
        DesignCampaignAttemptSpec(
            "baseline",
            "pipeline",
            "all",
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


def test_campaign_execution_returns_public_result_and_immutable_audit(
    tmp_path: Path,
) -> None:
    _write_campaign_project(tmp_path)
    _write_campaign_extension(tmp_path)
    source = _campaign()
    read = _read(tmp_path)
    planned = read.plan_campaign(campaign_json=source.canonical_json())
    campaign_identity = planned["data"]["campaign_identity"]

    grant = _grant(campaign_identity, planned["data"]["plan"])
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

    audits = tuple((tmp_path / "artifacts").rglob("audit.json"))
    assert len(audits) == 1
    audit = json.loads(audits[0].read_text(encoding="utf-8"))
    assert audit["campaign_identity"] == campaign_identity
    assert audit["approval"] == "campaign-test-approval"
    assert audit["terminal_status"] == "passed"


def test_campaign_run_identity_includes_execution_environment(
    tmp_path: Path,
) -> None:
    _write_campaign_project(tmp_path)
    _write_campaign_extension(tmp_path)
    source = _campaign()
    read = _read(tmp_path)
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
) -> None:
    _write_campaign_project(tmp_path)
    _write_campaign_extension(tmp_path)
    source = _campaign()
    read = _read(tmp_path)
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
            _read(tmp_path),
            grant=grant,
            environment_contract=second_path,
        ).run_campaign(
            campaign_json=source.canonical_json(),
            campaign_identity=campaign_identity,
        )


def test_campaign_run_requires_exact_identity_and_grant(
    tmp_path: Path,
) -> None:
    _write_campaign_project(tmp_path)
    _write_campaign_extension(tmp_path)
    source = _campaign()
    read = _read(tmp_path)
    planned = read.plan_campaign(campaign_json=source.canonical_json())
    campaign_identity = planned["data"]["campaign_identity"]
    unrelated = {"contract_kind": "unrelated-plan"}
    execution = AgenticExecutionInterface(
        read,
        grant=_grant(canonical_digest(unrelated), unrelated),
    )

    with pytest.raises(ValueError, match="approved"):
        execution.run_campaign(
            campaign_json=source.canonical_json(),
            campaign_identity=campaign_identity,
        )
    with pytest.raises(ValueError, match="identity drift"):
        AgenticExecutionInterface(
            read,
            grant=_grant(campaign_identity, planned["data"]["plan"]),
        ).run_campaign(
            campaign_json=source.canonical_json(),
            campaign_identity="sha256-" + "0" * 64,
        )
    assert not (tmp_path / "artifacts").exists()


def test_failed_baseline_is_a_durable_terminal_campaign_state(
    tmp_path: Path,
) -> None:
    _write_campaign_project(tmp_path)
    _write_campaign_extension(tmp_path, mode="failing")
    source = _campaign()
    read = _read(tmp_path)
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
    _write_campaign_extension(tmp_path)
    source = _campaign()
    read = _read(tmp_path)
    planned = read.plan_campaign(campaign_json=source.canonical_json())
    grant = _grant(
        planned["data"]["campaign_identity"],
        planned["data"]["plan"],
    )
    original_write = campaign_store_module.write_immutable_text
    failed = False

    def interrupt_campaign_write(path: Path, text: str) -> None:
        nonlocal failed
        if path.name == "campaign.json" and not failed:
            failed = True
            raise OSError("injected partial create")
        original_write(path, text)

    monkeypatch.setattr(
        campaign_store_module,
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
        campaign_store_module,
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
) -> None:
    _write_campaign_project(tmp_path)
    _write_campaign_extension(tmp_path, mode="feedback")
    source = _feedback_campaign()
    read = _read(tmp_path)
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
    rejected = replace(
        proposal,
        proposed_topology=_topology(
            master="UNAPPROVED",
            origin=TopologyOrigin.PROPOSED,
        ),
    )
    with pytest.raises(ValueError, match="Proposal rejected"):
        AgenticExecutionInterface(
            _read(tmp_path),
            grant=grant,
        ).run_campaign(
            run_id=run_id,
            proposal_json=rejected.canonical_json(),
        )
    resumed_interface = AgenticExecutionInterface(
        _read(tmp_path),
        grant=grant,
    )
    completed = resumed_interface.run_campaign(
        run_id=run_id,
        proposal_json=proposal.canonical_json(),
    )

    assert completed["conclusion"] == "passed"
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
    immutable_events = tuple((tmp_path / "artifacts").rglob("event-*.json"))
    assert len(immutable_events) == 2
