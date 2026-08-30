from __future__ import annotations

from pathlib import Path

import pytest
import sigilicon.domain.repository as repository_module

from sigilicon.cli import flow as flow_cli
from sigilicon.domain.repository import Project
from sigilicon.workflows.layout_targets import load_layout_target_catalog

from conftest import write_component_owner


def _catalog_project(
    tmp_path: Path,
    *,
    actions: str = '"check", "generate", "verify"',
) -> Path:
    routes = ""
    if "generate" in actions:
        routes += 'routes.generate = ["layout-validation", "leaf-generate"]\n'
    if "verify" in actions:
        routes += (
            'routes.verify-drc = ["layout-validation", "leaf-verify-drc"]\n'
            'routes.verify-lvs = ["layout-validation", "leaf-verify-lvs"]\n'
            'routes.verify-all = ["layout-validation", "leaf-verify-all"]\n'
        )
    flow_root = tmp_path / "ip/example/configs/flows"
    flow_root.mkdir(parents=True)
    (tmp_path / "ip/example").mkdir(parents=True, exist_ok=True)
    spec = tmp_path / "ip/example/leaf.toml"
    spec.write_text("# delegated layout spec\n", encoding="utf-8")
    (flow_root / "layout_targets.toml").write_text(
        f'''
schema = 1
contract_kind = "flow-layout-registry"
path_scope = "owner"
owner = "example"

[targets.leaf]
description = "Test leaf"
spec = "ip/example/leaf.toml"
actions = [{actions}]
{routes}
''',
        encoding="utf-8",
    )
    write_component_owner(
        tmp_path,
        "example",
        filesets={
            "flow": ("ip/example/configs/flows/layout_targets.toml",),
        },
    )
    return spec


def test_layout_target_catalog_can_start_empty(tmp_path: Path) -> None:
    flows = tmp_path / "ip/example/configs/flows"
    flows.mkdir(parents=True)
    (flows / "layout_targets.toml").write_text(
        "schema = 1\ncontract_kind = \"flow-layout-registry\"\npath_scope = \"owner\"\nowner = \"example\"\n\n[targets]\n",
        encoding="utf-8",
    )
    write_component_owner(
        tmp_path,
        "example",
        filesets={
            "flow": ("ip/example/configs/flows/layout_targets.toml",),
        },
    )

    catalog = load_layout_target_catalog(Project.from_project_root(tmp_path))

    assert catalog.paths == ((flows / "layout_targets.toml").resolve(),)
    assert catalog.targets == ()


def test_layout_target_catalog_is_an_optional_project_domain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = Project.from_project_root(tmp_path)

    catalog = load_layout_target_catalog(project=project)

    assert catalog.project is project
    assert catalog.paths == ()
    assert catalog.targets == ()

    monkeypatch.chdir(tmp_path)
    assert flow_cli.main(["layout", "list", "--json"], client_factory=object) == 0
    assert capsys.readouterr().out == "[]\n"


def test_layout_target_catalog_binds_explicit_project(tmp_path: Path) -> None:
    _catalog_project(tmp_path)
    project = Project.from_project_root(tmp_path)

    catalog = load_layout_target_catalog(project=project)

    assert catalog.project is project
    assert catalog.project_root == tmp_path


def test_layout_target_loader_reads_its_catalog_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _catalog_project(tmp_path)
    catalog_path = (
        tmp_path / "ip/example/configs/flows/layout_targets.toml"
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

    catalog = load_layout_target_catalog(project=project)

    assert len(catalog.targets) == 1
    assert reads == 1


def test_layout_catalog_preserves_project_identity(tmp_path: Path) -> None:
    _catalog_project(tmp_path)
    project = Project.from_file(tmp_path / "sigilicon.toml")

    catalog = load_layout_target_catalog(project=project)

    assert catalog.project is project


def test_layout_catalog_rejects_unknown_fields(tmp_path: Path) -> None:
    _catalog_project(tmp_path)
    catalog_path = tmp_path / "ip/example/configs/flows/layout_targets.toml"
    source = catalog_path.read_text(encoding="utf-8")
    catalog_path.write_text(
        source.replace("[targets.leaf]", 'unexpected = "root"\n\n[targets.leaf]'),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="catalog contains unknown fields"):
        load_layout_target_catalog(Project.from_project_root(tmp_path))

    catalog_path.write_text(
        source.replace(
            'description = "Test leaf"',
            'description = "Test leaf"\nunexpected = "row"',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="targets.leaf contains unknown fields"):
        load_layout_target_catalog(Project.from_project_root(tmp_path))


def test_layout_catalog_rejects_unsafe_specs_and_invalid_actions(tmp_path: Path) -> None:
    _catalog_project(tmp_path)
    catalog_path = tmp_path / "ip/example/configs/flows/layout_targets.toml"
    catalog_path.write_text(
        '''
schema = 1
contract_kind = "flow-layout-registry"
path_scope = "owner"
owner = "example"

[targets.escape]
description = "Unsafe"
spec = "../outside.toml"
actions = ["check"]
''',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="canonical project-relative path"):
        load_layout_target_catalog(Project.from_project_root(tmp_path))

    catalog_path.write_text(
        '''
schema = 1
contract_kind = "flow-layout-registry"
path_scope = "owner"
owner = "example"

[targets.leaf]
description = "Bad action"
spec = "ip/example/leaf.toml"
actions = ["check", "publish"]
''',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must contain only"):
        load_layout_target_catalog(Project.from_project_root(tmp_path))


def test_layout_catalog_cannot_route_to_another_owner_spec(
    tmp_path: Path,
) -> None:
    _catalog_project(tmp_path)
    neighbor = tmp_path / "ip/neighbor"
    neighbor.mkdir(parents=True)
    (neighbor / "layout.toml").write_text("# neighbor spec\n", encoding="utf-8")
    write_component_owner(tmp_path, "neighbor", filesets={})
    catalog_path = tmp_path / "ip/example/configs/flows/layout_targets.toml"
    catalog_path.write_text(
        catalog_path.read_text(encoding="utf-8").replace(
            'spec = "ip/example/leaf.toml"',
            'spec = "ip/neighbor/layout.toml"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="owner 'example' root"):
        load_layout_target_catalog(Project.from_project_root(tmp_path))


def test_layout_cli_lists_targets_without_opening_a_tool_client(
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
                "layout CLI must pass its already-loaded Project to the catalog"
            )
        ),
    )

    assert flow_cli.main(["layout", "list"], client_factory=object) == 0

    assert capsys.readouterr().out == "leaf\tcheck,generate,verify\tip/example/leaf.toml\n"


def test_layout_cli_runs_the_project_bound_layout_workflow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    spec = _catalog_project(tmp_path).resolve()
    canonical_project = Project.from_project_root(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        Project,
        "from_file",
        classmethod(lambda cls, path: canonical_project),
    )
    events: list[tuple[str, Project, object | None]] = []
    plan = type("Plan", (), {"canonical_json": lambda self: '{"plan":true}\n'})()
    client = object()

    def preview(path: Path, *, project: Project) -> object:
        assert path == spec
        events.append(("check", project, None))
        return type("Preview", (), {"plan": plan})()

    class FakeProjectFlow:
        def __init__(self, project, owner, *, client_factory):
            assert project is canonical_project
            assert owner == "example"
            assert client_factory() is client
            self.operation = ""

        def plan_layout(self, catalog, *, target, operation):
            assert catalog.project is canonical_project
            assert target == "leaf"
            self.operation = operation
            events.append((operation, canonical_project, client))
            return object()

        def run(self, planned, environment, *, run_id=None):
            assert planned is not None
            assert environment is not None
            assert run_id is None
            return type(
                "Result",
                (),
                {
                    "flow_id": "layout-validation",
                    "target": f"leaf-{self.operation}",
                    "run_id": "test-run",
                },
            )()

        def read_result(self, *, flow, target, run_id):
            assert flow == "layout-validation"
            assert run_id == "test-run"
            return {"status": "accepted", "target": target}

    monkeypatch.setattr(flow_cli, "plan_layout_spec", preview)
    monkeypatch.setattr(flow_cli, "ProjectFlow", FakeProjectFlow)
    monkeypatch.setattr(
        flow_cli,
        "_layout_execution_environment",
        lambda args, **kwargs: object(),
    )

    assert (
        flow_cli.main(
            ["layout", "check", "leaf"],
            client_factory=lambda: pytest.fail("layout check must not open a client"),
        )
        == 0
    )
    assert (
        flow_cli.main(
            ["layout", "generate", "leaf"],
            client_factory=lambda: client,
        )
        == 0
    )
    assert (
        flow_cli.main(
            [
                "layout",
                "verify",
                "leaf",
                "--check",
                "lvs",
            ],
            client_factory=lambda: client,
        )
        == 0
    )

    assert [kind for kind, _project, _client in events] == [
        "check",
        "generate",
        "verify-lvs",
    ]
    assert all(
        project is canonical_project for _kind, project, _client in events
    )
    output = capsys.readouterr().out
    assert '{"plan":true}\n' in output
    assert '"status": "accepted"' in output
    assert '"target": "leaf-verify-lvs"' in output


def test_layout_cli_refuses_an_action_not_enabled_for_the_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _catalog_project(tmp_path, actions='"check", "generate"')
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as raised:
        flow_cli.main(["layout", "verify", "leaf"], client_factory=object)

    assert raised.value.code == 1
    assert "does not support 'verify'" in capsys.readouterr().err
