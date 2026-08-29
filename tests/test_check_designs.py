from __future__ import annotations

from pathlib import Path

import pytest

from conftest import write_component_owner
import sigilicon.domain.repository as repository_module
import sigilicon.workflows.repository_checks as repository_checks_module
from sigilicon.cli.check_designs import main as check_designs_main


def test_check_designs_parses_the_project_manifest_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    contract = (tmp_path / "sigilicon.toml").resolve()
    original = repository_module.read_toml
    manifest_reads = 0

    def counted(path: Path):
        nonlocal manifest_reads
        if path.resolve() == contract:
            manifest_reads += 1
        return original(path)

    monkeypatch.setattr(repository_module, "read_toml", counted)
    monkeypatch.chdir(tmp_path)

    assert check_designs_main([]) == 0
    assert manifest_reads == 1
    assert '"passed": true' in capsys.readouterr().out


def test_check_designs_reuses_the_loaded_component_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    component = write_component_owner(tmp_path, "example", filesets={}).resolve()
    original = repository_checks_module.read_toml
    component_reads = 0

    def counted(path: Path):
        nonlocal component_reads
        if path.resolve() == component:
            component_reads += 1
        return original(path)

    monkeypatch.setattr(repository_checks_module, "read_toml", counted)
    monkeypatch.chdir(tmp_path)

    assert check_designs_main([]) == 0
    assert component_reads == 1
