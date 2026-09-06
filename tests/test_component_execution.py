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
        (directory / "component.toml").write_text(f'''schema = 5
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
            stream.write(f'\n[[component]]\nname = "child"\ncontract = "{child.relative_to(root)}/component.toml"\n')
            if edge == "release":
                stream.write('[component.release]\nexport = "child"\nrequired_maturity = "development"\nviews = ["rtl"]\n')
    (parent / "operations.toml").write_text('''schema = 5
contract_kind = "owner-operations"
path_scope = "owner"
owner = "parent"
[operations.rtl]
uses = "cadence.xcelium"
filesets = [{ component = "parent", fileset = "rtl" }, { component = "child", fileset = "rtl" }]
config = { success_marker = "FIXTURE_COMPLETE", timeout_seconds = 10 }
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
while read -r source; do cat "$source"; done < "$SIGILICON_VCS_RTL_FILELIST"
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
config = { runner = "run.sh", variant = "test", target = "structural", rtl_sources = [{component = "child", source = "rtl"}, {component = "parent", source = "rtl"}], success_marker = "COMPOSITE_COMPLETE", timeout_seconds = 10 }
''')
    with (tmp_path / "sigilicon.toml").open("a") as stream:
        stream.write('"runtime.bash" = "/bin/bash"\n"synopsys.vcs" = "/bin/true"\n')
    project = Project.open(tmp_path)
    result = project.run(project.plan("parent:rtl"))
    assert result.status == "succeeded"
    stdout = next(artifact for artifact in result.outcomes[0].result.artifacts if artifact.path.name == "stdout.log")
    assert "module child; endmodule\nmodule parent; endmodule" in stdout.read_text()
