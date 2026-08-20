from __future__ import annotations

from pathlib import Path

from sigilicon.workflows.design_catalog import inspect_design_catalog


def test_catalog_directory_is_the_design_root(tmp_path: Path) -> None:
    root = tmp_path / "project"
    design_root = root / "components/custom/oa"
    owner = design_root / "sample"
    owner.mkdir(parents=True)
    (owner / "source.sv").write_text("module sample; endmodule\n", encoding="utf-8")
    (root / "verify.py").write_text("# verification owner\n", encoding="utf-8")
    catalog_path = design_root / "catalog.toml"
    catalog_path.write_text(
        '''
[[entries]]
name = "sample"
directory = "sample"
kind = "systemverilog"
entrypoint = "sample/run.py"
design_specs = []
source_files = ["source.sv"]
oa_policy = "systemverilog-text"
verification_policy = "local"
verification_owner = "verify.py"
''',
        encoding="utf-8",
    )

    catalog, report = inspect_design_catalog(catalog_path, project_root=root)

    assert catalog.design_root == design_root.resolve()
    assert catalog.entries[0].directory == owner.resolve()
    assert report["catalog"] == "components/custom/oa/catalog.toml"
    assert report["entries"][0]["directory"] == "components/custom/oa/sample"
