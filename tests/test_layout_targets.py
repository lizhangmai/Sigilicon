from __future__ import annotations

from pathlib import Path

import pytest

from sigilicon.cli import flow as flow_cli
from sigilicon.workflows.layout_targets import load_layout_target_catalog


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
    return spec


def test_layout_target_catalog_can_start_empty(tmp_path: Path) -> None:
    flows = tmp_path / "ip/example/configs/flows"
    flows.mkdir(parents=True)
    (flows / "layout_targets.toml").write_text(
        "schema = 1\ncontract_kind = \"flow-layout-registry\"\npath_scope = \"owner\"\nowner = \"example\"\n\n[targets]\n",
        encoding="utf-8",
    )

    assert load_layout_target_catalog(tmp_path).targets == ()


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

    assert flow_cli.main(["layout", "list"], client_factory=object) == 0

    assert capsys.readouterr().out == "leaf\tcheck,generate,verify\tip/example/leaf.toml\n"


def test_layout_cli_delegates_to_existing_generation_and_verification_clis(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = _catalog_project(tmp_path).resolve()
    monkeypatch.chdir(tmp_path)
    events: list[tuple[str, list[str], object]] = []

    def generate(argv, *, client_factory):
        events.append(("generate", list(argv), client_factory))
        return 17

    def verify(argv, *, client_factory):
        events.append(("verify", list(argv), client_factory))
        return 23

    monkeypatch.setattr(flow_cli, "generate_layout_main", generate)
    monkeypatch.setattr(flow_cli, "verify_layout_main", verify)

    assert flow_cli.main(["layout", "check", "leaf"], client_factory=object) == 17
    assert (
        flow_cli.main(
            ["layout", "generate", "leaf", "--timeout", "91"],
            client_factory=object,
        )
        == 17
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
            client_factory=object,
        )
        == 23
    )

    assert events == [
        ("generate", ["--spec", str(spec), "--preview"], object),
        ("generate", ["--spec", str(spec), "--timeout", "91"], object),
        (
            "verify",
            [
                "--spec",
                str(spec),
                "--check",
                "lvs",
                "--xstream-timeout",
                "92",
                "--calibre-timeout",
                "93",
            ],
            object,
        ),
    ]


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
