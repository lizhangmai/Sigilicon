from __future__ import annotations

from pathlib import Path

from sigilicon.cli.artifact_path import main


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
