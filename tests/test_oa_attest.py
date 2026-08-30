from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from sigilicon.cli import flow as flow_cli
from sigilicon.cli.flow import _parser
from conftest import write_component_owner


def _write_oa_owner(root: Path) -> None:
    manifest = root / "ip/fixture/configs/oa.toml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        '''schema = 1
contract_kind = "oa-assembly"
path_scope = "owner"
owner = "fixture"
''',
        encoding="utf-8",
    )
    write_component_owner(
        root,
        "fixture",
        filesets={"oa_source": ("ip/fixture/configs/oa.toml",)},
    )


def test_attest_is_a_current_testbench_setup_check() -> None:
    args = _parser().parse_args(
        [
            "oa",
            "attest",
            "--owner",
            "fixture",
            "--testbench",
            "tb_main",
        ]
    )

    assert args.action == "attest"
    assert args.testbench == "tb_main"
    assert args.timeout == 300


def test_simulation_has_no_temporary_work_retention_option() -> None:
    args = _parser().parse_args(
        [
            "oa",
            "simulate",
            "--owner",
            "fixture",
            "--testbench",
            "tb_main",
        ]
    )

    assert not hasattr(args, "keep_work")
    assert not hasattr(args, "timeout")
    with pytest.raises(SystemExit):
        _parser().parse_args(
            [
                "oa",
                "simulate",
                "--owner",
                "fixture",
                "--testbench",
                "tb_main",
                "--keep-work",
            ]
        )


def test_rebuild_can_select_exactly_one_design_cell() -> None:
    args = _parser().parse_args(
        [
            "oa",
            "rebuild",
            "--owner",
            "fixture",
            "--cell",
            "FIXTURE_CELL",
        ]
    )

    assert args.cell == "FIXTURE_CELL"
    assert args.testbench is None


def test_cli_attest_reports_current_check_without_prior_state(
    monkeypatch, capsys, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_oa_owner(tmp_path)
    payload = {
        "passed": True,
        "library": "fixture_lib",
        "testbench": "tb_main",
        "simulation_run": False,
        "product_qualification_conclusion": False,
    }
    monkeypatch.setattr(
        flow_cli.ProjectOaWorkflow,
        "attest",
        lambda *_args, **_kwargs: payload,
    )

    assert (
        flow_cli.main(
            [
                "oa",
                "attest",
                "--owner",
                "fixture",
                "--testbench",
                "tb_main",
            ],
            client_factory=lambda: object(),
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "OA setup check passed: fixture_lib/tb_main" in output
    assert "manifest=" not in output


def test_cli_simulate_resolves_and_runs_the_unique_typed_target(
    monkeypatch, capsys, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_oa_owner(tmp_path)
    node = SimpleNamespace(
        node_id="simulate-main",
        action_kind="native-oa.simulate",
        config={"testbench": "tb_main"},
    )
    target = SimpleNamespace(target_id="tb-main-l2", goals=("simulate-main",))
    selection = SimpleNamespace(
        spec=SimpleNamespace(nodes=(node,), targets=(target,))
    )
    catalog = SimpleNamespace(entries=(SimpleNamespace(flow_id="native"),))
    calls: list[tuple[str, str]] = []

    class TypedFlow:
        def __init__(self, _project, owner: str) -> None:
            assert owner == "fixture"

        def catalog(self):
            return catalog

        def plan(self, *, flow: str, target: str):
            calls.append((flow, target))
            return object()

        def run(self, _planned, environment):
            assert set(environment.capabilities) == {
                "tool.virtuoso-bridge",
                "license.cadence-oa",
            }
            return SimpleNamespace(flow_id="native", target="tb-main-l2", run_id="a" * 32)

        def read_result(self, *, flow: str, target: str, run_id: str):
            assert (flow, target, run_id) == ("native", "tb-main-l2", "a" * 32)
            return {
                "flow": flow,
                "target": target,
                "run_id": run_id,
                "status": "accepted",
            }

    monkeypatch.setattr(flow_cli, "ProjectFlow", TypedFlow)
    monkeypatch.setattr(
        flow_cli,
        "resolve_catalog_selection",
        lambda *_args, **_kwargs: selection,
    )
    monkeypatch.setattr(
        flow_cli.ProjectOaWorkflow,
        "simulate",
        lambda *_args, **_kwargs: pytest.fail("standalone lifecycle must not run"),
    )

    assert (
        flow_cli.main(
            [
                "oa",
                "simulate",
                "--owner",
                "fixture",
                "--testbench",
                "tb_main",
            ],
            client_factory=lambda: pytest.fail("CLI must not start a direct backend"),
        )
        == 0
    )
    assert calls == [("native", "tb-main-l2")]
    assert "OA Maestro Flow completed: native/tb-main-l2" in capsys.readouterr().out
