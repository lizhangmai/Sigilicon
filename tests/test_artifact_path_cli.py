from __future__ import annotations

from pathlib import Path

from sigilicon.cli.artifact_path import main


def test_cli_resolves_and_creates_runs_through_artifact_layout(
    tmp_path: Path,
    capsys,
) -> None:
    identity = "1" * 32
    assert main(
        [
            "run",
            "owner",
            "target",
            "flow",
            "variant",
            "--project-root",
            str(tmp_path),
            "--identity",
            identity,
            "--create",
            "--role",
            "work",
        ]
    ) == 0

    work = Path(capsys.readouterr().out.strip())
    root = work.parent
    assert work == (
        tmp_path / "artifacts/runs/owner/target/flow/variant" / identity / "work"
    )
    assert {path.name for path in root.iterdir()} == {
        "inputs",
        "work",
        "outputs",
        "logs",
    }


def test_cli_resolves_named_exports_without_creating_them(
    tmp_path: Path,
    capsys,
) -> None:
    assert main(
        [
            "export",
            "owner",
            "netlist",
            "mapped.v",
            "--project-root",
            str(tmp_path),
        ]
    ) == 0

    exported = Path(capsys.readouterr().out.strip())
    assert exported == tmp_path / "artifacts/exports/owner/netlist/mapped.v"
    assert not exported.exists()
