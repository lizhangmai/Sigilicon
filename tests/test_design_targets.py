from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import sigilicon.domain.repository as repository_module

from sigilicon.cli import flow as flow_cli
from sigilicon.domain.repository import Project
from sigilicon.flow import ExecutionEnvironment, FlowExecutionError
from sigilicon.workflows.design_targets import load_design_target_catalog
from sigilicon.workflows.project_flow import ProjectFlow, RunRequest

from conftest import write_component_owner


def _catalog_project(tmp_path: Path) -> tuple[Path, Path]:
    flow_root = tmp_path / "ip/example/configs/flows"
    flow_root.mkdir(parents=True)
    design = tmp_path / "ip/example/leaf"
    design.mkdir(parents=True)
    runner = design / "run.py"
    runner.write_text("print('{\"passed\": true}')\n", encoding="utf-8")
    spec = design / "design.toml"
    spec.write_text("# delegated design spec\n", encoding="utf-8")
    (flow_root / "design_targets.toml").write_text(
        '''
schema = 1
contract_kind = "flow-design-registry"
path_scope = "owner"
owner = "example"

[targets.leaf]
description = "Test leaf"
kind = "script"
entrypoint = "ip/example/leaf/run.py"
spec_argument = "--spec"
spec = "ip/example/leaf/design.toml"
[targets.leaf.modes]
topology = { action = "circuit-design.source-check", evidence_level = "l0" }
sync = { args = ["--overwrite"], action = "circuit-design.source-check", evidence_level = "l0" }
[targets.leaf.routes]
topology = ["design-checks", "leaf-topology"]
sync = ["design-checks", "leaf-sync"]
''',
        encoding="utf-8",
    )
    (flow_root / "catalog.toml").write_text(
        '''
schema = 1
contract_kind = "flow-catalog"
path_scope = "owner"
owner = "example"

[flows.design-checks]
contract = "configs/flows/design_checks.toml"
default_profile = "design-checks"

[flows.design-checks.profiles]
design-checks = "configs/flows/design_profile.toml"
''',
        encoding="utf-8",
    )
    (flow_root / "design_checks.toml").write_text(
        '''
schema = 1
contract_kind = "flow"
path_scope = "owner"
owner = "example"
name = "design-checks"

[expand]
kind = "design-target-routes"
policy = "passed"
evidence_role = "diagnostic"

[[policies]]
id = "passed"

[[policies.checks]]
id = "passed"
fact = "passed"
operator = "equals"
expected = true
''',
        encoding="utf-8",
    )
    (flow_root / "design_profile.toml").write_text(
        '''
schema = 1
contract_kind = "execution-profile"
path_scope = "owner"
owner = "example"
name = "design-checks"

[actions."circuit-design.source-check"]
adapter = "project-design-source-check"

[actions."circuit-design.source-check".config]
timeout_seconds = 30
''',
        encoding="utf-8",
    )
    write_component_owner(
        tmp_path,
        "example",
        filesets={
            "flow": (
                "ip/example/configs/flows/design_targets.toml",
                "ip/example/configs/flows/catalog.toml",
                "ip/example/configs/flows/design_checks.toml",
                "ip/example/configs/flows/design_profile.toml",
            ),
        },
    )
    return runner.resolve(), spec.resolve()


def test_design_target_catalog_can_start_empty(tmp_path: Path) -> None:
    flows = tmp_path / "ip/example/configs/flows"
    flows.mkdir(parents=True)
    (flows / "design_targets.toml").write_text(
        "schema = 1\ncontract_kind = \"flow-design-registry\"\npath_scope = \"owner\"\nowner = \"example\"\n\n[targets]\n",
        encoding="utf-8",
    )
    write_component_owner(
        tmp_path,
        "example",
        filesets={
            "flow": ("ip/example/configs/flows/design_targets.toml",),
        },
    )

    catalog = load_design_target_catalog(Project.from_project_root(tmp_path))

    assert catalog.paths == ((flows / "design_targets.toml").resolve(),)
    assert catalog.targets == ()


def test_design_target_catalog_is_an_optional_project_domain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = Project.from_project_root(tmp_path)

    catalog = load_design_target_catalog(project=project)

    assert catalog.project is project
    assert catalog.paths == ()
    assert catalog.targets == ()

    monkeypatch.chdir(tmp_path)
    assert flow_cli.main(["design", "list", "--json"]) == 0
    assert capsys.readouterr().out == "[]\n"


def test_design_target_catalog_binds_explicit_project(tmp_path: Path) -> None:
    _catalog_project(tmp_path)
    project = Project.from_project_root(tmp_path)

    catalog = load_design_target_catalog(project=project)

    assert catalog.project is project
    assert catalog.project_root == tmp_path


def test_design_target_loader_reads_its_catalog_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _catalog_project(tmp_path)
    catalog_path = (
        tmp_path / "ip/example/configs/flows/design_targets.toml"
    ).resolve()
    reads = 0
    original_read = repository_module.read_toml_record

    def counted_read(path):
        nonlocal reads
        if Path(path).resolve() == catalog_path:
            reads += 1
        return original_read(path)

    monkeypatch.setattr(repository_module, "read_toml_record", counted_read)
    project = Project.from_project_root(tmp_path)

    catalog = load_design_target_catalog(project=project)

    assert len(catalog.targets) == 1
    assert reads == 1


def test_design_catalog_preserves_project_identity(tmp_path: Path) -> None:
    _catalog_project(tmp_path)
    project = Project.from_file(tmp_path / "sigilicon.toml")

    catalog = load_design_target_catalog(project=project)

    assert catalog.project is project


def test_project_design_action_runs_inside_one_flow_lifecycle(tmp_path: Path) -> None:
    _catalog_project(tmp_path)
    project = Project.from_project_root(tmp_path)
    workflow = ProjectFlow(project, "example")
    planned = workflow.plan(RunRequest.design("leaf", "topology"))

    result = workflow.run(
        planned,
        ExecutionEnvironment(),
        run_id="design-flow-fixture",
    )
    payload = workflow.read_result(
        flow=result.flow_id,
        target=result.target,
        run_id=result.run_id,
    )

    assert payload["status"] == "accepted"
    assert payload["nodes"]["leaf-topology"]["facts"]["passed"] is True
    assert {
        source["path"] for source in planned.record["implementation_sources"]
    } == {
        "ip/example/configs/flows/design_targets.toml",
        "ip/example/leaf/design.toml",
        "ip/example/leaf/run.py",
    }
    assert list(result.run_root.rglob("run_manifest.json")) == [
        result.run_root / "run_manifest.json"
    ]


def test_project_design_action_rejects_exact_route_source_drift(tmp_path: Path) -> None:
    runner, _spec = _catalog_project(tmp_path)
    project = Project.from_project_root(tmp_path)
    workflow = ProjectFlow(project, "example")
    planned = workflow.plan_design(
        load_design_target_catalog(project),
        target="leaf",
        mode="topology",
    )
    runner.write_text("print('{\"passed\": false}')\n", encoding="utf-8")

    with pytest.raises(FlowExecutionError, match="preflight is blocked"):
        workflow.run(
            planned,
            ExecutionEnvironment(),
            run_id="design-source-drift-fixture",
        )


def test_project_design_action_preserves_valid_failed_diagnostic(
    tmp_path: Path,
) -> None:
    runner, _spec = _catalog_project(tmp_path)
    runner.write_text(
        "print('{\"passed\": false, \"reason\": \"fixture\", "
        "\"manifest\": \"/private/run_manifest.json\"}')\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    project = Project.from_project_root(tmp_path)
    workflow = ProjectFlow(project, "example")
    planned = workflow.plan_design(
        load_design_target_catalog(project),
        target="leaf",
        mode="topology",
    )

    result = workflow.run(
        planned,
        ExecutionEnvironment(),
        run_id="design-failed-diagnostic-fixture",
    )

    outcome = result.nodes["leaf-topology"]
    assert result.status == "failed"
    assert outcome.execution_status == "succeeded"
    assert outcome.result_status == "valid"
    assert outcome.facts["passed"] is False
    assert outcome.facts["execution-completed"] is True
    assert outcome.facts["process-returncode"] == 1
    payload = json.loads(
        outcome.artifacts["evidence"].path.read_text(encoding="utf-8")
    )
    assert payload["runner_result"]["reason"] == "fixture"
    assert "manifest" not in payload["runner_result"]
    assert "/private/run_manifest.json" not in json.dumps(payload)
    assert payload["process_returncode"] == 1


def test_design_catalog_owner_must_match_project_flow(tmp_path: Path) -> None:
    _catalog_project(tmp_path)
    catalog_path = tmp_path / "ip/example/configs/flows/design_targets.toml"
    catalog_path.write_text(
        catalog_path.read_text(encoding="utf-8").replace(
            'owner = "example"', 'owner = "different-owner"'
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="owner must be 'example'"):
        load_design_target_catalog(Project.from_project_root(tmp_path))


def test_design_catalog_rejects_unknown_fields(tmp_path: Path) -> None:
    _catalog_project(tmp_path)
    catalog_path = tmp_path / "ip/example/configs/flows/design_targets.toml"
    source = catalog_path.read_text(encoding="utf-8")
    catalog_path.write_text(
        source.replace("[targets.leaf]", 'unexpected = "root"\n\n[targets.leaf]'),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="catalog contains unknown fields"):
        load_design_target_catalog(Project.from_project_root(tmp_path))

    catalog_path.write_text(
        source.replace(
            'description = "Test leaf"',
            'description = "Test leaf"\nunexpected = "row"',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="targets.leaf contains unknown fields"):
        load_design_target_catalog(Project.from_project_root(tmp_path))


def test_design_catalog_rejects_unsafe_entrypoints_and_routing_overrides(
    tmp_path: Path,
) -> None:
    _catalog_project(tmp_path)
    catalog_path = tmp_path / "ip/example/configs/flows/design_targets.toml"
    catalog_path.write_text(
        '''
schema = 1
contract_kind = "flow-design-registry"
path_scope = "owner"
owner = "example"

[targets.escape]
description = "Unsafe"
kind = "script"
entrypoint = "../run.py"
spec_argument = "--spec"
spec = "ip/example/leaf/design.toml"
[targets.escape.modes]
topology = []
[targets.escape.routes]
topology = ["design-checks", "escape-topology"]
''',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="canonical project-relative path"):
        load_design_target_catalog(Project.from_project_root(tmp_path))

    catalog_path.write_text(
        '''
schema = 1
contract_kind = "flow-design-registry"
path_scope = "owner"
owner = "example"

[targets.leaf]
description = "Routing override"
kind = "script"
entrypoint = "ip/example/leaf/run.py"
spec_argument = "--spec"
spec = "ip/example/leaf/design.toml"
[targets.leaf.modes]
topology = ["--mode", "sync"]
[targets.leaf.routes]
topology = ["design-checks", "leaf-topology"]
''',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="cannot override routing argument --mode"):
        load_design_target_catalog(Project.from_project_root(tmp_path))


def test_design_catalog_routes_dv_owned_modules_without_script_wrappers(
    tmp_path: Path,
) -> None:
    _catalog_project(tmp_path)
    module = tmp_path / "ip/example/dv/transaction.py"
    module.parent.mkdir(parents=True)
    module.write_text("raise SystemExit(0)\n", encoding="utf-8")
    catalog_path = tmp_path / "ip/example/configs/flows/design_targets.toml"
    catalog_path.write_text(
        '''
schema = 1
contract_kind = "flow-design-registry"
path_scope = "owner"
owner = "example"

[targets.dv-check]
description = "DV-owned entrypoint"
kind = "module"
entrypoint = "ip.example.dv.transaction"
[targets.dv-check.modes]
contract = []
[targets.dv-check.routes]
contract = ["design-checks", "dv-contract"]
''',
        encoding="utf-8",
    )
    target = load_design_target_catalog(Project.from_project_root(tmp_path)).get("dv-check")
    assert target.command("contract") == (
        sys.executable,
        "-m",
        "ip.example.dv.transaction",
        "--mode",
        "contract",
    )

    catalog_path.write_text(
        '''
schema = 1
contract_kind = "flow-design-registry"
path_scope = "owner"
owner = "example"

[targets.external]
description = "Unowned module"
kind = "module"
entrypoint = "unowned.runner"
[targets.external.modes]
contract = []
[targets.external.routes]
contract = ["design-checks", "external-contract"]
''',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="project-owned module"):
        load_design_target_catalog(Project.from_project_root(tmp_path))

    catalog_path.write_text(
        '''
schema = 1
contract_kind = "flow-design-registry"
path_scope = "owner"
owner = "example"

[targets.future-cli]
description = "Undeclared shared CLI"
kind = "module"
entrypoint = "sigilicon.cli.future_command"
[targets.future-cli.modes]
contract = []
[targets.future-cli.routes]
contract = ["design-checks", "future-contract"]
''',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="project-owned module"):
        load_design_target_catalog(Project.from_project_root(tmp_path))


def test_design_catalog_cannot_route_through_another_owner(
    tmp_path: Path,
) -> None:
    _catalog_project(tmp_path)
    neighbor = tmp_path / "ip/neighbor"
    neighbor.mkdir(parents=True)
    (neighbor / "run.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    (neighbor / "design.toml").write_text("# neighbor spec\n", encoding="utf-8")
    write_component_owner(tmp_path, "neighbor", filesets={})
    catalog_path = tmp_path / "ip/example/configs/flows/design_targets.toml"
    original = catalog_path.read_text(encoding="utf-8")

    catalog_path.write_text(
        original.replace(
            'entrypoint = "ip/example/leaf/run.py"',
            'entrypoint = "ip/neighbor/run.py"',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="owner 'example' root"):
        load_design_target_catalog(Project.from_project_root(tmp_path))

    catalog_path.write_text(
        original.replace(
            'spec = "ip/example/leaf/design.toml"',
            'spec = "ip/neighbor/design.toml"',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="owner 'example' root"):
        load_design_target_catalog(Project.from_project_root(tmp_path))

    catalog_path.write_text(
        original.replace('kind = "script"', 'kind = "module"').replace(
            'entrypoint = "ip/example/leaf/run.py"',
            'entrypoint = "ip.neighbor.run"',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="owner 'example' root"):
        load_design_target_catalog(Project.from_project_root(tmp_path))


def test_design_cli_lists_targets_without_executing_a_runner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _catalog_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        Project,
        "from_project_root",
        classmethod(
            lambda cls, root: pytest.fail(
                "design CLI must pass its already-loaded Project to the catalog"
            )
        ),
    )
    events: list[object] = []

    assert (
        flow_cli.main(["design", "list"])
        == 0
    )

    assert capsys.readouterr().out == (
        "leaf\ttopology,sync\tip/example/leaf/design.toml\n"
    )
    assert events == []


def test_design_cli_runs_the_cataloged_typed_flow_route(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _runner, _spec = _catalog_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    events: list[object] = []

    class FakeProjectFlow:
        def __init__(self, project, owner):
            events.append(("init", project.project_root, owner))

        def plan(self, request):
            selection = request.selection
            events.append(("plan-design", selection.target, selection.mode))
            return "planned"

        def run(self, planned, environment, *, run_id):
            events.append(("run", planned, environment, run_id))
            return SimpleNamespace(
                flow_id="design-checks",
                target="leaf-sync",
                run_id="fixture-run",
            )

        def read_result(self, *, flow, target, run_id):
            events.append(("read", flow, target, run_id))
            return {"status": "accepted", "run_id": run_id}

    monkeypatch.setattr(flow_cli, "ProjectFlow", FakeProjectFlow)

    assert (
        flow_cli.main(
            ["design", "run", "leaf", "sync", "--run-id", "fixture-run"],
        )
        == 0
    )
    assert events[0] == ("init", tmp_path.resolve(), "example")
    assert events[1] == ("plan-design", "leaf", "sync")
    assert events[2][0] == "run"
    assert events[2][3] == "fixture-run"
    assert events[3] == (
        "read",
        "design-checks",
        "leaf-sync",
        "fixture-run",
    )
    assert '"status": "accepted"' in capsys.readouterr().out


def test_design_cli_rejects_unknown_modes_and_extra_routing_arguments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _catalog_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        flow_cli.main(["design", "run", "leaf", "missing"])
    assert "does not support mode 'missing'" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        flow_cli.main(["design", "run", "leaf", "topology", "--mode=sync"])
    assert "unrecognized arguments" in capsys.readouterr().err
