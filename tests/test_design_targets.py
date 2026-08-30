from __future__ import annotations

from pathlib import Path
import sys
import tomllib

import pytest

from sigilicon.cli import flow as flow_cli
from sigilicon.domain.repository import Project
from sigilicon.workflows.design_targets import load_design_target_catalog
from sigilicon.workflows.project_targets import ProjectTargets

from conftest import write_component_owner


def _catalog_project(tmp_path: Path) -> tuple[Path, Path]:
    flow_root = tmp_path / "ip/example/configs/flows"
    flow_root.mkdir(parents=True)
    design = tmp_path / "ip/example/leaf"
    design.mkdir(parents=True)
    runner = design / "run.py"
    runner.write_text("raise SystemExit(0)\n", encoding="utf-8")
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
topology = []
sync = ["--overwrite"]
''',
        encoding="utf-8",
    )
    write_component_owner(
        tmp_path,
        "example",
        filesets={
            "flow": ("ip/example/configs/flows/design_targets.toml",),
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

    catalog = load_design_target_catalog(tmp_path)

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


def test_design_target_catalog_reuses_explicit_project(tmp_path: Path) -> None:
    _catalog_project(tmp_path)
    project = Project.from_project_root(tmp_path)

    catalog = load_design_target_catalog(project=project)

    assert catalog.project is project
    assert catalog.project_root == tmp_path

    with pytest.raises(ValueError, match="root disagrees with explicit Project"):
        load_design_target_catalog(tmp_path / "other", project=project)


def test_design_target_loader_reads_its_catalog_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _catalog_project(tmp_path)
    catalog_path = (
        tmp_path / "ip/example/configs/flows/design_targets.toml"
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

    catalog = load_design_target_catalog(project=project)

    assert len(catalog.targets) == 1
    assert reads == 1


def test_project_targets_preserves_design_project_identity(tmp_path: Path) -> None:
    _catalog_project(tmp_path)

    targets = ProjectTargets.from_file(tmp_path / "sigilicon.toml")

    assert targets.design().project is targets.project


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
        load_design_target_catalog(tmp_path)


def test_design_catalog_rejects_unknown_fields(tmp_path: Path) -> None:
    _catalog_project(tmp_path)
    catalog_path = tmp_path / "ip/example/configs/flows/design_targets.toml"
    source = catalog_path.read_text(encoding="utf-8")
    catalog_path.write_text(
        source.replace("[targets.leaf]", 'unexpected = "root"\n\n[targets.leaf]'),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="catalog contains unknown fields"):
        load_design_target_catalog(tmp_path)

    catalog_path.write_text(
        source.replace(
            'description = "Test leaf"',
            'description = "Test leaf"\nunexpected = "row"',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="targets.leaf contains unknown fields"):
        load_design_target_catalog(tmp_path)


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
''',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="canonical project-relative path"):
        load_design_target_catalog(tmp_path)

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
''',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="cannot override routing argument --mode"):
        load_design_target_catalog(tmp_path)


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
''',
        encoding="utf-8",
    )
    target = load_design_target_catalog(tmp_path).get("dv-check")
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
''',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="project-owned module"):
        load_design_target_catalog(tmp_path)

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
''',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="project-owned module"):
        load_design_target_catalog(tmp_path)


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
        load_design_target_catalog(tmp_path)

    catalog_path.write_text(
        original.replace(
            'spec = "ip/example/leaf/design.toml"',
            'spec = "ip/neighbor/design.toml"',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="owner 'example' root"):
        load_design_target_catalog(tmp_path)

    catalog_path.write_text(
        original.replace('kind = "script"', 'kind = "module"').replace(
            'entrypoint = "ip/example/leaf/run.py"',
            'entrypoint = "ip.neighbor.run"',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="owner 'example' root"):
        load_design_target_catalog(tmp_path)


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
        flow_cli.main(
            ["design", "list"],
            process_executor=lambda *_args: events.append(_args),
        )
        == 0
    )

    assert capsys.readouterr().out == (
        "leaf\ttopology,sync\tip/example/leaf/design.toml\n"
    )
    assert events == []


def test_design_cli_execs_the_existing_runner_with_catalog_defaults_and_extras(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _runner, _spec = _catalog_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    events: list[tuple[str, list[str]]] = []

    working_directories: list[Path] = []

    def execute(path: str, argv: list[str]) -> None:
        events.append((path, argv))
        working_directories.append(Path.cwd())

    assert (
        flow_cli.main(
            ["design", "run", "leaf", "sync", "--", "--timeout", "91"],
            process_executor=execute,
        )
        == 0
    )

    command = [
        sys.executable,
        "ip/example/leaf/run.py",
        "--spec",
        "ip/example/leaf/design.toml",
        "--mode",
        "sync",
        "--overwrite",
        "--timeout",
        "91",
    ]
    assert events == [(sys.executable, command)]
    assert working_directories == [tmp_path.resolve()]


def test_design_cli_rejects_unknown_modes_and_extra_routing_arguments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _catalog_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    events: list[object] = []

    with pytest.raises(SystemExit):
        flow_cli.main(
            ["design", "run", "leaf", "missing"],
            process_executor=lambda *_args: events.append(_args),
        )
    assert "does not support mode 'missing'" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        flow_cli.main(
            ["design", "run", "leaf", "topology", "--", "--mode=sync"],
            process_executor=lambda *_args: events.append(_args),
        )
    assert "cannot override routing argument --mode" in capsys.readouterr().err
    assert events == []
