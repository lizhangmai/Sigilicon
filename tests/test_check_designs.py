from __future__ import annotations

from pathlib import Path

import pytest

import sigilicon.domain.repository as repository_module
import sigilicon.workflows.design_catalog as design_catalog_module
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


def test_check_designs_threads_one_project_through_an_explicit_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    contract = (tmp_path / "sigilicon.toml").resolve()
    design_root = tmp_path / "designs"
    owner = design_root / "sample"
    owner.mkdir(parents=True)
    (owner / "design.toml").write_text("design = 'stub'\n", encoding="utf-8")
    (owner / "source.cdl").write_text("* stub\n", encoding="utf-8")
    (tmp_path / "verify.py").write_text("# owner\n", encoding="utf-8")
    catalog = design_root / "catalog.toml"
    catalog.write_text(
        '''[[entries]]
name = "sample"
directory = "sample"
kind = "analog"
entrypoint = "sample/run.py"
design_specs = ["design.toml"]
source_files = ["source.cdl"]
oa_policy = "recursive-schematic"
verification_policy = "local"
verification_owner = "verify.py"
''',
        encoding="utf-8",
    )
    original = repository_module.read_toml
    manifest_reads = 0

    def counted(path: Path):
        nonlocal manifest_reads
        if path.resolve() == contract:
            manifest_reads += 1
        return original(path)

    class Inspection:
        def as_dict(self) -> dict[str, bool]:
            return {"passed": True}

    def inspect_design(path: Path, *, project=None, project_root=None):
        assert path == (owner / "design.toml").resolve()
        assert project is not None
        assert project.project_root == tmp_path.resolve()
        assert project_root is None
        return Inspection()

    monkeypatch.setattr(repository_module, "read_toml", counted)
    monkeypatch.setattr(design_catalog_module, "inspect_design", inspect_design)
    monkeypatch.chdir(tmp_path)

    assert check_designs_main(["--catalog", str(catalog)]) == 0
    assert manifest_reads == 1
    assert '"passed": true' in capsys.readouterr().out
