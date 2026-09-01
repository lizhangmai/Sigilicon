from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import (
    write_component_owner,
    write_fake_action_module,
    write_project_context,
)
from sigilicon.domain.repository import Project
from sigilicon.workflows.agentic_read import AgenticReadInterface, _public_value
from sigilicon.workflows.run_read import RunReadInterface
from sigilicon.workflows.project import bind_run_read, bind_run_store


def _read(root: Path) -> AgenticReadInterface:
    return AgenticReadInterface.from_project(Project.from_project_root(root))


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
    extension = write_fake_action_module(root, owner)
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
    assert plan["data"]["plan_identity"].startswith("sha256-")
    assert "flow" not in plan["data"]
    assert "profile" not in plan["data"]
    assert str(tmp_path) not in json.dumps(project)
    assert str(tmp_path) not in json.dumps(plan)
    assert not (tmp_path / "artifacts").exists()


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
        RunReadInterface.from_project(interface.project).inspect(
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


def test_historical_run_binders_reject_redirected_project_root(tmp_path: Path) -> None:
    declared = tmp_path / "declared"
    redirected = tmp_path / "redirected"
    write_project_context(declared)
    write_project_context(redirected)
    contract = declared / "sigilicon.toml"
    contract.write_text(
        contract.read_text(encoding="utf-8").replace(
            'project_root = "."',
            'project_root = "../redirected"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="different project root"):
        bind_run_read(declared)
    with pytest.raises(ValueError, match="different project root"):
        bind_run_store(declared)
