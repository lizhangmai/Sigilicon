from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import write_component_owner
import sigilicon.flow.contracts as flow_contracts_module
import sigilicon.workflows.agentic_read as agentic_read_module
from sigilicon.cli.agentic_read import main as agentic_read_cli_main
from sigilicon.domain.repository import RepositoryContext
from sigilicon.domain.circuit_design import (
    ARTIFACT_SCHEMA,
    CIRCUIT_TOPOLOGY_KIND,
    DESIGN_CANDIDATE_KIND,
    SOURCE_NETLIST_KIND,
    ArtifactMetadata,
    ArtifactReference,
    CircuitPort,
    CircuitTopologyProposal,
    DesignCandidate,
    PortDirection,
    ProposalProvenance,
    TopologyOrigin,
)
from sigilicon.flow import FlowEngine, load_catalog_selection
from sigilicon.workflows.agentic_read import AgenticReadInterface, _public_value
from sigilicon.workflows.builtin import builtin_workflow_registry


def write_read_only_flow_project(root: Path, owner: str = "example") -> Path:
    owner_root = root / "ip" / owner
    flow_root = owner_root / "configs" / "flows"
    profile_root = flow_root / "profiles"
    profile_root.mkdir(parents=True)
    (flow_root / "pipeline.toml").write_text(
        f'''schema = 1
contract_kind = "flow"
path_scope = "owner"
owner = "{owner}"
name = "pipeline"

[[nodes]]
id = "source"
action = "fake.source"
config = {{ text = "hello" }}

[[targets]]
name = "all"
goals = ["source"]
''',
        encoding="utf-8",
    )
    (profile_root / "offline.toml").write_text(
        f'''schema = 1
contract_kind = "execution-profile"
path_scope = "owner"
owner = "{owner}"
name = "offline"

[actions."fake.source"]
adapter = "fake-source"
''',
        encoding="utf-8",
    )
    catalog = flow_root / "catalog.toml"
    catalog.write_text(
        f'''schema = 1
contract_kind = "flow-catalog"
path_scope = "owner"
owner = "{owner}"

[flows.pipeline]
contract = "configs/flows/pipeline.toml"
default_profile = "offline"

[flows.pipeline.profiles]
offline = "configs/flows/profiles/offline.toml"
''',
        encoding="utf-8",
    )
    relative_catalog = catalog.relative_to(root).as_posix()
    relative_flow = (flow_root / "pipeline.toml").relative_to(root).as_posix()
    relative_profile = (profile_root / "offline.toml").relative_to(root).as_posix()
    write_component_owner(
        root,
        owner,
        filesets={"flow": (relative_catalog, relative_flow, relative_profile)},
    )
    return catalog


def test_read_interface_inspects_cataloged_project_and_plans_without_writing(
    tmp_path: Path,
) -> None:
    write_read_only_flow_project(tmp_path)
    interface = AgenticReadInterface.from_project_root(tmp_path)
    assert interface.repository is interface.project

    with pytest.raises(ValueError, match="identity drift"):
        AgenticReadInterface(
            RepositoryContext.from_project_root(tmp_path),
            "corrupt-identity",
        )

    project = interface.inspect_project(owner="example")
    plan = interface.plan_flow(
        owner="example",
        flow="pipeline",
        target="all",
        profile=None,
    )

    assert project["operation"] == "project.inspect"
    assert project["authority"] == "source-contract"
    assert project["conclusion"] == "valid"
    assert [item["name"] for item in project["data"]["owners"]] == ["example"]
    assert project["data"]["owners"][0]["flows"] == [
        {
            "default_profile": "offline",
            "name": "pipeline",
            "profiles": ["offline"],
            "targets": ["all"],
        }
    ]
    assert plan["operation"] == "flow.plan"
    assert plan["authority"] == "plan"
    assert plan["conclusion"] == "planned"
    assert plan["data"]["plan"]["topology"] == ["source"]
    assert plan["data"]["plan_identity"] == "example:pipeline:all:offline"
    assert str(tmp_path) not in json.dumps(project)
    assert str(tmp_path) not in json.dumps(plan)
    assert not (tmp_path / "artifacts").exists()


def test_project_inspection_reads_each_flow_catalog_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = write_read_only_flow_project(tmp_path)
    interface = AgenticReadInterface.from_project_root(tmp_path)
    original = flow_contracts_module.load_flow_catalog
    reads = 0

    def counted(path: Path, *, owner_root: Path):
        nonlocal reads
        if path.resolve() == catalog.resolve():
            reads += 1
        return original(path, owner_root=owner_root)

    monkeypatch.setattr(agentic_read_module, "load_flow_catalog", counted)
    monkeypatch.setattr(flow_contracts_module, "load_flow_catalog", counted)

    interface.inspect_project(owner="example")

    assert reads == 1

    reads = 0
    resolved = interface.resolve_plan_identity("example:pipeline:all:offline")

    assert resolved.plan_identity == "example:pipeline:all:offline"
    assert reads == 1


def test_cli_python_and_run_inspection_share_the_exact_interface(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    catalog = write_read_only_flow_project(tmp_path)
    interface = AgenticReadInterface.from_project_root(tmp_path)
    python_plan = interface.plan_flow(
        owner="example",
        flow="pipeline",
        target="all",
        profile="offline",
    )

    assert agentic_read_cli_main(
        [
            "--project-root",
            str(tmp_path),
            "flow-plan",
            "example",
            "pipeline",
            "all",
            "--profile",
            "offline",
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out) == python_plan

    owner_root = tmp_path / "ip/example"
    selection = load_catalog_selection(
        catalog,
        owner_root=owner_root,
        flow_id="pipeline",
        profile_id="offline",
    )
    engine = FlowEngine(builtin_workflow_registry())
    result = engine.run(
        engine.plan(selection.spec, "all", selection.profile),
        artifact_root=tmp_path / "artifacts",
        run_id="a" * 32,
    )
    python_run = interface.inspect_run(
        owner="example",
        flow="pipeline",
        target="all",
        run_id=result.run_id,
    )
    assert python_run["operation"] == "run.inspect"
    assert python_run["authority"] == "recorded-flow-result"
    assert python_run["conclusion"] == "recorded"
    assert python_run["data"]["result"]["status"] == "accepted"

    assert agentic_read_cli_main(
        [
            "--project-root",
            str(tmp_path),
            "run-inspect",
            "example",
            "pipeline",
            "all",
            result.run_id,
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out) == python_run


def test_read_interface_rejects_injection_cross_owner_and_identity_drift(
    tmp_path: Path,
) -> None:
    write_read_only_flow_project(tmp_path, "example")
    write_read_only_flow_project(tmp_path, "other")
    interface = AgenticReadInterface.from_project_root(tmp_path)

    with pytest.raises(ValueError, match="owner"):
        interface.inspect_project(owner="../example")
    with pytest.raises(ValueError, match="Flow identity"):
        interface.plan_flow(
            owner="example",
            flow="pipeline; touch owned",
            target="all",
            profile=None,
        )
    with pytest.raises(ValueError, match="cataloged Flow"):
        interface.plan_flow(
            owner="other",
            flow="missing",
            target="all",
            profile=None,
        )
    with pytest.raises(ValueError, match="Run"):
        interface.inspect_run(
            owner="example",
            flow="pipeline",
            target="all",
            run_id="../../outside",
        )


def test_public_projection_preserves_boolean_executable_qualifier() -> None:
    assert _public_value(
        {
            "executable": True,
            "command": ["private-tool", "--private-option"],
        }
    ) == {
        "executable": True,
        "command": "<redacted-private-execution-material>",
    }


def test_candidate_validation_has_python_cli_parity(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_read_only_flow_project(tmp_path)
    interface = AgenticReadInterface.from_project_root(tmp_path)
    topology = CircuitTopologyProposal(
        ArtifactMetadata(ARTIFACT_SCHEMA, CIRCUIT_TOPOLOGY_KIND, "example", "example:topology:agentic-read"),
        "inv",
        "source-fixture",
        TopologyOrigin.PROPOSED,
        (CircuitPort("IN", PortDirection.INPUT, "signal"),),
        (),
        (),
        (),
        ProposalProvenance("test-proposal", "1"),
    )
    candidate = DesignCandidate(
        ArtifactMetadata(ARTIFACT_SCHEMA, DESIGN_CANDIDATE_KIND, "example", "example:candidate:agentic-read"),
        "inv",
        ArtifactReference("example", SOURCE_NETLIST_KIND, "source-fixture", None),
        None,
        (),
        topology.reference(),
        None,
        None,
        (),
        None,
        topology.provenance,
    )
    candidate_path = tmp_path / "candidate.json"
    topology_path = tmp_path / "topology.json"
    candidate_path.write_text(candidate.canonical_json(), encoding="utf-8")
    topology_path.write_text(topology.canonical_json(), encoding="utf-8")

    expected = interface.validate_candidate(
        owner="example",
        candidate_json=candidate.canonical_json(),
        artifact_json=(topology.canonical_json(),),
    )
    assert agentic_read_cli_main(
        [
            "--project-root",
            str(tmp_path),
            "candidate-validate",
            "example",
            "--candidate",
            str(candidate_path),
            "--artifact",
            str(topology_path),
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out) == expected

    with pytest.raises(ValueError, match="owner"):
        interface.validate_candidate(
            owner="other",
            candidate_json=candidate.canonical_json(),
            artifact_json=(topology.canonical_json(),),
        )
