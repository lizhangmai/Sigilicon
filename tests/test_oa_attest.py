from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from sigilicon.execution import RunStore

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
    calls: list[tuple[str, str]] = []

    class TypedFlow:
        def __init__(self, _project, owner: str) -> None:
            assert owner == "fixture"

        def plan(self, target: str, operation: str):
            calls.append((target, operation))

            class Execution:
                owner = "fixture"
                target = "native"
                operation = "tb-main-l2"

                def run(self, environment):
                    assert set(environment.capabilities) == {
                        "tool.virtuoso-bridge",
                        "license.cadence-oa",
                    }
                    return SimpleNamespace(run_id="a" * 32)

            return Execution()

    monkeypatch.setattr(flow_cli, "ProjectRunner", TypedFlow)
    monkeypatch.setattr(
        RunStore,
        "read",
        lambda _self, **values: {
            "flow": "native",
            "target": "tb-main-l2",
            "run_id": values["run_id"],
            "status": "accepted",
        },
    )
    assert (
        flow_cli.main(
            [
                "oa",
                "simulate",
                "--owner",
                "fixture",
                "--target",
                "tb-main",
                "--operation",
                "electrical",
            ],
            client_factory=lambda: pytest.fail("CLI must not start a direct backend"),
        )
        == 0
    )
    assert calls == [("tb-main", "electrical")]
    assert "OA Maestro Flow completed: native/tb-main-l2" in capsys.readouterr().out
