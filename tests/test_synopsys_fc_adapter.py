from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
import subprocess

import pytest

from sigilicon.cli.main import main as sigilicon_cli_main
from sigilicon.flow import (
    ActionContext,
    ActionContract,
    AdapterSelection,
    ArtifactBinding,
    ArtifactPort,
    ExecutionEnvironment,
    ExecutionProfile,
    FlowEngine,
    FlowExecutionError,
    FlowNode,
    FlowRegistry,
    FlowSpec,
    FlowTarget,
    InputArtifact,
    PolicyCheck,
    PolicySpec,
    ResolvedCapability,
    ResolvedPlatformAsset,
    ResolvedPlatformAssetMember,
    SourceAssetsAdapter,
    SynopsysFCAdapter,
    register_standard_asic_actions,
)


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _write_owner(
    owner_root: Path,
    *,
    initialize_git: bool = True,
    fail_pnr: bool = False,
    library_report: str | None = None,
    omit_library_report: bool = False,
    design_report: str | None = None,
    physical_completion_report: str | None = None,
) -> None:
    owner_root.mkdir()
    (owner_root / "mapped.v").write_text("module top; endmodule\n", encoding="utf-8")
    (owner_root / "mapped.sdc").write_text(
        "create_clock -period 1 clk\n",
        encoding="utf-8",
    )
    (owner_root / "build-reference.tcl").write_text(
        "# reference fixture\n",
        encoding="utf-8",
    )
    (owner_root / "place-route.tcl").write_text(
        "# implementation fixture\n",
        encoding="utf-8",
    )
    (owner_root / "site.lef").write_text("END LIBRARY\n", encoding="utf-8")
    failure = "raise SystemExit(7)" if fail_pnr else "pass"
    if library_report is None:
        library_report = (
            "Checking libraries...\n"
            "Warning: fixture library warning (LM-001)\n"
            "Workspace check succeeded!\n"
        )
    if design_report is None:
        design_report = (
            "Total 2 EMS messages : 0 errors, 2 warnings, 0 info.\n"
            "Total 1 non-EMS messages : 0 errors, 1 warnings, 0 info.\n"
        )
    if physical_completion_report is None:
        physical_completion_report = (
            "SIGILICON_PHYSICAL_COMPLETION_REPORT 1\n"
            "Required PG ports = 2\n"
            "Placed required PG ports = 2\n"
            "Unplaced required PG ports = 0\n"
            "PG connectivity check = performed\n"
            "PG connectivity violations = 0\n"
        )
    _write_executable(
        owner_root / "run-fc-fixture.py",
        f'''#!/usr/bin/env python3
import os
from pathlib import Path
import sys

target = sys.argv[1]
assert target in {{"library", "pnr"}}
assert os.environ["SIGILICON_DESIGN_VARIANT"] == "fixture_variant"
assert os.environ["SIGILICON_DESIGN_CORNER"] == "tt"
assert os.environ["SIGILICON_DESIGN_TOP"] == "top"
assert Path(os.environ["SIGILICON_FC_WORK_ROOT"]).is_dir()

if target == "library":
    assert Path(os.environ["SIGILICON_SYNOPSYS_LM_SHELL"]).is_file()
    for name in (
        "SIGILICON_FC_TECH_FILE",
        "SIGILICON_FC_TECH_LEF",
        "SIGILICON_STDCELL_RVT_LEF",
        "SIGILICON_STDCELL_HVT_LEF",
        "SIGILICON_STDCELL_LVT_LEF",
        "SIGILICON_STDCELL_RVT_DB",
        "SIGILICON_STDCELL_HVT_DB",
        "SIGILICON_STDCELL_LVT_DB",
    ):
        assert Path(os.environ[name]).is_file()
    reference = Path(os.environ["SIGILICON_FC_REFERENCE_NDM"])
    reference.mkdir(parents=True)
    (reference / "library.ndm").write_text("reference library\\n")
    if {not omit_library_report!r}:
        report = Path(os.environ["SIGILICON_FC_LIBRARY_CHECK_REPORT"])
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text({library_report!r})
else:
    {failure}
    assert Path(os.environ["SIGILICON_SYNOPSYS_FC_SHELL"]).is_file()
    assert Path(os.environ["SIGILICON_FC_MAPPED_NETLIST"]).read_text().startswith("module top")
    assert "create_clock" in Path(os.environ["SIGILICON_FC_MAPPED_SDC"]).read_text()
    reference = Path(os.environ["SIGILICON_FC_REFERENCE_NDM"])
    assert (reference / "library.ndm").read_text() == "reference library\\n"
    assert Path(os.environ["SIGILICON_FC_TLUPLUS"]).is_file()
    assert Path(os.environ["SIGILICON_FC_GDS_MAP"]).is_file()
    assert Path(os.environ["SIGILICON_FC_ANTENNA_RULES"]).is_file()
    files = {{
        "SIGILICON_FC_ROUTED_NETLIST": "module top; endmodule\\n",
        "SIGILICON_FC_ROUTED_CONSTRAINTS": "create_clock -period 1 clk\\n",
        "SIGILICON_FC_GDS": "gds fixture\\n",
        "SIGILICON_FC_DESIGN_CHECK_REPORT": {design_report!r},
        "SIGILICON_FC_STRUCTURAL_REPORT": "structure passed\\n",
        "SIGILICON_FC_QOR_REPORT": (
            "Critical Path Slack:                   -0.04\\n"
            "Worst Hold Violation:                  -0.08\\n"
            "Max Trans Violations:                      2\\n"
            "Max Cap Violations:                        3\\n"
            "TOTAL LEAF CELLS                         418      387.590\\n"
        ),
        "SIGILICON_FC_TIMING_REPORT": "slack (VIOLATED) -0.04\\n",
        "SIGILICON_FC_AREA_REPORT": (
            "Total physical cell area: 387.590\\n"
            "TOTAL LEAF CELLS 418 387.590\\n"
        ),
        "SIGILICON_FC_POWER_REPORT": (
            "Running switching activity propagation in scalar mode!\\n"
            "Total Dynamic Power = 9.55e+04 nW\\n"
            "Cell Leakage Power = 3.86e+02 nW\\n"
        ),
        "SIGILICON_FC_DRC_REPORT": (
            "Total number of nets = 422\\n"
            "Total number of open nets = 0\\n"
            "@@@@@@@ TOTAL VIOLATIONS = 0\\n"
            "Total number of antenna violations = 0\\n"
            "Total number of tie to rail violations = 0\\n"
            "Total number of tie to rail directly violations = 0\\n"
        ),
        "SIGILICON_FC_PHYSICAL_COMPLETION_REPORT": {physical_completion_report!r},
        "SIGILICON_FC_TIE_OFF_CHECK_REPORT": (
            "Report : check_mv_design\\n"
            "        -tieoff\\n"
            "Information: Total 0 error(s) and 0 warning(s) from "
            "check_mv_design. (MV-082)\\n"
        ),
    }}
    for name, content in files.items():
        path = Path(os.environ[name])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    checkpoint = Path(os.environ["SIGILICON_FC_CHECKPOINT"])
    checkpoint.mkdir(parents=True)
    (checkpoint / "top.ndm").write_text("routed checkpoint\\n")
''',
    )
    (owner_root / "source-assets.toml").write_text(
        '''schema = 1
contract_kind = "source-assets"
path_scope = "owner"
owner = "fixture"
name = "fc-fixture-source"

[qualifiers]
variant = "fixture_variant"
corner = "tt"

[[artifacts]]
role = "mapped-netlist"
kind = "netlist.verilog"
materialization = "file"
members = ["mapped.v"]

[[artifacts]]
role = "mapped-constraints"
kind = "constraints.sdc"
materialization = "file"
members = ["mapped.sdc"]

[[artifacts]]
role = "reference-library-recipe"
kind = "recipe.reference-library"
materialization = "manifest"
members = ["run-fc-fixture.py", "build-reference.tcl", "site.lef"]

[[artifacts]]
role = "implementation-recipe"
kind = "recipe.physical-implementation"
materialization = "manifest"
members = ["run-fc-fixture.py", "place-route.tcl"]
''',
        encoding="utf-8",
    )
    if initialize_git:
        subprocess.run(("git", "init", "-q"), cwd=owner_root, check=True)
        subprocess.run(
            ("git", "config", "user.email", "fixture@example.com"),
            cwd=owner_root,
            check=True,
        )
        subprocess.run(
            ("git", "config", "user.name", "Fixture"),
            cwd=owner_root,
            check=True,
        )
        subprocess.run(("git", "add", "."), cwd=owner_root, check=True)
        subprocess.run(("git", "commit", "-qm", "fixture"), cwd=owner_root, check=True)


def _registry(owner_root: Path) -> FlowRegistry:
    registry = FlowRegistry()
    registry.register_action(
        ActionContract(
            kind="design.fc-fixture",
            outputs=(
                ArtifactPort("mapped-netlist", "netlist.verilog"),
                ArtifactPort("mapped-constraints", "constraints.sdc"),
                ArtifactPort(
                    "reference-library-recipe",
                    "recipe.reference-library",
                ),
                ArtifactPort(
                    "implementation-recipe",
                    "recipe.physical-implementation",
                ),
            ),
            adapters=("source-assets",),
            resolves_source_assets=True,
        )
    )
    register_standard_asic_actions(registry)
    registry.register_adapter("source-assets", SourceAssetsAdapter())
    registry.register_adapter("synopsys-fc", SynopsysFCAdapter(owner_root))
    return registry


def _flow(owner_root: Path) -> tuple[FlowSpec, ExecutionProfile]:
    spec = FlowSpec(
        owner="fixture",
        flow_id="fc-managed",
        nodes=(
            FlowNode(
                "assets",
                "design.fc-fixture",
                config={"source": "source-assets.toml"},
            ),
            FlowNode(
                "reference-library",
                "asic.reference-library-construction",
                config={
                    "runner": "run-fc-fixture.py",
                    "target": "library",
                    "top": "top",
                },
                bindings=(
                    ArtifactBinding(
                        "reference-library-recipe",
                        "assets",
                        "reference-library-recipe",
                    ),
                ),
                policy="reference-library-quality",
            ),
            FlowNode(
                "implementation",
                "asic.physical-implementation",
                config={
                    "runner": "run-fc-fixture.py",
                    "target": "pnr",
                    "top": "top",
                },
                bindings=(
                    ArtifactBinding("mapped-netlist", "assets", "mapped-netlist"),
                    ArtifactBinding(
                        "mapped-constraints",
                        "assets",
                        "mapped-constraints",
                    ),
                    ArtifactBinding(
                        "implementation-recipe",
                        "assets",
                        "implementation-recipe",
                    ),
                    ArtifactBinding(
                        "reference-library",
                        "reference-library",
                        "reference-library",
                    ),
                ),
                policy="physical-completion-readiness",
            ),
        ),
        targets=(
            FlowTarget("reference-library", ("reference-library",)),
            FlowTarget("implementation", ("implementation",)),
        ),
        policies=(
            PolicySpec(
                "reference-library-quality",
                (
                    PolicyCheck(
                        "tool-completed",
                        "tool-execution-completed",
                        "equals",
                        True,
                    ),
                    PolicyCheck(
                        "no-library-errors",
                        "library-check-error-count",
                        "at_most",
                        0,
                    ),
                    PolicyCheck(
                        "workspace-check-succeeded",
                        "library-check-succeeded",
                        "equals",
                        True,
                    ),
                ),
            ),
            PolicySpec(
                "physical-completion-readiness",
                (
                    PolicyCheck(
                        "tool-completed",
                        "tool-execution-completed",
                        "equals",
                        True,
                    ),
                    PolicyCheck(
                        "no-design-check-errors",
                        "design-check-error-count",
                        "at_most",
                        0,
                    ),
                    PolicyCheck(
                        "no-open-nets",
                        "open-net-count",
                        "at_most",
                        0,
                    ),
                    PolicyCheck(
                        "no-route-drc",
                        "route-drc-violation-count",
                        "at_most",
                        0,
                    ),
                    PolicyCheck(
                        "required-pg-ports",
                        "required-pg-port-count",
                        "equals",
                        2,
                    ),
                    PolicyCheck(
                        "all-required-pg-ports-placed",
                        "unplaced-required-pg-port-count",
                        "at_most",
                        0,
                    ),
                    PolicyCheck(
                        "pg-connectivity-checked",
                        "pg-connectivity-check-performed",
                        "equals",
                        True,
                    ),
                    PolicyCheck(
                        "no-pg-connectivity-violations",
                        "pg-connectivity-violation-count",
                        "at_most",
                        0,
                    ),
                    PolicyCheck(
                        "antenna-check-active",
                        "antenna-check-active",
                        "equals",
                        True,
                    ),
                    PolicyCheck(
                        "no-antenna-violations",
                        "antenna-violation-count",
                        "at_most",
                        0,
                    ),
                    PolicyCheck(
                        "tie-off-checked",
                        "tie-off-check-performed",
                        "equals",
                        True,
                    ),
                    PolicyCheck(
                        "no-tie-off-violations",
                        "tie-off-violation-count",
                        "at_most",
                        0,
                    ),
                ),
            ),
        ),
        owner_root=owner_root,
    )
    profile = ExecutionProfile(
        owner="fixture",
        profile_id="fc-fixture",
        selections=(
            AdapterSelection("design.fc-fixture", "source-assets"),
            AdapterSelection(
                "asic.reference-library-construction",
                "synopsys-fc",
                config={
                    "timeout_seconds": 30,
                    "outputs": {
                        "reference-library": "reference.ndm",
                        "library-check-report": "check_workspace.rpt",
                    },
                },
            ),
            AdapterSelection(
                "asic.physical-implementation",
                "synopsys-fc",
                config={
                    "timeout_seconds": 30,
                    "outputs": {
                        "routed-netlist": "routed.v",
                        "routed-constraints": "routed.sdc",
                        "layout-stream": "routed.gds",
                        "checkpoint": "routed.ndm",
                        "design-check-report": "check_design.rpt",
                        "structural-report": "protected_inventory.tsv",
                        "qor-report": "qor.rpt",
                        "timing-report": "timing.rpt",
                        "area-report": "area.rpt",
                        "power-report": "power.rpt",
                        "drc-report": "drc.rpt",
                        "physical-completion-report": "physical_completion.rpt",
                        "tie-off-check-report": "tie_off_check.rpt",
                    },
                },
            ),
        ),
    )
    return spec, profile


def _member(path: Path, role: str) -> ResolvedPlatformAssetMember:
    return ResolvedPlatformAssetMember(
        role=role,
        digest=sha256(path.read_bytes()).hexdigest(),
        location=path,
    )


def _environment(tmp_path: Path) -> tuple[ExecutionEnvironment, dict[str, Path]]:
    collateral: dict[str, Path] = {}
    for name in (
        "technology-file",
        "technology-lef",
        "tluplus",
        "gds-layer-map",
        "antenna-rules",
    ):
        path = tmp_path / "platform" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"{name} fixture\n", encoding="utf-8")
        collateral[name] = path
    physical: list[ResolvedPlatformAssetMember] = []
    timing: list[ResolvedPlatformAssetMember] = []
    for role in ("rvt", "hvt", "lvt"):
        lef = tmp_path / "lef" / f"{role}.lef"
        lef.parent.mkdir(exist_ok=True)
        lef.write_text(f"{role} LEF fixture\n", encoding="utf-8")
        physical.append(_member(lef, role))
        db = tmp_path / "db" / f"{role}.db"
        db.parent.mkdir(exist_ok=True)
        db.write_text(f"{role} DB fixture\n", encoding="utf-8")
        timing.append(_member(db, role))
    bin_root = tmp_path / "bin"
    bin_root.mkdir()
    lm_shell = bin_root / "lm_shell"
    fc_shell = bin_root / "fc_shell"
    dc_shell = bin_root / "dc_shell"
    for executable in (lm_shell, fc_shell, dc_shell):
        _write_executable(executable, "#!/bin/sh\nexit 0\n")
    environment = ExecutionEnvironment(
        capabilities={
            "tool.synopsys-dc": ResolvedCapability(
                "design-compiler@fixture",
                executable=dc_shell,
            ),
            "tool.synopsys-library-manager": ResolvedCapability(
                "library-manager@fixture",
                executable=lm_shell,
            ),
            "tool.synopsys-fc": ResolvedCapability(
                "fusion-compiler@fixture",
                executable=fc_shell,
            ),
        },
        platform_assets=(
            ResolvedPlatformAsset(
                role="physical-technology",
                kind="platform.physical-view-set",
                identity="physical-technology@fixture",
                members=tuple(_member(collateral[role], role) for role in collateral),
            ),
            ResolvedPlatformAsset(
                role="standard-cell-physical",
                kind="library.lef-set",
                identity="standard-cell-lef@fixture",
                members=tuple(physical),
            ),
            ResolvedPlatformAsset(
                role="standard-cell-timing",
                kind="library.synopsys-db-set",
                identity="standard-cell-db@fixture",
                members=tuple(timing),
            ),
        ),
    )
    return environment, collateral


def _write_cli_flow_owner(owner_root: Path) -> tuple[Path, Path]:
    (owner_root / "rtl").mkdir()
    (owner_root / "rtl" / "top.sv").write_text(
        "module top; endmodule\n",
        encoding="utf-8",
    )
    for name, content in (
        ("tb.sv", "module tb; endmodule\n"),
        ("simulate.sh", "#!/bin/sh\nexit 0\n"),
        ("synthesis.toml", "schema = 1\n"),
        ("electrical.sp", ".end\n"),
        ("deck.sp", ".end\n"),
        ("electrical.sh", "#!/bin/sh\nexit 0\n"),
        ("qualification.toml", "schema = 1\n"),
        ("qualification.py", "# fixture qualification recipe\n"),
    ):
        (owner_root / name).write_text(content, encoding="utf-8")
    _write_executable(
        owner_root / "run-dc-fixture.py",
        '''#!/usr/bin/env python3
import os
from pathlib import Path

root = Path(os.environ["SIGILICON_DC_BUILD_ROOT"])
root.mkdir(parents=True, exist_ok=True)
assert os.environ["SIGILICON_DESIGN_VARIANT"] == "fixture_variant"
assert Path(os.environ["SIGILICON_RTL_SOURCES_FILE"]).is_file()
assert Path(os.environ["SIGILICON_CONSTRAINTS"]).is_file()
assert Path(os.environ["SIGILICON_SYNOPSYS_DC_SHELL"]).is_file()
for role in ("RVT", "HVT", "LVT"):
    assert Path(os.environ[f"SIGILICON_STDCELL_{role}_DB"]).is_file()
(root / "mapped.v").write_text("module top; endmodule\\n")
(root / "mapped.sdc").write_text("create_clock -period 1 clk\\n")
(root / "mapped.ddc").write_text("checkpoint\\n")
(root / "check_design.rpt").write_text("passed\\n")
''',
    )
    (owner_root / "source-assets.toml").write_text(
        '''schema = 1
contract_kind = "source-assets"
path_scope = "owner"
owner = "fixture"
name = "fc-fixture-source"

[qualifiers]
variant = "fixture_variant"
corner = "tt"

[[artifacts]]
role = "rtl-sources"
kind = "source-set.systemverilog"
materialization = "manifest"
members = ["rtl/top.sv"]

[[artifacts]]
role = "testbench"
kind = "source-set.systemverilog"
materialization = "manifest"
members = ["tb.sv"]

[[artifacts]]
role = "simulation-recipe"
kind = "recipe.simulation"
materialization = "manifest"
members = ["simulate.sh"]

[[artifacts]]
role = "constraints"
kind = "constraints.sdc"
materialization = "file"
members = ["mapped.sdc"]

[[artifacts]]
role = "synthesis-recipe"
kind = "recipe.synthesis"
materialization = "manifest"
members = ["run-dc-fixture.py", "synthesis.toml"]

[[artifacts]]
role = "reference-library-recipe"
kind = "recipe.reference-library"
materialization = "manifest"
members = ["run-fc-fixture.py", "build-reference.tcl", "site.lef"]

[[artifacts]]
role = "implementation-recipe"
kind = "recipe.physical-implementation"
materialization = "manifest"
members = ["run-fc-fixture.py", "place-route.tcl"]

[[artifacts]]
role = "electrical-sources"
kind = "source-set.spice"
materialization = "manifest"
members = ["electrical.sp"]

[[artifacts]]
role = "decks"
kind = "source-set.spice-deck"
materialization = "manifest"
members = ["deck.sp"]

[[artifacts]]
role = "electrical-recipe"
kind = "recipe.electrical-simulation"
materialization = "manifest"
members = ["electrical.sh"]

[[artifacts]]
role = "qualification-spec"
kind = "spec.qualification"
materialization = "file"
members = ["qualification.toml"]

[[artifacts]]
role = "qualification-recipe"
kind = "recipe.qualification"
materialization = "manifest"
members = ["qualification.py"]
''',
        encoding="utf-8",
    )
    (owner_root / "interface.toml").write_text(
        '''schema = 1
contract_kind = "ip-interface"
path_scope = "owner"
owner = "fixture"

[module]
name = "top"
''',
        encoding="utf-8",
    )
    (owner_root / "component.toml").write_text(
        '''schema = 1
contract_kind = "ip-component"
path_scope = "owner"
owner = "fixture"
name = "fc-fixture"
kind = "rtl-ip"

[filesets]
specification = ["owner/interface.toml"]
''',
        encoding="utf-8",
    )
    flow = owner_root / "flow.toml"
    flow.write_text(
        '''schema = 1
contract_kind = "flow"
path_scope = "owner"
owner = "fixture"
name = "fc-managed"

[[nodes]]
id = "assets"
action = "design.source-assets"
config = { source = "source-assets.toml" }

[[nodes]]
id = "reference-library"
action = "asic.reference-library-construction"
config = { runner = "run-fc-fixture.py", target = "library", top = "top" }
policy = "reference-library-quality"

[[nodes.bindings]]
input = "reference-library-recipe"
producer = "assets"
output = "reference-library-recipe"

[[nodes]]
id = "synthesis"
action = "asic.synthesis"
config = { runner = "run-dc-fixture.py" }
policy = "tool-pass"

[[nodes.bindings]]
input = "rtl-sources"
producer = "assets"
output = "rtl-sources"

[[nodes.bindings]]
input = "constraints"
producer = "assets"
output = "constraints"

[[nodes.bindings]]
input = "synthesis-recipe"
producer = "assets"
output = "synthesis-recipe"

[[nodes]]
id = "implementation"
action = "asic.physical-implementation"
config = { runner = "run-fc-fixture.py", target = "pnr", top = "top" }
policy = "implementation-regression"

[[nodes.bindings]]
input = "mapped-netlist"
producer = "synthesis"
output = "mapped-netlist"

[[nodes.bindings]]
input = "mapped-constraints"
producer = "synthesis"
output = "mapped-constraints"

[[nodes.bindings]]
input = "implementation-recipe"
producer = "assets"
output = "implementation-recipe"

[[nodes.bindings]]
input = "reference-library"
producer = "reference-library"
output = "reference-library"

[[targets]]
name = "implementation"
goals = ["implementation"]

[[policies]]
id = "tool-pass"

[[policies.checks]]
id = "passed"
fact = "passed"
operator = "equals"
expected = true

[[policies]]
id = "reference-library-quality"

[[policies.checks]]
id = "tool-completed"
fact = "tool-execution-completed"
operator = "equals"
expected = true

[[policies.checks]]
id = "no-library-errors"
fact = "library-check-error-count"
operator = "at_most"
expected = 0

[[policies.checks]]
id = "workspace-check-succeeded"
fact = "library-check-succeeded"
operator = "equals"
expected = true

[[policies]]
id = "implementation-regression"

[[policies.checks]]
id = "tool-completed"
fact = "tool-execution-completed"
operator = "equals"
expected = true

[[policies.checks]]
id = "no-design-check-errors"
fact = "design-check-error-count"
operator = "at_most"
expected = 0

[[policies.checks]]
id = "no-open-nets"
fact = "open-net-count"
operator = "at_most"
expected = 0

[[policies.checks]]
id = "no-route-drc"
fact = "route-drc-violation-count"
operator = "at_most"
expected = 0

[[policies]]
id = "physical-completion-readiness"

[[policies.checks]]
id = "tool-completed"
fact = "tool-execution-completed"
operator = "equals"
expected = true

[[policies.checks]]
id = "required-pg-ports"
fact = "required-pg-port-count"
operator = "equals"
expected = 2

[[policies.checks]]
id = "all-required-pg-ports-placed"
fact = "unplaced-required-pg-port-count"
operator = "at_most"
expected = 0

[[policies.checks]]
id = "pg-connectivity-checked"
fact = "pg-connectivity-check-performed"
operator = "equals"
expected = true

[[policies.checks]]
id = "no-pg-connectivity-violations"
fact = "pg-connectivity-violation-count"
operator = "at_most"
expected = 0

[[policies.checks]]
id = "antenna-check-active"
fact = "antenna-check-active"
operator = "equals"
expected = true

[[policies.checks]]
id = "no-antenna-violations"
fact = "antenna-violation-count"
operator = "at_most"
expected = 0

[[policies.checks]]
id = "tie-off-checked"
fact = "tie-off-check-performed"
operator = "equals"
expected = true

[[policies.checks]]
id = "no-tie-off-violations"
fact = "tie-off-violation-count"
operator = "at_most"
expected = 0
''',
        encoding="utf-8",
    )
    profile = owner_root / "profile.toml"
    profile.write_text(
        '''schema = 1
contract_kind = "execution-profile"
path_scope = "owner"
owner = "fixture"
name = "fc-fixture"

[actions."design.source-assets"]
adapter = "source-assets"

[actions."asic.synthesis"]
adapter = "synopsys-dc"

[actions."asic.synthesis".config]
output_root_environment = "SIGILICON_DC_BUILD_ROOT"
timeout_seconds = 30
reports = ["check_design.rpt"]

[actions."asic.synthesis".config.input_environment]
rtl-sources = "SIGILICON_RTL_SOURCES_FILE"
constraints = "SIGILICON_CONSTRAINTS"

[actions."asic.synthesis".config.qualifier_environment]
variant = "SIGILICON_DESIGN_VARIANT"

[actions."asic.synthesis".config.outputs]
mapped-netlist = "mapped.v"
mapped-constraints = "mapped.sdc"
checkpoint = "mapped.ddc"

[actions."asic.synthesis".platform_assets]
standard-cell-timing = "standard-cell-db@fixture"

[actions."asic.reference-library-construction"]
adapter = "synopsys-fc"

[actions."asic.reference-library-construction".config]
timeout_seconds = 30

[actions."asic.reference-library-construction".config.outputs]
reference-library = "reference.ndm"
library-check-report = "check_workspace.rpt"

[actions."asic.reference-library-construction".platform_assets]
physical-technology = "physical-technology@fixture"
standard-cell-physical = "standard-cell-lef@fixture"
standard-cell-timing = "standard-cell-db@fixture"

[actions."asic.physical-implementation"]
adapter = "synopsys-fc"

[actions."asic.physical-implementation".config]
timeout_seconds = 30

[actions."asic.physical-implementation".config.outputs]
routed-netlist = "routed.v"
routed-constraints = "routed.sdc"
layout-stream = "routed.gds"
checkpoint = "routed.ndm"
design-check-report = "check_design.rpt"
structural-report = "protected_inventory.tsv"
qor-report = "qor.rpt"
timing-report = "timing.rpt"
area-report = "area.rpt"
power-report = "power.rpt"
drc-report = "drc.rpt"
physical-completion-report = "physical_completion.rpt"
tie-off-check-report = "tie_off_check.rpt"

[actions."asic.physical-implementation".platform_assets]
physical-technology = "physical-technology@fixture"
''',
        encoding="utf-8",
    )
    catalog = owner_root / "catalog.toml"
    catalog.write_text(
        '''schema = 1
contract_kind = "flow-catalog"
path_scope = "owner"
owner = "fixture"

[flows.fc-managed]
contract = "flow.toml"
default_profile = "fc-fixture"

[flows.fc-managed.profiles]
fc-fixture = "profile.toml"
''',
        encoding="utf-8",
    )
    return catalog, profile


def _write_environment_contract(
    path: Path,
    environment: ExecutionEnvironment,
) -> None:
    lines = [
        "schema = 1",
        'contract_kind = "execution-environment"',
        'path_scope = "site"',
        'owner = "fixture-site"',
        'name = "fc-fixture-site"',
        "",
    ]
    for name, capability in environment.capabilities.items():
        lines.extend(
            (
                f'[capabilities."{name}"]',
                f'identity = "{capability.identity}"',
                f'executable = {json.dumps(str(capability.executable))}',
                "",
            )
        )
    for asset in environment.platform_assets:
        lines.extend(
            (
                "[[platform_assets]]",
                f'role = "{asset.role}"',
                f'kind = "{asset.kind}"',
                f'identity = "{asset.identity}"',
                "",
            )
        )
        for member in asset.members:
            lines.extend(
                (
                    "[[platform_assets.members]]",
                    f'role = "{member.role}"',
                    f'path = {json.dumps(str(member.location))}',
                    "",
                )
            )
    path.write_text("\n".join(lines), encoding="utf-8")


def test_public_cli_closes_fc_flow_reference_promotion_and_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    owner_root = tmp_path / "owner"
    _write_owner(owner_root, initialize_git=False)
    catalog, _profile = _write_cli_flow_owner(owner_root)
    (tmp_path / ".gitignore").write_text(
        "artifacts/\n.site/\n",
        encoding="utf-8",
    )
    (tmp_path / "catalogs" / "ip.toml").write_text(
        '''schema = 1
contract_kind = "ip-catalog"
path_scope = "repository"
owner = "repository"

[targets.fc-fixture]
contract = "owner/promotion.toml"

[components.fc-fixture]
contract = "owner/component.toml"
root = "owner"
''',
        encoding="utf-8",
    )
    subprocess.run(("git", "init", "-q"), cwd=tmp_path, check=True)
    subprocess.run(
        ("git", "config", "user.email", "fixture@example.com"),
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ("git", "config", "user.name", "Fixture"),
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(("git", "add", "."), cwd=tmp_path, check=True)
    subprocess.run(
        ("git", "commit", "-qm", "fixture source"),
        cwd=tmp_path,
        check=True,
    )
    source_commit = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=tmp_path,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()

    site_root = tmp_path / ".site"
    site_root.mkdir()
    environment, _collateral = _environment(site_root)
    environment_contract = site_root / "environment.toml"
    _write_environment_contract(environment_contract, environment)
    artifact_root = tmp_path / "artifacts"
    run_id = "c" * 32
    monkeypatch.chdir(tmp_path)

    def invoke(arguments: list[str]) -> dict[str, object]:
        assert sigilicon_cli_main(arguments) == 0
        captured = capsys.readouterr()
        assert captured.err == ""
        return json.loads(captured.out)

    selection = [
        str(catalog),
        "fc-managed",
        "implementation",
        "--owner-root",
        str(owner_root),
    ]
    plan = invoke(["flow", "plan", *selection])
    source = plan["nodes"][0]["source_assets"]["git"]  # type: ignore[index]
    assert source == {"commit": source_commit, "dirty": False}
    assert str(tmp_path) not in json.dumps(plan)

    preflight = invoke(
        [
            "flow",
            "preflight",
            *selection,
            "--environment",
            str(environment_contract),
        ]
    )
    assert preflight["status"] == "ready"
    assert str(tmp_path) not in json.dumps(preflight)

    result = invoke(
        [
            "flow",
            "run",
            *selection,
            "--environment",
            str(environment_contract),
            "--artifact-root",
            str(artifact_root),
            "--run-id",
            run_id,
        ]
    )
    assert result["status"] == "accepted"
    assert str(tmp_path) not in json.dumps(result)
    status = invoke(
        [
            "flow",
            "status",
            "--artifact-root",
            str(artifact_root),
            "fixture",
            "fc-managed",
            run_id,
        ]
    )
    assert status == result

    reference = result["nodes"]["reference-library"]["artifacts"][  # type: ignore[index]
        "reference-library"
    ]
    reference_digest = reference["digest"]
    (owner_root / "promotion.toml").write_text(
        f'''schema = 1
contract_kind = "ip-promotion"
path_scope = "owner"
owner = "fixture"

name = "fc-fixture"
producer = "owner"
component = "component.toml"
export = "fixture"
maturity = "development"
boundaries = []

[source]
commit = "{source_commit}"
dirty = false

[interface]
contract = "interface.toml"
logical = "top:rtl"
physical = "top:routed"

[conclusions]
implementation_regression = true
physical_completion_readiness = true
qualification = false
signoff = false

[[artifacts]]
schema = 1
contract_kind = "run-artifact-reference"
owner = "fixture"
flow = "fc-managed"
run_id = "{run_id}"
node = "reference-library"
role = "reference-library"
kind = "library.synopsys-ndm"
digest = "{reference_digest}"
required_policy = "reference-library-quality"
qualifiers = {{ variant = "fixture_variant", corner = "tt" }}

[[evidence]]
node = "implementation"
role = "implementation-regression"
policy = "implementation-regression"
evidence_role = "regression"
evaluation = "producer"

[[evidence]]
node = "implementation"
role = "physical-completion-readiness"
policy = "physical-completion-readiness"
evidence_role = "readiness"
evaluation = "run-policy"
''',
        encoding="utf-8",
    )
    promoted = invoke(["ip", "promote", "fc-fixture", "--json"])
    audited = invoke(["ip", "audit", "fc-fixture", "--json"])

    assert audited == promoted
    assert promoted["source"] == {"commit": source_commit, "dirty": False}
    assert promoted["conclusions"] == {
        "implementation_regression": True,
        "physical_completion_readiness": True,
        "qualification": False,
        "signoff": False,
    }
    promoted_reference = promoted["artifacts"][0]["reference"]  # type: ignore[index]
    assert promoted_reference == {
        "owner": "fixture",
        "flow": "fc-managed",
        "run_id": run_id,
        "node": "reference-library",
        "role": "reference-library",
        "kind": "library.synopsys-ndm",
        "qualifiers": {"variant": "fixture_variant", "corner": "tt"},
        "digest": reference_digest,
        "required_policy": "reference-library-quality",
    }
    assert str(tmp_path) not in json.dumps(promoted)


def test_physical_implementation_requires_fc_antenna_rules() -> None:
    registry = FlowRegistry()
    register_standard_asic_actions(registry)

    action = registry.action("asic.physical-implementation")
    physical_technology = next(
        requirement
        for requirement in action.platform_assets
        if requirement.role == "physical-technology"
    )

    assert physical_technology.members == (
        "tluplus",
        "gds-layer-map",
        "antenna-rules",
    )


def test_synopsys_fc_adapter_runs_separate_library_and_pnr_actions(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "owner"
    _write_owner(owner_root)
    spec, profile = _flow(owner_root)
    environment, _collateral = _environment(tmp_path)
    registry = _registry(owner_root)
    engine = FlowEngine(registry)
    plan = engine.plan(spec, "implementation", profile)

    assert plan.topology == ("assets", "reference-library", "implementation")
    result = engine.run(
        plan,
        artifact_root=tmp_path / "artifacts",
        environment=environment,
        run_id="a" * 32,
    )

    assert result.status == "accepted"
    reference = result.nodes["reference-library"]
    implementation = result.nodes["implementation"]
    assert set(reference.artifacts) == {
        "execution-evidence",
        "library-check-report",
        "reference-library",
    }
    assert set(implementation.artifacts) == {
        "area-report",
        "checkpoint",
        "design-check-report",
        "drc-report",
        "execution-evidence",
        "layout-stream",
        "power-report",
        "physical-completion-report",
        "tie-off-check-report",
        "qor-report",
        "routed-constraints",
        "routed-netlist",
        "timing-report",
        "structural-report",
    }
    reference_manifest = json.loads(
        reference.artifacts["reference-library"].path.read_text(encoding="utf-8")
    )
    checkpoint_manifest = json.loads(
        implementation.artifacts["checkpoint"].path.read_text(encoding="utf-8")
    )
    assert reference_manifest["root"] == "reference.ndm"
    assert checkpoint_manifest["root"] == "routed.ndm"
    assert reference_manifest["members"][0]["path"] == "library.ndm"
    assert checkpoint_manifest["members"][0]["path"] == "top.ndm"
    assert "digest" in reference_manifest["members"][0]
    assert "digest" not in checkpoint_manifest["members"][0]
    assert reference.artifacts["reference-library"].digest is not None
    assert all(
        artifact.digest is None
        for role, artifact in reference.artifacts.items()
        if role != "reference-library"
    )
    assert all(artifact.digest is None for artifact in implementation.artifacts.values())
    assert dict(implementation.facts) == {
        "tool-execution-completed": True,
        "design-check-error-count": 0,
        "design-check-warning-count": 3,
        "open-net-count": 0,
        "route-drc-violation-count": 0,
        "worst-setup-slack-ns": -0.04,
        "worst-hold-slack-ns": -0.08,
        "max-transition-violation-count": 2,
        "max-capacitance-violation-count": 3,
        "physical-cell-area-um2": 387.59,
        "leaf-cell-count": 418,
        "power-activity-mode": "scalar",
        "total-dynamic-power-nw": 95500.0,
        "cell-leakage-power-nw": 386.0,
        "antenna-check-active": True,
        "antenna-check-status": "active",
        "antenna-violation-count": 0,
        "tie-to-rail-check-performed": True,
        "tie-to-rail-check-status": "performed",
        "tie-to-rail-violation-count": 0,
        "tie-to-rail-direct-violation-count": 0,
        "tie-off-check-performed": True,
        "tie-off-check-status": "performed",
        "tie-off-violation-count": 0,
        "required-pg-port-count": 2,
        "placed-required-pg-port-count": 2,
        "unplaced-required-pg-port-count": 0,
        "pg-connectivity-check-performed": True,
        "pg-connectivity-check-status": "performed",
        "pg-connectivity-violation-count": 0,
    }
    for record in result.run_root.rglob("*.json"):
        assert str(tmp_path) not in record.read_text(encoding="utf-8")


def test_synopsys_fc_adapter_records_owner_runner_failure(tmp_path: Path) -> None:
    owner_root = tmp_path / "owner"
    _write_owner(owner_root, fail_pnr=True)
    spec, profile = _flow(owner_root)
    environment, _collateral = _environment(tmp_path)
    engine = FlowEngine(_registry(owner_root))

    result = engine.run(
        engine.plan(spec, "implementation", profile),
        artifact_root=tmp_path / "artifacts",
        environment=environment,
        run_id="b" * 32,
    )

    assert result.status == "failed"
    assert result.nodes["reference-library"].status == "accepted"
    implementation = result.nodes["implementation"]
    assert implementation.execution_status == "failed"
    assert implementation.result_status == "failed"
    assert implementation.artifacts == {}
    action_result = json.loads(
        (
            result.run_root
            / "nodes"
            / "implementation"
            / "action_result.json"
        ).read_text(encoding="utf-8")
    )
    assert action_result["execution"]["exit_code"] == 7
    assert str(tmp_path) not in json.dumps(action_result)


def test_library_manager_exit_zero_with_report_error_is_policy_rejected(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "owner"
    _write_owner(
        owner_root,
        library_report=(
            "Checking libraries...\n"
            "Error: timing libraries have the same PVT (LM-073)\n"
            "Workspace check succeeded!\n"
        ),
    )
    spec, profile = _flow(owner_root)
    environment, _collateral = _environment(tmp_path)
    engine = FlowEngine(_registry(owner_root))

    result = engine.run(
        engine.plan(spec, "reference-library", profile),
        artifact_root=tmp_path / "artifacts",
        environment=environment,
        run_id="e" * 32,
    )

    reference = result.nodes["reference-library"]
    assert reference.execution_status == "succeeded"
    assert reference.result_status == "valid"
    assert reference.facts["tool-execution-completed"] is True
    assert reference.facts["library-check-error-count"] == 1
    assert reference.policy_status == "rejected"
    assert reference.status == "rejected"


def test_failed_workspace_check_marker_is_policy_rejected(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "owner"
    _write_owner(owner_root, library_report="Workspace check failed!\n")
    spec, profile = _flow(owner_root)
    environment, _collateral = _environment(tmp_path)
    engine = FlowEngine(_registry(owner_root))

    result = engine.run(
        engine.plan(spec, "reference-library", profile),
        artifact_root=tmp_path / "artifacts",
        environment=environment,
        run_id="5" * 32,
    )

    reference = result.nodes["reference-library"]
    assert reference.execution_status == "succeeded"
    assert reference.result_status == "valid"
    assert reference.facts["library-check-error-count"] == 0
    assert reference.facts["library-check-succeeded"] is False
    assert reference.status == "rejected"


def test_rejected_reference_library_blocks_physical_implementation(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "owner"
    _write_owner(
        owner_root,
        library_report=(
            "Error: timing libraries have the same PVT (LM-073)\n"
            "Workspace check succeeded!\n"
        ),
    )
    spec, profile = _flow(owner_root)
    environment, _collateral = _environment(tmp_path)
    engine = FlowEngine(_registry(owner_root))

    result = engine.run(
        engine.plan(spec, "implementation", profile),
        artifact_root=tmp_path / "artifacts",
        environment=environment,
        run_id="4" * 32,
    )

    assert result.nodes["reference-library"].status == "rejected"
    implementation = result.nodes["implementation"]
    assert implementation.status == "blocked"
    assert implementation.execution_status is None
    assert "reference-library" in (implementation.reason or "")


def test_synopsys_fc_adapter_rejects_missing_library_report(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "owner"
    _write_owner(owner_root, omit_library_report=True)
    spec, profile = _flow(owner_root)
    environment, _collateral = _environment(tmp_path)
    engine = FlowEngine(_registry(owner_root))

    result = engine.run(
        engine.plan(spec, "reference-library", profile),
        artifact_root=tmp_path / "artifacts",
        environment=environment,
        run_id="f" * 32,
    )

    reference = result.nodes["reference-library"]
    assert result.status == "failed"
    assert reference.execution_status == "succeeded"
    assert reference.result_status == "failed"
    assert "library-check-report" in (reference.reason or "")


def test_synopsys_fc_adapter_rejects_malformed_library_report(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "owner"
    _write_owner(owner_root, library_report="Checking libraries...\n")
    spec, profile = _flow(owner_root)
    environment, _collateral = _environment(tmp_path)
    engine = FlowEngine(_registry(owner_root))

    result = engine.run(
        engine.plan(spec, "reference-library", profile),
        artifact_root=tmp_path / "artifacts",
        environment=environment,
        run_id="1" * 32,
    )

    reference = result.nodes["reference-library"]
    assert result.status == "failed"
    assert reference.execution_status == "succeeded"
    assert reference.result_status == "failed"
    assert "completion marker is missing" in (reference.reason or "")


def test_fc_exit_zero_with_design_check_error_is_policy_rejected(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "owner"
    _write_owner(
        owner_root,
        design_report=(
            "Total 2 EMS messages : 1 errors, 1 warnings, 0 info.\n"
            "Total 1 non-EMS messages : 0 errors, 1 warnings, 0 info.\n"
        ),
    )
    spec, profile = _flow(owner_root)
    environment, _collateral = _environment(tmp_path)
    engine = FlowEngine(_registry(owner_root))

    result = engine.run(
        engine.plan(spec, "implementation", profile),
        artifact_root=tmp_path / "artifacts",
        environment=environment,
        run_id="2" * 32,
    )

    assert result.nodes["reference-library"].status == "accepted"
    implementation = result.nodes["implementation"]
    assert implementation.execution_status == "succeeded"
    assert implementation.result_status == "valid"
    assert implementation.facts["design-check-error-count"] == 1
    assert implementation.policy_status == "rejected"
    assert implementation.status == "rejected"


def test_fc_exit_zero_with_incomplete_pg_is_policy_rejected(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "owner"
    _write_owner(
        owner_root,
        physical_completion_report=(
            "SIGILICON_PHYSICAL_COMPLETION_REPORT 1\n"
            "Required PG ports = 2\n"
            "Placed required PG ports = 0\n"
            "Unplaced required PG ports = 2\n"
            "PG connectivity check = performed\n"
            "PG connectivity violations = 4\n"
        ),
    )
    spec, profile = _flow(owner_root)
    environment, _collateral = _environment(tmp_path)
    engine = FlowEngine(_registry(owner_root))

    result = engine.run(
        engine.plan(spec, "implementation", profile),
        artifact_root=tmp_path / "artifacts",
        environment=environment,
        run_id="6" * 32,
    )

    implementation = result.nodes["implementation"]
    assert implementation.execution_status == "succeeded"
    assert implementation.result_status == "valid"
    assert implementation.facts["unplaced-required-pg-port-count"] == 2
    assert implementation.facts["pg-connectivity-violation-count"] == 4
    assert implementation.policy_status == "rejected"
    assert implementation.status == "rejected"


def test_synopsys_fc_adapter_rejects_malformed_implementation_report(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "owner"
    _write_owner(owner_root, design_report="design check finished\n")
    spec, profile = _flow(owner_root)
    environment, _collateral = _environment(tmp_path)
    engine = FlowEngine(_registry(owner_root))

    result = engine.run(
        engine.plan(spec, "implementation", profile),
        artifact_root=tmp_path / "artifacts",
        environment=environment,
        run_id="3" * 32,
    )

    implementation = result.nodes["implementation"]
    assert result.status == "failed"
    assert implementation.execution_status == "succeeded"
    assert implementation.result_status == "failed"
    assert "message summary is missing or duplicated" in (
        implementation.reason or ""
    )


def test_synopsys_fc_adapter_rejects_stale_reference_before_downstream_use(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "owner"
    _write_owner(owner_root)
    spec, profile = _flow(owner_root)
    environment, _collateral = _environment(tmp_path)
    registry = _registry(owner_root)
    engine = FlowEngine(registry)
    plan = engine.plan(spec, "implementation", profile)
    first = engine.run(
        plan,
        artifact_root=tmp_path / "artifacts",
        environment=environment,
        run_id="d" * 32,
    )
    reference_artifact = first.nodes["reference-library"].artifacts[
        "reference-library"
    ]
    reference_manifest = json.loads(
        reference_artifact.path.read_text(encoding="utf-8")
    )
    reference_member = (
        reference_artifact.path.parent
        / reference_manifest["root"]
        / reference_manifest["members"][0]["path"]
    )
    reference_member.write_text("stale reference library\n", encoding="utf-8")
    implementation_node = spec.node("implementation")
    implementation_action = registry.action(implementation_node.action_kind)
    bindings = {
        binding.input: first.nodes[binding.producer].artifacts[binding.output]
        for binding in implementation_node.bindings
    }
    validation_root = tmp_path / "validation"
    (validation_root / "work").mkdir(parents=True)
    (validation_root / "outputs").mkdir()
    context = ActionContext(
        node_id="implementation",
        action=implementation_action,
        run_root=first.run_root,
        node_root=validation_root,
        work_root=validation_root / "work",
        output_root=validation_root / "outputs",
        inputs={
            role: InputArtifact(
                role=role,
                kind=artifact.kind,
                path=artifact.path,
                digest=artifact.digest,
                producer=artifact.producer,
                qualifiers=artifact.qualifiers,
            )
            for role, artifact in bindings.items()
        },
        action_config=implementation_node.config,
        adapter_config=profile.selection(
            "asic.physical-implementation"
        ).config,
        capabilities={
            "tool.synopsys-fc": environment.capabilities["tool.synopsys-fc"]
        },
        platform_assets={
            "physical-technology": environment.platform_asset(
                "physical-technology"
            )
        },
    )

    diagnostics = registry.adapter("synopsys-fc").validate_inputs(context)

    assert any("missing or stale" in diagnostic for diagnostic in diagnostics)


def test_synopsys_fc_preflight_rejects_stale_recipe_and_platform(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "owner"
    _write_owner(owner_root)
    spec, profile = _flow(owner_root)
    environment, collateral = _environment(tmp_path)
    engine = FlowEngine(_registry(owner_root))
    plan = engine.plan(spec, "implementation", profile)

    (owner_root / "place-route.tcl").write_text(
        "# stale implementation fixture\n",
        encoding="utf-8",
    )
    source_preflight = engine.preflight(plan, environment)
    assert source_preflight.status == "blocked"
    assert any(
        check.requirement_kind == "git-source" and check.status == "changed"
        for check in source_preflight.checks
    )
    with pytest.raises(FlowExecutionError, match="preflight is blocked"):
        engine.run(
            plan,
            artifact_root=tmp_path / "source-artifacts",
            environment=environment,
            run_id="c" * 32,
        )
    assert not (tmp_path / "source-artifacts").exists()

    clean_plan = engine.plan(spec, "implementation", profile)
    collateral["tluplus"].write_text("stale TLU+ fixture\n", encoding="utf-8")
    platform_preflight = engine.preflight(clean_plan, environment)
    assert platform_preflight.status == "blocked"
    assert any(
        check.requirement == "physical-technology" and check.status == "stale"
        for check in platform_preflight.checks
    )
