from __future__ import annotations

from pathlib import Path
import tomllib

import pytest

from sigilicon.cli import flow as flow_cli
from sigilicon.domain.repository import Project
from sigilicon.workflows.layout_targets import load_layout_target_catalog
from sigilicon.workflows.project_layout import ProjectLayoutWorkflow
from sigilicon.workflows.project_targets import ProjectTargets

from conftest import write_component_owner


def _catalog_project(
    tmp_path: Path,
    *,
    actions: str = '"check", "generate", "verify"',
) -> Path:
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

    catalog = load_layout_target_catalog(tmp_path)

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


def test_layout_target_catalog_reuses_explicit_project(tmp_path: Path) -> None:
    _catalog_project(tmp_path)
    project = Project.from_project_root(tmp_path)

    catalog = load_layout_target_catalog(project=project)

    assert catalog.project is project
    assert catalog.project_root == tmp_path

    with pytest.raises(ValueError, match="root disagrees with explicit Project"):
        load_layout_target_catalog(tmp_path / "other", project=project)


def test_layout_target_loader_reads_its_catalog_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _catalog_project(tmp_path)
    catalog_path = (
        tmp_path / "ip/example/configs/flows/layout_targets.toml"
    ).resolve()
    reads = 0
    original_load = tomllib.load

    def counted_load(stream):
        nonlocal reads
        if Path(stream.name).resolve() == catalog_path:
            reads += 1
        return original_load(stream)

    monkeypatch.setattr(tomllib, "load", counted_load)
    project = Project.from_project_root(tmp_path)

    catalog = load_layout_target_catalog(project=project)

    assert len(catalog.targets) == 1
    assert reads == 1


def test_project_targets_preserves_layout_project_identity(tmp_path: Path) -> None:
    _catalog_project(tmp_path)

    targets = ProjectTargets.from_file(tmp_path / "sigilicon.toml")

    assert targets.layout().project is targets.project


def test_layout_catalog_rejects_unknown_fields(tmp_path: Path) -> None:
    _catalog_project(tmp_path)
    catalog_path = tmp_path / "ip/example/configs/flows/layout_targets.toml"
    source = catalog_path.read_text(encoding="utf-8")
    catalog_path.write_text(
        source.replace("[targets.leaf]", 'unexpected = "root"\n\n[targets.leaf]'),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="catalog contains unknown fields"):
        load_layout_target_catalog(tmp_path)

    catalog_path.write_text(
        source.replace(
            'description = "Test leaf"',
            'description = "Test leaf"\nunexpected = "row"',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="targets.leaf contains unknown fields"):
        load_layout_target_catalog(tmp_path)


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
    with pytest.raises(ValueError, match="must stay below the project root"):
        load_layout_target_catalog(tmp_path)

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
        load_layout_target_catalog(tmp_path)


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
    events: list[tuple[str, ProjectLayoutWorkflow, object | None]] = []
    plan = type("Plan", (), {"canonical_json": lambda self: '{"plan":true}\n'})()
    layout_spec = type(
        "LayoutSpec",
        (),
        {"library": "example", "cell": "leaf", "view": "layout"},
    )()
    generation = type(
        "Generation",
        (),
        {"instance_count": 3, "manifest_path": tmp_path / "generation.json"},
    )()
    verification = type(
        "Verification",
        (),
        {
            "check": "lvs",
            "passed": True,
            "details": {},
            "manifest_path": tmp_path / "lvs.json",
        },
    )()
    client = object()

    def preview(workflow: ProjectLayoutWorkflow, path: Path) -> object:
        assert path == spec
        events.append(("check", workflow, None))
        return type("Preview", (), {"plan": plan})()

    def generate(
        workflow: ProjectLayoutWorkflow,
        path: Path,
        oa_client: object,
        *,
        timeout: int,
    ) -> tuple[object, object]:
        assert path == spec
        assert oa_client is client
        assert timeout == 91
        events.append(("generate", workflow, oa_client))
        return layout_spec, generation

    def verify(
        workflow: ProjectLayoutWorkflow,
        path: Path,
        oa_client: object,
        *,
        checks: tuple[str, ...],
        xstream_timeout: int,
        calibre_timeout: int,
    ) -> tuple[tuple[object, object], ...]:
        assert path == spec
        assert oa_client is client
        assert checks == ("lvs",)
        assert xstream_timeout == 92
        assert calibre_timeout == 93
        events.append(("verify", workflow, oa_client))
        return ((layout_spec, verification),)

    monkeypatch.setattr(ProjectLayoutWorkflow, "plan", preview)
    monkeypatch.setattr(ProjectLayoutWorkflow, "generate", generate)
    monkeypatch.setattr(ProjectLayoutWorkflow, "verify", verify)

    assert (
        flow_cli.main(
            ["layout", "check", "leaf"],
            client_factory=lambda: pytest.fail("layout check must not open a client"),
        )
        == 0
    )
    assert (
        flow_cli.main(
            ["layout", "generate", "leaf", "--timeout", "91"],
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
                "--xstream-timeout",
                "92",
                "--calibre-timeout",
                "93",
            ],
            client_factory=lambda: client,
        )
        == 0
    )

    assert [kind for kind, _workflow, _client in events] == [
        "check",
        "generate",
        "verify",
    ]
    assert all(
        workflow.project is canonical_project for _kind, workflow, _client in events
    )
    output = capsys.readouterr().out
    assert '{"plan":true}\n' in output
    assert "[generated] example/leaf/layout instances=3" in output
    assert "[lvs] PASS example/leaf/layout" in output


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
