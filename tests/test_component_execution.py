from __future__ import annotations

from pathlib import Path

import pytest
from conftest import write_file

from sigilicon.adapters.cadence import xcelium
from sigilicon.external_tools import ProcessResult
from sigilicon.project import Project


def _composite(root: Path, *, edge: str = "source") -> tuple[Project, Path]:
    parent = root / "ip/parent"
    child = root / "ip/child"
    for name, directory in (("parent", parent), ("child", child)):
        directory.mkdir(parents=True, exist_ok=True)
        relative = directory.relative_to(root).as_posix()
        (directory / "top.sv").write_text(f"module {name}; endmodule\n")
        (directory / "component.toml").write_text(f'''schema = 6
contract_kind = "ip-component"
path_scope = "owner"
owner = "{name}"
name = "{name}"
root = "{relative}"
kind = "rtl-ip"
{('operation_catalog = "operations"' if name == 'parent' else '')}
[sources]
rtl = "{relative}/top.sv"
{('operations = "'+relative+'/operations.toml"' if name == 'parent' else '')}
[filesets]
rtl = ["rtl"]
''')
        with (root / "catalogs/ip.toml").open("a") as stream:
            stream.write(f'\n[components.{name}]\ncontract = "{relative}/component.toml"\n')
    if edge != "absent":
        with (parent / "component.toml").open("a") as stream:
            stream.write('\n[[component]]\nname = "child"\n')
            if edge == "source":
                stream.write(f'contract = "{child.relative_to(root)}/component.toml"\n')
            if edge == "release":
                stream.write('[component.release]\nexport = "child"\nrequired_maturity = "development"\nviews = ["rtl"]\n')
    (parent / "operations.toml").write_text('''schema = 5
contract_kind = "owner-operations"
path_scope = "owner"
owner = "parent"
[operations.rtl]
uses = "cadence.xcelium"
filesets = [{ component = "parent", fileset = "rtl" }, { component = "child", fileset = "rtl" }]
config = { hdl = {top = "parent", sources = [{component = "parent", source = "rtl"}, {component = "child", source = "rtl"}]}, success_marker = "FIXTURE_COMPLETE", timeout_seconds = 10 }
''')
    executable = write_file(root / "site/xcelium/tools/bin/xrun", "offline tool boundary\n", executable=True)
    with (root / "sigilicon.toml").open("a") as stream:
        stream.write(f'\n[runtime.tools]\n"cadence.xrun" = "{executable}"\n')
    return Project.open(root), child / "top.sv"


def test_composite_rtl_consumes_distinct_sealed_component_sources(tmp_path: Path, monkeypatch) -> None:
    project, child = _composite(tmp_path)

    def simulate(request):
        child.write_text("changed live source after sealing\n")
        selected = [Path(argument).read_text() for argument in request.argv if argument.endswith(".sv")]
        native_log = Path(request.argv[request.argv.index("-log") + 1])
        native_log.write_text("offline process boundary\n")
        return ProcessResult(0, "FIXTURE_COMPLETE\n" + "".join(selected), "")

    monkeypatch.setattr(xcelium.managed_process, "run", simulate)
    plan = project.plan("parent:rtl")
    assert project.preflight(plan).ready
    result = project.run(plan)
    assert result.status == "succeeded"
    stdout = next(artifact for artifact in result.outcomes[0].result.artifacts if artifact.path.name == "stdout.log")
    assert "module parent; endmodule" in stdout.read_text()
    assert "module child; endmodule" in stdout.read_text()
    assert "changed live source" not in stdout.read_text()


@pytest.mark.parametrize("edge", ["absent", "release"])
def test_composite_rtl_cannot_select_sources_without_a_source_edge(tmp_path: Path, edge: str) -> None:
    project, _ = _composite(tmp_path, edge=edge)
    with pytest.raises(ValueError, match="source-level dependency"):
        project.plan("parent:rtl")


def test_synopsys_composite_compiles_qualified_sources_in_declared_order(tmp_path: Path) -> None:
    _composite(tmp_path)
    parent = tmp_path / "ip/parent"
    runner = write_file(parent / "run.sh", """#!/bin/bash
set -eu
while read -r source; do cat "$source"; done < "$SIGILICON_HDL_FILELIST"
printf 'COMPOSITE_COMPLETE\\n'
""", executable=True)
    component = parent / "component.toml"
    component.write_text(component.read_text().replace('[sources]', '[sources]\nrunner = "ip/parent/run.sh"').replace('rtl = ["rtl"]', 'rtl = ["rtl", "runner"]'))
    (parent / "operations.toml").write_text('''schema = 5
contract_kind = "owner-operations"
path_scope = "owner"
owner = "parent"
[runtime.vcs.tools]
SIGILICON_RUNNER_SHELL = "runtime.bash"
SIGILICON_SYNOPSYS_VCS = "synopsys.vcs"
[operations.rtl]
uses = "synopsys.vcs"
runtime = "vcs"
filesets = [{ component = "parent", fileset = "rtl" }, { component = "child", fileset = "rtl" }]
config = { runner = "run.sh", variant = "test", target = "structural", hdl = {top = "parent", sources = [{component = "child", source = "rtl"}, {component = "parent", source = "rtl"}]}, success_marker = "COMPOSITE_COMPLETE", timeout_seconds = 10 }
''')
    with (tmp_path / "sigilicon.toml").open("a") as stream:
        stream.write('"runtime.bash" = "/bin/bash"\n"synopsys.vcs" = "/bin/true"\n')
    project = Project.open(tmp_path)
    result = project.run(project.plan("parent:rtl"))
    assert result.status == "succeeded"
    stdout = next(artifact for artifact in result.outcomes[0].result.artifacts if artifact.path.name == "stdout.log")
    assert "module child; endmodule\nmodule parent; endmodule" in stdout.read_text()


def test_plan_identity_includes_execution_software_content(tmp_path: Path) -> None:
    import os
    import shutil
    import subprocess
    import sys
    import sigilicon

    _composite(tmp_path)
    software = tmp_path / "software"
    package = software / "sigilicon"
    shutil.copytree(Path(sigilicon.__file__).parent, package, ignore=shutil.ignore_patterns("__pycache__"))
    script = "from sigilicon.project import Project; import sys; print(Project.open(sys.argv[1]).plan('parent:rtl').identity)"
    environment = {**os.environ, "PYTHONPATH": str(software)}
    before = subprocess.check_output([sys.executable, "-c", script, str(tmp_path)], env=environment, text=True)
    with (package / "__init__.py").open("a") as stream:
        stream.write("\n# changed implementation source\n")
    after = subprocess.check_output([sys.executable, "-c", script, str(tmp_path)], env=environment, text=True)
    assert before != after


@pytest.mark.parametrize('owner', ['parent', 'child'])
def test_nested_components_keep_source_identity_separate_from_owner(tmp_path: Path, monkeypatch, owner: str) -> None:
    _composite(tmp_path)
    owner_root = tmp_path / 'ip' / owner
    for name, parent in [('alu', owner_root / 'component.toml'),
                         ('adder', owner_root / 'alu/component.toml')]:
        directory = owner_root / name
        directory.mkdir()
        (directory / 'rtl.sv').write_text(f'module {name}; endmodule\n')
        (directory / 'component.toml').write_text(f'''schema = 6
contract_kind = "ip-component"
path_scope = "owner"
owner = "{owner}"
name = "{name}"
root = "ip/{owner}"
kind = "rtl-ip"
[sources]
rtl = "ip/{owner}/{name}/rtl.sv"
[filesets]
rtl = ["rtl"]
''')
        parent.write_text(parent.read_text() + f'\n[[component]]\nname = "{name}"\ncontract = "ip/{owner}/{name}/component.toml"\n')
    catalog = tmp_path / 'ip/parent/operations.toml'
    catalog.write_text(catalog.read_text().replace('{ component = "child", fileset = "rtl" }',
        '{ component = "alu", fileset = "rtl" }, { component = "adder", fileset = "rtl" }').replace('{component = "child", source = "rtl"}', '{component = "alu", source = "rtl"}, {component = "adder", source = "rtl"}'))

    def simulate(request):
        selected = [Path(argument).read_text() for argument in request.argv if argument.endswith('.sv')]
        Path(request.argv[request.argv.index('-log') + 1]).write_text('offline process boundary\n')
        return ProcessResult(0, 'FIXTURE_COMPLETE\n' + ''.join(selected), '')

    monkeypatch.setattr(xcelium.managed_process, 'run', simulate)
    project = Project.open(tmp_path)
    result = project.run(project.plan('parent:rtl'))
    assert result.status == 'succeeded'
    stdout = next(a for a in result.outcomes[0].result.artifacts if a.path.name == 'stdout.log')
    assert 'module alu; endmodule' in stdout.read_text()
    assert 'module adder; endmodule' in stdout.read_text()
    references = project.source_inventory('parent')
    assert references[owner_root / 'adder/rtl.sv'].component == 'adder'
    assert project.require_owner(owner_root / 'adder/rtl.sv').name == owner


def test_xcelium_keeps_headers_out_of_units_and_passes_compile_options(tmp_path: Path, monkeypatch) -> None:
    _composite(tmp_path)
    parent = tmp_path / 'ip/parent'
    write_file(parent / 'defs/width.svh', '`define WIDTH 8\n')
    write_file(parent / 'types.sv', 'package types; typedef logic [7:0] byte_t; endpackage\n')
    component = parent / 'component.toml'
    component.write_text(component.read_text().replace('[sources]', '[sources]\nheader = "ip/parent/defs/width.svh"\npackage = "ip/parent/types.sv"').replace('rtl = ["rtl"]', 'rtl = ["rtl", "header", "package"]'))
    catalog = parent / 'operations.toml'
    catalog.write_text(catalog.read_text().replace('sources = [{component = "parent", source = "rtl"}', 'headers = [{component = "parent", source = "header"}], include_dirs = ["defs"], defines = {MODE = "3"}, sources = [{component = "parent", source = "package"}, {component = "parent", source = "rtl"}'))

    def simulate(request):
        argv = request.argv
        units = [Path(item).name for item in argv if item.endswith('.sv')]
        assert units == ['types.sv', 'top.sv', 'top.sv']
        assert not any(item.endswith('.svh') for item in argv)
        assert argv[argv.index('-top') + 1] == 'parent'
        assert argv[argv.index('-define') + 1] == 'MODE=3'
        include = Path(argv[argv.index('-incdir') + 1])
        assert (include / 'width.svh').read_text() == '`define WIDTH 8\n'
        assert include != parent / 'defs'
        Path(argv[argv.index('-log') + 1]).write_text('compile options checked\n')
        return ProcessResult(0, 'FIXTURE_COMPLETE\n', '')

    monkeypatch.setattr(xcelium.managed_process, 'run', simulate)
    project = Project.open(tmp_path)
    assert project.run(project.plan('parent:rtl')).status == 'succeeded'


def test_static_check_does_not_require_the_configured_eda_installation(tmp_path: Path) -> None:
    from sigilicon.cli.main import main

    project, _ = _composite(tmp_path)
    manifest = tmp_path / 'sigilicon.toml'
    manifest.write_text(manifest.read_text().replace(str(tmp_path / 'site/xcelium/tools/bin/xrun'), '/uninstalled/xcelium/tools/bin/xrun'))
    assert main(['check', '--project-root', str(tmp_path)]) == 0
    with pytest.raises((ValueError, OSError), match='executable|tool|No such file'):
        Project.open(tmp_path).plan('parent:rtl')
