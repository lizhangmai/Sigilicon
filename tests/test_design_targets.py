from __future__ import annotations

from pathlib import Path
import sys

import pytest

from sigilicon.cli import flow as flow_cli
from sigilicon.workflows.design_targets import load_design_target_catalog


def _catalog_project(tmp_path: Path) -> tuple[Path, Path]:
    (tmp_path / "configs").mkdir()
    design = tmp_path / "ip/example/leaf"
    design.mkdir(parents=True)
    runner = design / "run.py"
    runner.write_text("raise SystemExit(0)\n", encoding="utf-8")
    spec = design / "design.toml"
    spec.write_text("# delegated design spec\n", encoding="utf-8")
    (tmp_path / "configs" / "design_targets.toml").write_text(
        '''
schema = 1
contract_kind = "flow-design-registry"
path_scope = "repository"
owner = "repository"

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
    return runner.resolve(), spec.resolve()


def test_design_target_catalog_can_start_empty(tmp_path: Path) -> None:
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "design_targets.toml").write_text(
        "schema = 1\ncontract_kind = \"flow-design-registry\"\npath_scope = \"repository\"\nowner = \"repository\"\n\n[targets]\n",
        encoding="utf-8",
    )

    assert load_design_target_catalog(tmp_path).targets == ()


def test_design_catalog_rejects_unsafe_entrypoints_and_routing_overrides(
    tmp_path: Path,
) -> None:
    _catalog_project(tmp_path)
    catalog_path = tmp_path / "configs" / "design_targets.toml"
    catalog_path.write_text(
        '''
schema = 1
contract_kind = "flow-design-registry"
path_scope = "repository"
owner = "repository"

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
    with pytest.raises(ValueError, match="must stay below the project root"):
        load_design_target_catalog(tmp_path)

    catalog_path.write_text(
        '''
schema = 1
contract_kind = "flow-design-registry"
path_scope = "repository"
owner = "repository"

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
    catalog_path = tmp_path / "configs" / "design_targets.toml"
    catalog_path.write_text(
        '''
schema = 1
contract_kind = "flow-design-registry"
path_scope = "repository"
owner = "repository"

[targets.dv-check]
description = "DV-owned entrypoint"
kind = "module"
entrypoint = "soc.example.dv.transaction"
[targets.dv-check.modes]
contract = []
''',
        encoding="utf-8",
    )
    target = load_design_target_catalog(tmp_path).get("dv-check")
    assert target.command("contract") == (
        sys.executable,
        "-m",
        "soc.example.dv.transaction",
        "--mode",
        "contract",
    )

    catalog_path.write_text(
        '''
schema = 1
contract_kind = "flow-design-registry"
path_scope = "repository"
owner = "repository"

[targets.external]
description = "Unowned module"
kind = "module"
entrypoint = "unowned.runner"
[targets.external.modes]
contract = []
''',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="project-owned module prefix"):
        load_design_target_catalog(tmp_path)


def test_design_cli_lists_targets_without_executing_a_runner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _catalog_project(tmp_path)
    monkeypatch.chdir(tmp_path)
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
