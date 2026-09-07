"""FC operation tests use Project and a fake external-process boundary."""
from pathlib import Path
import json
import re
import pytest

from sigilicon.project import Project
from sigilicon.external_tools import ProcessResult, managed_process
from conftest import write_file


@pytest.fixture
def fc_project(tmp_path):
    root = tmp_path
    write_file(root / "ip/fc/design.sv", "module top; endmodule\n")
    write_file(root / "ip/fc/design.toml", '[bank]\ntop_module="top"\nblocks=4\n')
    write_file(root / "ip/fc/constraints.sdc", "create_clock -period 2 clk\n")
    write_file(root / "ip/fc/floorplan.tcl", "# fixture floorplan\n")
    write_file(root / "ip/fc/component.toml", '''
schema=6
contract_kind="ip-component"
path_scope="owner"
owner="fc"
root="ip/fc"
name="fc"
kind="rtl-ip"
operation_catalog="operations"
[sources]
operations="ip/fc/operations.toml"
rtl="ip/fc/design.sv"
design="ip/fc/design.toml"
sdc="ip/fc/constraints.sdc"
floorplan="ip/fc/floorplan.tcl"
[filesets]
physical=["rtl","design","sdc","floorplan"]
''')
    write_file(root / "catalogs/ip.toml", '''
schema=2
contract_kind="ip-catalog"
path_scope="repository"
owner="test"
[components.fc]
contract="ip/fc/component.toml"
''')
    slots = ("TECH_FILE", "TECH_LEF", "CELL_LEF", "CELL_DB",
             "TECHNOLOGY_SETUP", "RC_EARLY", "RC_LATE", "RC_MAP", "GDS_MAP")
    profile = "\n".join(f'SIGILICON_FC_{s}="fc.{s.lower()}"' for s in slots)
    bindings = "\n".join(f'"fc.{s.lower()}"="{write_file(root / ("assets/" + s), "# fixture")}"' for s in slots)
    with (root / "sigilicon.toml").open("a") as stream:
        stream.write('\n[runtime.tools]\n"runtime.bash"="/bin/bash"\n"fc.shell"="/bin/true"\n"fc.lc"="/bin/true"\n[runtime.files]\n' + bindings + "\n")
    write_file(root / "ip/fc/operations.toml", '''
schema=5
contract_kind="owner-operations"
path_scope="owner"
owner="fc"
[runtime.fc.tools]
SIGILICON_RUNNER_SHELL="runtime.bash"
SIGILICON_SYNOPSYS_FC_SHELL="fc.shell"
SIGILICON_SYNOPSYS_LC_SHELL="fc.lc"
[runtime.fc.files]
''' + profile + '''
[operations.implement]
uses="synopsys.fc"
runtime="fc"
filesets=[{component="fc",fileset="physical"}]
[operations.implement.config]
flow="rtl-to-gds"
design="design.toml"
design_table="bank"
constraints="constraints.sdc"
floorplan="floorplan.tcl"
corner="tt"
voltage=0.9
temperature=25
process=1
cores=2
metric="timing"
timeout_seconds=10
[operations.implement.config.parameter_bindings]
BLOCKS="blocks"
[operations.implement.config.hdl]
top="top"
sources=[{component="fc",source="rtl"}]
[operations.implement.evidence]
role="diagnostic"
level="l2"
scope="digital-shell"
''')
    return Project.open(root)


def test_fc_full_flow_expands_before_target_selection(fc_project):
    plan = fc_project.plan("fc:implement")
    assert [step.id for step in plan.steps] == [
        "reference-library", "init", "compile", "cts", "clock-opt",
        "route", "route-opt", "chip-finish", "export"]
    assert [step.id for step in fc_project.plan("fc:implement", to_step="compile").steps] == [
        "reference-library", "init", "compile"]
    assert fc_project.preflight(plan).ready


@pytest.mark.parametrize("failure", ["none", "zero-exit-error", "missing-completion"])
def test_fc_library_result_requires_completion_and_no_tool_errors(fc_project, monkeypatch, failure):
    def process(request):
        root = Path(request.environment["SIGILICON_FC_LAUNCH"]).parent.parent
        write_file(root / "references/cells/registry.dat", "fixture reference\n")
        write_file(root / "references/@@backup@@_cells.ndm/registry.dat", "stale backup\n")
        write_file(root / "references/cells/@@backup@@_reference.ndm/registry.dat", "nested backup\n")
        write_file(root / "references/cells/reference.ndm/registry.dat", "active frame\n")
        write_file(root / "references/temporary/log.txt", "intermediate\n")
        if failure != "missing-completion":
            write_file(root / "stage_complete.rpt",
                       "stage=reference-library\nblock=top/create_fusion_reference_library\n")
            write_file(root / "create_fusion_reference_library", "completed")
        stdout = "Error: fixture tool error\n" if failure == "zero-exit-error" else "fixture\n"
        return ProcessResult(0, stdout, "")
    monkeypatch.setattr(managed_process, "run", process)
    result = fc_project.run(fc_project.plan("fc:implement", to_step="reference-library"))
    assert result.status == ("succeeded" if failure == "none" else "failed")
    # These are execution-contract fixtures, never physical-design evidence.
    paths = list(fc_project.artifact_root.rglob("verdict.json"))
    verdict = json.loads(paths[-1].read_text())
    assert verdict["product_qualification_conclusion"] is False
    assert verdict["signoff"] == "not_requested"
    assert verdict["passed"] == (failure == "none")
    registries = list(fc_project.artifact_root.rglob("outputs/reference-library/reference-library/**/registry.dat"))
    assert sorted(p.read_text() for p in registries) == ["active frame\n", "fixture reference\n"]


def test_fc_rejects_noninteger_owner_elaboration_data(fc_project):
    path = fc_project.project_root / "ip/fc/design.toml"
    path.write_text('[bank]\ntop_module="top"\nblocks="four"\n')
    with pytest.raises(ValueError, match="integer design field"):
        Project.open(fc_project.project_root).plan("fc:implement")


def test_fc_checkpoint_chain_and_export_identity(fc_project, monkeypatch):
    import gzip
    stages = []
    def process(request):
        root = Path(request.environment["SIGILICON_FC_LAUNCH"]).parent.parent
        script = (root / "generated/finalize.tcl").read_text()
        stage = re.search(r'set stage_name "([^"]+)"', script).group(1)
        label = re.search(r'set expected_label "([^"]+)"', script).group(1)
        stages.append(stage)
        if stage == "reference-library":
            write_file(root / "references/cells.ndm/registry.dat", "reference registry")
        else:
            assert (root / "references/cells.ndm/registry.dat").read_text() == "reference registry"
            if stage != "init":
                assert (root / "design.dlib/lib.ndm").is_file()
            write_file(root / "design.dlib/lib.ndm", label)
            write_file(root / ("reports_fc/" + label.split("/")[1] + "/report_qor"),
                       "Cell Area: 42.0\nCritical Path Slack: -0.125\nNo. of Violating Paths: 3\nNo. of Hold Violations: 2\n")
            report_dir = root / "reports_fc" / label.split("/")[1]
            write_file(report_dir / "macro_accounting", "macro_count=4\nmacro_area=20\ndigital_count=12\ndigital_area=21\nphysical_only_count=2\nphysical_only_area=1\n")
            write_file(report_dir / "digital_power", "Total ( 12 cells) 2.0 uW 3.0 uW 5.0 uW ( 40.0%) 7.0 nW\n")
        if stage == "export":
            for suffix in ("v", "pt.v", "lvs.v", "gds", "def", "tt.spef"):
                path = root / ("outputs_fc/write_data." + suffix + ".gz")
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(gzip.compress(("fixture " + suffix).encode()))
        write_file(root / "stage_complete.rpt", f"stage={stage}\nblock={label}\n")
        write_file(root / label.split("/")[1], "completed")
        return ProcessResult(0, "fixture process\n", "")
    monkeypatch.setattr(managed_process, "run", process)
    result = fc_project.run(fc_project.plan("fc:implement"))
    assert result.status == "succeeded"
    assert stages[-2:] == ["chip-finish", "export"]
    output = next(fc_project.artifact_root.rglob("design.pt.v"))
    assert output.read_text() == "fixture pt.v"
    verdicts = [json.loads(p.read_text()) for p in fc_project.artifact_root.rglob("verdict.json")]
    exported = next(v for v in verdicts if v["stage"] == "export")
    assert exported["observations"]["setup_wns"]["value"] == -0.125
    assert exported["observations"]["open_nets"]["value"] is None
    assert exported["observations"]["macro_count"]["value"] == 4
    assert exported["observations"]["digital_area"]["value"] == 21
    assert exported["observations"]["physical_only_area"]["value"] == 1
    assert exported["observations"]["digital_dynamic_power"]["value"] == pytest.approx(5e-6)
    assert exported["observations"]["digital_leakage_power"]["value"] == pytest.approx(7e-9)
    assert exported["passed"] is True  # execution succeeded, NOT timing closure
    assert exported["product_qualification_conclusion"] is False


@pytest.mark.parametrize("failure", [None, "unsealed", "zero-count", "duplicate", "invalid-name"])
def test_fc_macro_collateral_uses_owner_source_closure(fc_project, failure):
    root = fc_project.project_root
    write_file(root / "ip/fc/macro.lef", "MACRO SRAM\nEND SRAM\n")
    write_file(root / "ip/fc/macro.lib", "library (sram) { cell (SRAM) {} }\n")
    write_file(root / "ip/fc/pg.tcl", "# owner PG binding\n")
    manifest = root / "ip/fc/component.toml"
    content = manifest.read_text().replace('[filesets]', 'lef="ip/fc/macro.lef"\nlib="ip/fc/macro.lib"\npg="ip/fc/pg.tcl"\n[filesets]')
    if failure != "unsealed":
        content = content.replace('"floorplan"]', '"floorplan","lef","lib","pg"]')
    manifest.write_text(content)
    operation = root / "ip/fc/operations.toml"
    macro = '\n[[operations.implement.config.macros]]\ncell="SRAM"\nlibrary="sram"\nlef="macro.lef"\nliberty="macro.lib"\ninstances=4\n'
    if failure == "zero-count":
        macro = macro.replace('instances=4', 'instances=0')
    elif failure == "invalid-name":
        macro = macro.replace('cell="SRAM"', 'cell="SRAM;exit"')
    elif failure == "duplicate":
        macro += macro
    operation.write_text(operation.read_text().replace('floorplan="floorplan.tcl"', 'floorplan="floorplan.tcl"\npg_connections="pg.tcl"') + macro)
    project = Project.open(root)
    if failure:
        with pytest.raises(ValueError):
            project.plan("fc:implement")
    else:
        plan = project.plan("fc:implement")
        for step in plan.steps:
            assert {"macro.lef", "macro.lib", "pg.tcl"} <= {s.path for s in step.source_closure}
        assert project.preflight(plan).ready
