"""Design-owned Tcl plugins, exercised through Project and native tool outputs."""
import json
from pathlib import Path

import pytest

from sigilicon.external_tools import ProcessResult, managed_process
from sigilicon.project import Project
from conftest import write_file


QOR = """Timing Path Group 'core'
Critical Path Slack: 0.10
Critical Path Clk Period: 2.00
Total Negative Slack: 0.00
No. of Violating Paths: 0.00
Worst Hold Violation: 0.00
Total Hold Violation: 0.00
No. of Hold Violations: 0.00
Timing Path Group 'sample_0'
Critical Path Slack: 0.10
Critical Path Clk Period: 0.50
Total Negative Slack: 0.00
No. of Violating Paths: 0.00
Worst Hold Violation: 0.00
Total Hold Violation: 0.00
No. of Hold Violations: 0.00
"""
AREA = "digital_count=10\ndigital_area=25.0\nmacro_count=1\nmacro_area=100.0\n"


@pytest.fixture
def dc_project(tmp_path):
    root = tmp_path
    owner = root / "ip/synth"
    write_file(owner / "design.sv", "module design; endmodule\n")
    write_file(owner / "design.toml", '''[implementation]
top_module="design"
lanes=1
frequency=500.0
[acceptance]
no_setup_violations=true
no_hold_violations=true
''')
    write_file(owner / "constraints.sdc", "create_clock -name core -period 2 clk\n")
    write_file(owner / "synthesis.tcl", "# design owns the complete synthesis recipe\n")
    write_file(owner / "library.tcl", "# design owns Liberty preparation\n")
    write_file(owner / "macro.lib", "library (mem) { cell (MEM) {} }\n")
    write_file(owner / "component.toml", '''schema=6
contract_kind="ip-component"
path_scope="owner"
owner="synth"
root="ip/synth"
name="synth"
kind="rtl-ip"
operation_catalog="operations"
[sources]
operations="ip/synth/operations.toml"
rtl="ip/synth/design.sv"
design="ip/synth/design.toml"
sdc="ip/synth/constraints.sdc"
script="ip/synth/synthesis.tcl"
lc="ip/synth/library.tcl"
macro="ip/synth/macro.lib"
[filesets]
synthesis=["rtl","design","sdc","script"]
library=["lc","macro"]
''')
    write_file(root / "catalogs/ip.toml", '''schema=2
contract_kind="ip-catalog"
path_scope="repository"
owner="test"
[components.synth]
contract="ip/synth/component.toml"
''')
    target = write_file(root / "assets/target.db", "target library\n")
    with (root / "sigilicon.toml").open("a") as stream:
        stream.write(f'''\n[runtime.tools]
"dc"="/bin/true"
"lc"="/bin/true"
[runtime.files]
"target"="{target}"
''')
    write_file(owner / "operations.toml", '''schema=5
contract_kind="owner-operations"
path_scope="owner"
owner="synth"
[runtime.dc.tools]
SIGILICON_SYNOPSYS_DC_SHELL="dc"
[runtime.dc.files]
SIGILICON_DC_TARGET_DB="target"
[runtime.lc.tools]
SIGILICON_SYNOPSYS_LC_SHELL="lc"
[operations.map]
[[operations.map.steps]]
id="library"
uses="synopsys.library-compiler"
runtime="lc"
filesets=[{component="synth",fileset="library"}]
[operations.map.steps.config]
script="library.tcl"
liberty="macro.lib"
library="mem"
success_marker="LIBRARY_COMPLETE"
timeout_seconds=10
[operations.map.steps.evidence]
role="diagnostic"
level="l0"
scope="library-compilation"
[[operations.map.steps]]
id="synthesis"
uses="synopsys.dc"
runtime="dc"
needs=["library"]
filesets=[{component="synth",fileset="synthesis"}]
[operations.map.steps.config]
script="synthesis.tcl"
variant="test"
corner="tt"
constraints="constraints.sdc"
design="design.toml"
design_table="implementation"
parameter_bindings={LANES="lanes"}
macro_count_field="lanes"
acceptance_table="acceptance"
libraries=[{step="library",role="compiled-library",kind="library.synopsys-db",path="library.db"}]
reports=["qor.rpt","accounting.rpt","area.rpt"]
success_marker="SYNTHESIS_COMPLETE"
timeout_seconds=10
[operations.map.steps.config.timing]
clock="core"
frequency_field="frequency"
path_groups={core=1,"sample_*"="lanes"}
[operations.map.steps.config.hdl]
top="design"
sources=[{component="synth",source="rtl"}]
[operations.map.steps.evidence]
role="diagnostic"
level="l2"
scope="digital-synthesis"
''')
    return Project.open(root)


@pytest.fixture
def native_outputs(monkeypatch):
    outputs = dict(qor=QOR, accounting=AREA, dc_log="SYNTHESIS_COMPLETE\n", lc_log="LIBRARY_COMPLETE\n",
                   exit_code=0, missing=None, empty=None, calls=[], tamper=False, symlink=False,
                   extra_report=None)

    def process(request):
        request.before_spawn()
        root = Path(request.environment["SIGILICON_OUTPUT_ROOT"])
        # The plugin passes the sealed owner script itself, not generated Tcl.
        script = Path(request.argv[-1]).read_text()
        if "SIGILICON_LC_LIBERTY" in request.environment:
            assert script == "# design owns Liberty preparation\n"
            assert "library (mem)" in Path(request.environment["SIGILICON_LC_LIBERTY"]).read_text()
            write_file(Path(request.environment["SIGILICON_LC_DB"]), "compiled mem DB\n")
            outputs["calls"].append("library")
            return ProcessResult(0, outputs["lc_log"], "")
        assert script == "# design owns the complete synthesis recipe\n"
        assert request.environment["SIGILICON_DC_PARAMETERS"] == "LANES=1"
        assert Path(request.environment["SIGILICON_DC_TARGET_DB"]).read_text() == "target library\n"
        libraries = Path(request.environment["SIGILICON_DC_LINK_LIBRARIES"]).read_text().splitlines()
        assert len(libraries) == 1 and Path(libraries[0]).read_text() == "compiled mem DB\n"
        rtl = Path(Path(request.environment["SIGILICON_HDL_FILELIST"]).read_text().strip())
        assert rtl.read_text() == "module design; endmodule\n"
        outputs["calls"].append("synthesis")
        for name, content in {"qor.rpt": outputs["qor"], "accounting.rpt": outputs["accounting"],
                              "area.rpt": "cell area report\n", "mapped.v": "module design; endmodule\n",
                              "mapped.sdc": "create_clock -period 2 clk\n", "mapped.ddc": "mapped checkpoint\n"}.items():
            if name != outputs["missing"]:
                write_file(root / name, "" if name == outputs["empty"] else content)
        write_file(root / "scratch/undeclared.txt", "not a declared artifact\n")
        if outputs["extra_report"]:
            write_file(root / "diagnostic/qor.rpt", outputs["extra_report"])
        if outputs["symlink"]:
            (root / "mapped.ddc").unlink()
            (root / "mapped.ddc").symlink_to(root / "scratch/undeclared.txt")
        if outputs["tamper"]:
            rtl.chmod(0o600)
            rtl.write_text("modified during tool invocation\n")
        return ProcessResult(outputs["exit_code"], outputs["dc_log"], "")

    monkeypatch.setattr(managed_process, "run", process)
    return outputs


def run(dc_project):
    plan = dc_project.plan("synth:map")
    assert dc_project.preflight(plan).ready
    result = dc_project.run(plan)
    return result, {step.step: {a.role: json.loads(a.read_text()) for a in step.result.artifacts
                            if a.role in {"execution-verdict", "measurements"}}
                    for step in result.outcomes}


def test_dc_consumes_compiled_library_and_publishes_checked_observations(dc_project, native_outputs):
    result, artifacts = run(dc_project)
    assert result.status == "succeeded"
    assert native_outputs["calls"] == ["library", "synthesis"]
    root = result.run_root
    assert root == dc_project.artifact_root / "synth/map" / result.run_id
    assert (root / "outputs/library/library.db").read_text() == "compiled mem DB\n"
    assert json.loads((root / "result.json").read_text())["status"] == "succeeded"
    assert json.loads((root / "outputs/synthesis/measurements.json").read_text())["run_id"] == result.run_id
    assert (root / "outputs/synthesis/reports/qor.rpt").read_text() == QOR
    verdict = artifacts["synthesis"]["execution-verdict"]
    assert verdict["passed"] is True
    assert verdict["product_qualification_conclusion"] is False
    assert verdict["run_id"] == result.run_id
    assert verdict["plan_identity"] == result.plan_identity
    assert verdict["step_id"] == "synthesis"
    measurements = artifacts["synthesis"]["measurements"]
    assert measurements["area"]["digital_area"] == 25
    assert measurements["area"]["macro_area"] == 100
    assert measurements["timing"]["core"]["clock_period_ns"] == 2
    assert all(measurements["checks"].values())
    assert all(a.path.name != "undeclared.txt" for step in result.outcomes for a in step.result.artifacts)


@pytest.mark.parametrize(("old", "new", "check"), [
    ("No. of Violating Paths: 0.00", "No. of Violating Paths: 1.00", "no_setup_violations"),
    ("Critical Path Slack: 0.10", "Critical Path Slack: -0.01", "no_setup_violations"),
    ("Total Negative Slack: 0.00", "Total Negative Slack: -0.01", "no_setup_violations"),
    ("No. of Hold Violations: 0.00", "No. of Hold Violations: 1.00", "no_hold_violations"),
    ("Worst Hold Violation: 0.00", "Worst Hold Violation: -0.01", "no_hold_violations"),
    ("Total Hold Violation: 0.00", "Total Hold Violation: -0.01", "no_hold_violations"),
    ("Critical Path Clk Period: 2.00", "Critical Path Clk Period: 3.00", "target_clock_period"),
    ("sample_0", "unexpected_group", "timing_report_complete"),
    ("No. of Hold Violations: 0.00", "", "timing_report_complete"),
    ("No. of Violating Paths: 0.00", "No. of Violating Paths: 0.50", "timing_report_complete"),
    ("Critical Path Slack: 0.10", "Critical Path Slack: 1e999", "timing_report_complete"),
    ("Critical Path Slack: 0.10", "Critical Path Slack: +--1", "timing_report_complete"),
])
def test_dc_fails_closed_on_native_timing_evidence(dc_project, native_outputs, old, new, check):
    native_outputs["qor"] = QOR.replace(old, new, 1)
    result, artifacts = run(dc_project)
    assert result.status == "failed"
    assert artifacts["synthesis"]["execution-verdict"]["checks"][check] is False


@pytest.mark.parametrize(("field", "value", "check"), [
    ("qor", QOR + QOR, "timing_report_complete"),
    ("qor", "", "timing_report_complete"),
    ("accounting", AREA + "macro_count=1\n", "area_report_complete"),
    ("accounting", AREA.replace("25.0", "nan"), "area_report_complete"),
    ("accounting", AREA.replace("macro_count=1", "macro_count=2"), "macro_count"),
    ("dc_log", "Error: fixture error\nSYNTHESIS_COMPLETE\n", "no_tool_errors"),
    ("dc_log", "Warning: Unable to resolve reference 'MEM'\nSYNTHESIS_COMPLETE\n", "no_unresolved_references"),
    ("dc_log", "SYNTHESIS_COMPLETE\nSYNTHESIS_COMPLETE\n", "completed"),
    ("dc_log", "prefix SYNTHESIS_COMPLETE\n", "completed"),
    ("exit_code", 1, "process_exit"),
    ("missing", "mapped.v", "declared_outputs"),
    ("empty", "mapped.ddc", "declared_outputs"),
    ("symlink", True, "declared_outputs"),
])
def test_dc_keeps_reports_but_rejects_invalid_completion(dc_project, native_outputs, field, value, check):
    native_outputs[field] = value
    result, artifacts = run(dc_project)
    assert result.status == "failed"
    assert artifacts["synthesis"]["execution-verdict"]["checks"][check] is False


def test_dc_reports_timing_without_claiming_unrequested_acceptance(dc_project, native_outputs):
    spec = dc_project.project_root / "ip/synth/design.toml"
    spec.write_text(spec.read_text().replace("violations=true", "violations=false"))
    native_outputs["qor"] = QOR.replace("No. of Violating Paths: 0.00", "No. of Violating Paths: 1.00")
    result, artifacts = run(Project.open(dc_project.project_root))
    assert result.status == "succeeded"
    assert artifacts["synthesis"]["measurements"]["checks"]["no_setup_violations"] is False
    assert artifacts["synthesis"]["execution-verdict"]["product_qualification_conclusion"] is False


def test_dc_custom_report_paths_preserve_the_canonical_timing_report(dc_project, native_outputs):
    path = dc_project.project_root / "ip/synth/operations.toml"
    path.write_text(path.read_text().replace('"area.rpt"]', '"area.rpt","diagnostic/qor.rpt"]'))
    native_outputs["extra_report"] = "custom diagnostic, not the acceptance report\n"
    result, artifacts = run(Project.open(dc_project.project_root))
    assert result.status == "succeeded"
    assert artifacts["synthesis"]["measurements"]["timing"]["core"]["clock_period_ns"] == 2
    assert any(a.read_text() == native_outputs["extra_report"] for step in result.outcomes
               for a in step.result.artifacts if a.role == "report")


def test_library_failure_prevents_synthesis(dc_project, native_outputs):
    native_outputs["lc_log"] = "Error: library failure\nLIBRARY_COMPLETE\n"
    result, artifacts = run(dc_project)
    assert result.status == "failed"
    assert native_outputs["calls"] == ["library"]
    assert artifacts["library"]["execution-verdict"]["checks"]["no_tool_errors"] is False


def test_design_source_mutation_is_rejected(dc_project, native_outputs):
    native_outputs["tamper"] = True
    with pytest.raises(RuntimeError, match="input changed"):
        run(dc_project)


@pytest.mark.parametrize(("old", "new"), [
    ('parameter_bindings={LANES="lanes"}', 'parameter_bindings={LANES="missing"}'),
    ('acceptance_table="acceptance"', 'acceptance_table="missing"'),
    ('script="synthesis.tcl"', 'script="unsealed.tcl"'),
    ('kind="library.synopsys-db"', 'kind="netlist.verilog"'),
    ('needs=["library"]', 'needs=[]'),
    ('variant="test"', 'variant="test"\nmisspelled_field=true'),
])
def test_dc_configuration_is_checked_before_execution(dc_project, old, new):
    path = dc_project.project_root / "ip/synth/operations.toml"
    path.write_text(path.read_text().replace(old, new))
    with pytest.raises(ValueError):
        Project.open(dc_project.project_root).plan("synth:map")
