from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import (
    write_component_owner,
    write_fake_flow_extension,
    write_project_context,
)
import sigilicon.domain.repository as repository_module
from sigilicon.cli.agentic_read import main as agentic_read_cli_main
from sigilicon.domain.repository import Project
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
from sigilicon.flow import ExecutionEnvironment
from sigilicon.workflows.agentic_read import AgenticReadInterface, _public_value
from sigilicon.workflows.project_runner import ProjectRunner


def _read(root: Path) -> AgenticReadInterface:
    return AgenticReadInterface(Project.from_project_root(root))


def write_read_only_flow_project(root: Path, owner: str = "example") -> Path:
    owner_root = root / "ip" / owner
    flow_root = owner_root / "configs" / "flows"
    flow_root.mkdir(parents=True)
    (flow_root / "pipeline.toml").write_text(
        f'''schema = 1
contract_kind = "execution-recipe"
path_scope = "owner"
owner = "{owner}"
name = "pipeline"

[actions."fake.source"]
adapter = "fake-source"

[[nodes]]
id = "source"
action = "fake.source"
config = {{ text = "hello" }}
''',
        encoding="utf-8",
    )
    catalog = owner_root / "configs" / "targets.toml"
    catalog.write_text(
        f'''schema = 1
contract_kind = "owner-targets"
path_scope = "owner"
owner = "{owner}"

[targets.pipeline]
description = "Read-only pipeline"

[targets.pipeline.operations.all]
recipe = "configs/flows/pipeline.toml"
goals = ["source"]
''',
        encoding="utf-8",
    )
    relative_catalog = catalog.relative_to(root).as_posix()
    relative_flow = (flow_root / "pipeline.toml").relative_to(root).as_posix()
    extension = write_fake_flow_extension(root, owner)
    component = write_component_owner(
        root,
        owner,
        filesets={
            "flow": (
                relative_catalog,
                relative_flow,
                extension.relative_to(root).as_posix(),
            )
        },
    )
    component.write_text(
        component.read_text(encoding="utf-8").replace(
            "\n[filesets]\n",
            f'\ntarget_catalog = "{relative_catalog}"\n\n[filesets]\n',
        ),
        encoding="utf-8",
    )
    return catalog


def test_read_interface_inspects_project_targets_and_plans_without_writing(
    tmp_path: Path,
) -> None:
    write_read_only_flow_project(tmp_path)
    interface = _read(tmp_path)

    assert interface.project_id == f"test.{tmp_path.name}"
    assert not hasattr(interface, "plan_flow")

    project = interface.inspect_project(owner="example")
    plan = interface.plan_target(
        owner="example",
        target="pipeline",
        operation="all",
    )

    assert project["operation"] == "project.inspect"
    assert project["authority"] == "source-contract"
    assert project["conclusion"] == "valid"
    assert [item["name"] for item in project["data"]["owners"]] == ["example"]
    assert project["data"]["owners"][0]["targets"] == [
        {
            "name": "pipeline",
            "description": "Read-only pipeline",
            "operations": ["all"],
        }
    ]
    assert "flows" not in project["data"]["owners"][0]
    assert "catalogs" not in project["data"]
    assert plan["operation"] == "target.plan"
    assert plan["authority"] == "plan"
    assert plan["conclusion"] == "planned"
    assert plan["data"]["plan"]["topology"] == ["source"]
    assert plan["data"]["plan_identity"] == "example:pipeline:all"
    assert "flow" not in plan["data"]
    assert "profile" not in plan["data"]
    assert str(tmp_path) not in json.dumps(project)
    assert str(tmp_path) not in json.dumps(plan)
    assert not (tmp_path / "artifacts").exists()


def test_project_inspection_reads_each_target_catalog_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = write_read_only_flow_project(tmp_path)
    interface = _read(tmp_path)
    original = repository_module.read_toml_record
    reads = 0

    def counted(path: Path):
        nonlocal reads
        if path.resolve() == catalog.resolve():
            reads += 1
        return original(path)

    monkeypatch.setattr(repository_module, "read_toml_record", counted)

    interface.inspect_project(owner="example")

    assert reads == 1

def test_project_rejects_owner_from_another_project(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    write_project_context(first_root)
    write_project_context(second_root)
    write_read_only_flow_project(first_root)
    write_read_only_flow_project(second_root)
    first = _read(first_root).project
    second = _read(second_root).project

    with pytest.raises(ValueError, match="does not contain owner"):
        first.owner_target_catalog(second.owner("example"))


def test_cli_python_and_run_inspection_share_the_exact_interface(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = write_read_only_flow_project(tmp_path)
    interface = _read(tmp_path)
    python_plan = interface.plan_target(
        owner="example",
        target="pipeline",
        operation="all",
    )

    assert agentic_read_cli_main(
        [
            "--project-root",
            str(tmp_path),
            "target-plan",
            "example",
            "pipeline",
            "all",
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out) == python_plan

    project_runner = ProjectRunner(interface.project, "example")
    planned = project_runner.plan("pipeline", "all")
    result = planned.run(
        ExecutionEnvironment(),
        run_id="a" * 32,
    )
    original = repository_module.read_toml_record
    catalog_reads = 0

    def counted(path: Path):
        nonlocal catalog_reads
        if path.resolve() == catalog.resolve():
            catalog_reads += 1
        return original(path)

    monkeypatch.setattr(repository_module, "read_toml_record", counted)
    python_run = interface.inspect_run(
        owner="example",
        target="pipeline",
        operation="all",
        run_id=result.run_id,
    )
    assert catalog_reads == 1
    assert python_run["operation"] == "run.inspect"
    assert python_run["authority"] == "recorded-target-operation-result"
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
    interface = _read(tmp_path)

    with pytest.raises(ValueError, match="owner"):
        interface.inspect_project(owner="../example")
    with pytest.raises(ValueError, match="target"):
        interface.plan_target(
            owner="example",
            target="pipeline; touch owned",
            operation="all",
        )
    with pytest.raises(ValueError, match="unknown target"):
        interface.plan_target(
            owner="other",
            target="missing",
            operation="all",
        )
    with pytest.raises(ValueError, match="Run"):
        interface.inspect_run(
            owner="example",
            target="pipeline",
            operation="all",
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
    interface = _read(tmp_path)
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
