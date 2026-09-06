from __future__ import annotations

import json
import hashlib
import os
from dataclasses import replace
from pathlib import Path
import sys

import pytest

from sigilicon.adapters.synopsys.dc_adapter import DcAdapter
from sigilicon.adapters.synopsys.fc_adapter import FcAdapter
from sigilicon.adapters.synopsys.hspice_adapter import HspiceAdapter
from sigilicon.adapters.synopsys.vcs_adapter import VcsAdapter
from sigilicon.execution import Step
from sigilicon.execution._source import Source
from sigilicon.source import SourceReference
from sigilicon.project import Project
from sigilicon.execution._values import ContractError, ExecutionError
from sigilicon.execution._resources import Resources
from sigilicon.execution._plan import RuntimeEnvironment
from sigilicon.execution._io import ExecutionIO
from sigilicon.execution._result import Artifact, StepResult

from conftest import write_file as _file


def _context(
    tmp_path: Path,
    step: Step,
    resources: Resources,
    dependencies: dict[str, StepResult] | None = None,
) -> ExecutionIO:
    project_root = next(path for path in (tmp_path, *tmp_path.parents) if (path / "sigilicon.toml").is_file())
    adapters = {adapter.name: adapter for adapter in (VcsAdapter(), DcAdapter(), FcAdapter(), HspiceAdapter())}
    captured = []
    for path in step.sources:
        location = tmp_path / "run/inputs/sources" / path
        if not location.exists():
            _file(location)
        captured.append(replace(Source.capture(location, root=tmp_path / "run/inputs/sources"),
                                reference=SourceReference("fixture", path)))
    step = replace(step, source_closure=tuple(captured))
    step = replace(step, action=adapters[step.uses].prepare(Project.open(project_root), step, resources).action)
    run_root = tmp_path / "run"
    roots = (
        run_root / "work" / step.id,
        run_root / "outputs" / step.id,
        run_root / "inputs/sources",
    )
    for root in roots:
        root.mkdir(parents=True, exist_ok=True)
    return ExecutionIO(
        "1" * 64,
        step,
        "2" * 32,
        "3" * 64,
        run_root,
        resources,
        dependencies or {},
        owner="fixture",
    )


@pytest.mark.parametrize("adapter", (VcsAdapter(), DcAdapter(), FcAdapter()))
def test_synopsys_adapters_reject_unknown_configuration_fields(adapter, tmp_path: Path) -> None:
    common = {
        "runner": "flow/run.sh",
        "variant": "test",
        "timeout_seconds": 1,
    }
    configs = {
        "synopsys.vcs": {
            **common,
            "target": "rtl",
            "rtl_sources": [{"component": "fixture", "source": "rtl/design.sv"}],
            "testbench_sources": [{"component": "fixture", "source": "dv/testbench.sv"}],
            "success_marker": "passed",
        },
        "synopsys.dc": {
            **common,
            "constraints": "flow/constraints.sdc",
            "corner": "tt",
            "rtl_sources": [{"component": "fixture", "source": "rtl/design.sv"}],
        },
        "synopsys.fc": {
            **common,
            "target": "library",
            "corner": "tt",
        },
    }
    config = {**configs[adapter.name], "misspelled_field": True}
    step = Step(
        "check",
        adapter.name,
        config,
        sources=("flow/run.sh", "flow/constraints.sdc", "rtl/top.sv", "dv/tb.sv"),
        runtime=RuntimeEnvironment(tools={"SHELL": "runtime.bash"}),
    )

    with pytest.raises(ContractError, match="unknown config fields.*misspelled_field"):
        adapter.prepare(Project.open(tmp_path), step, Resources())


def test_vcs_backend_runs_one_sealed_owner_script(tmp_path: Path) -> None:
    sources = tmp_path / "run/inputs/sources"
    runner = _file(
        sources / "dv/run_vcs.sh",
        """#!/usr/bin/env bash
set -euo pipefail
test "$1" = rtl
test -x "$SIGILICON_SYNOPSYS_VCS"
test "$VCS_HOME" != /ambient/vcs
test "$VCS_ARCH_OVERRIDE" = linux
test -s "$SIGILICON_VCS_RTL_FILELIST"
mapfile -t rtl < "$SIGILICON_VCS_RTL_FILELIST"
test "${rtl[0]##*/}" = design.sv
test "${rtl[1]##*/}" = aaa_legacy.v
test -s "$SIGILICON_VCS_TESTBENCH_FILELIST"
mkdir -p "$SIGILICON_VCS_OUTPUT_ROOT/csrc" "$SIGILICON_VCS_OUTPUT_ROOT/simv.daidir"
printf 'archive\n' >"$SIGILICON_VCS_OUTPUT_ROOT/simv.daidir/archive.so"
ln -s ../simv.daidir/archive.so "$SIGILICON_VCS_OUTPUT_ROOT/csrc/archive.so"
printf 'managed vcs\n'
""",
        executable=True,
    )
    _file(sources / "rtl/design.sv")
    _file(sources / "rtl/aaa_legacy.v")
    _file(sources / "dv/testbench.sv")
    executable = _file(
        tmp_path / "site/vcs-home/bin/vcs",
        "#!/bin/sh\nexit 0\n",
        executable=True,
    )
    step = Step(
        "rtl",
        "synopsys.vcs",
        {
            "runner": runner.relative_to(sources).as_posix(),
            "target": "rtl",
            "variant": "test",
            "rtl_sources": [{"component": "fixture", "source": name} for name in ("rtl/design.sv", "rtl/aaa_legacy.v")],
            "testbench_sources": [{"component": "fixture", "source": "dv/testbench.sv"}],
            "success_marker": "managed vcs",
            "timeout_seconds": 10,
        },
        sources=("dv/run_vcs.sh", "rtl/design.sv", "rtl/aaa_legacy.v", "dv/testbench.sv"),
        runtime=RuntimeEnvironment(
            tools={
                "SIGILICON_RUNNER_SHELL": "runtime.bash",
                "SIGILICON_SYNOPSYS_VCS": "synopsys.vcs",
            }
        ),
    )
    resources = Resources(
        tools={"runtime.bash": "/bin/bash", "synopsys.vcs": str(executable)},
        environment={
            **os.environ,
            "VCS_HOME": "/ambient/vcs",
            "VCS_ARCH_OVERRIDE": "ambient",
        },
    )
    context = _context(tmp_path, step, resources)
    adapter = VcsAdapter()

    assert all(
        check.status == "ready"
        for check in adapter.preflight(context.step, context.runtime)
    )
    result = adapter.run(context)

    assert result.status == "succeeded"
    assert {artifact.role for artifact in result.artifacts} == {"log"}
    assert "managed vcs" in result.artifacts[0].path.read_text()
    assert not (context.work_directory / "tool").exists()

    missing = _context(tmp_path / "missing-marker", step, resources)
    _file(
        missing.source_directory / "dv/run_vcs.sh",
        "#!/usr/bin/env bash\nprintf 'incomplete vcs run\\n'\n",
        executable=True,
    )
    _file(missing.source_directory / "rtl/design.sv")
    _file(missing.source_directory / "rtl/aaa_legacy.v")
    _file(missing.source_directory / "dv/testbench.sv")

    failed = adapter.run(missing)

    assert failed.status == "failed"
    assert failed.message == "VCS runner omitted its declared success marker"

    for name, output in (
        ("duplicate", "managed vcs\nmanaged vcs\n"),
        ("nonterminal", "managed vcs\nlate failure\n"),
        ("substring", "prefix managed vcs\n"),
    ):
        invalid = _context(tmp_path / name, step, resources)
        _file(
            invalid.source_directory / "dv/run_vcs.sh",
            f"#!/usr/bin/env bash\nprintf '%s' {output!r}\n",
            executable=True,
        )
        _file(invalid.source_directory / "rtl/design.sv")
        _file(invalid.source_directory / "rtl/aaa_legacy.v")
        _file(invalid.source_directory / "dv/testbench.sv")

        rejected = adapter.run(invalid)

        assert rejected.status == "failed"


def test_owner_runner_cannot_mutate_a_watched_step_source(tmp_path: Path) -> None:
    sources = tmp_path / "run/inputs/sources"
    runner = _file(
        sources / "dv/run_vcs.sh",
        """#!/usr/bin/env bash
set -euo pipefail
source_file="$(head -n 1 "$SIGILICON_VCS_RTL_FILELIST")"
printf 'tampered\n' >>"$source_file"
""",
        executable=True,
    )
    rtl = _file(sources / "rtl/design.sv")
    _file(sources / "dv/testbench.sv")
    executable = _file(tmp_path / "site/vcs", "#!/bin/sh\nexit 0\n", executable=True)
    step = Step(
        "rtl",
        "synopsys.vcs",
        {
            "runner": runner.relative_to(sources).as_posix(),
            "target": "rtl",
            "variant": "test",
            "rtl_sources": [{"component": "fixture", "source": "rtl/design.sv"}],
            "testbench_sources": [{"component": "fixture", "source": "dv/testbench.sv"}],
            "success_marker": "must not reach completion",
            "timeout_seconds": 10,
        },
        sources=("dv/run_vcs.sh", "rtl/design.sv", "dv/testbench.sv"),
        runtime=RuntimeEnvironment(
            tools={
                "SIGILICON_RUNNER_SHELL": "runtime.bash",
                "SIGILICON_SYNOPSYS_VCS": "synopsys.vcs",
            },
        ),
    )
    resources = Resources(
        tools={"runtime.bash": "/bin/bash", "synopsys.vcs": str(executable)},
        environment=dict(os.environ),
    )

    with pytest.raises(RuntimeError, match="changed during invocation"):
        VcsAdapter().run(_context(tmp_path, step, resources))
    assert rtl.read_text(encoding="utf-8").endswith("tampered\n")


@pytest.mark.parametrize("mismatch", (None, "owner", "stage", "variant", "corner", "plan_identity", "run_id", "step_id"))
def test_dc_backend_collects_only_declared_delivery_files(
    tmp_path: Path, mismatch: str | None
) -> None:
    sources = tmp_path / "run/inputs/sources"
    runner = _file(
        sources / "impl/syn/run_dc.sh",
        """#!/usr/bin/env bash
set -euo pipefail
test -x "$SIGILICON_SYNOPSYS_DC_SHELL"
mkdir -p "$SIGILICON_DC_OUTPUT_ROOT"
for output in mapped.v mapped.sdc mapped.ddc check_design.rpt area.rpt; do
  printf '%s\n' "$output" >"$SIGILICON_DC_OUTPUT_ROOT/$output"
done
printf '{"schema":2,"plan_identity":"1111111111111111111111111111111111111111111111111111111111111111","run_id":"22222222222222222222222222222222","step_id":"synthesis","corner":"tt","contract_kind":"tool-verdict","owner":"fixture","stage":"synthesis","variant":"test","passed":true,"product_qualification_conclusion":false,"checks":{"timing_clean":true}}\n' \
  >"$SIGILICON_DC_OUTPUT_ROOT/verdict.json"
mkdir -p "$SIGILICON_DC_OUTPUT_ROOT/cache"
ln -s ../mapped.ddc "$SIGILICON_DC_OUTPUT_ROOT/cache/current.ddc"
""",
        executable=True,
    )
    _file(sources / "rtl/design.sv")
    _file(sources / "impl/syn/constraints.sdc")
    _file(sources / "tools/evaluate.py")
    site = tmp_path / "site"
    executable = site / "dc_shell"
    target = _file(
        site / "snps_shell",
        "#!/bin/sh\nexit 0\n",
        executable=True,
    )
    executable.symlink_to(target.name)
    runner.write_text(
        runner.read_text(encoding="utf-8").replace(
            'test -x "$SIGILICON_SYNOPSYS_DC_SHELL"',
            '"$SIGILICON_SYNOPSYS_DC_SHELL"',
        ),
        encoding="utf-8",
    )
    files: dict[str, str] = {}
    for flavor in ("RVT", "HVT", "LVT"):
        files[f"stdcell.{flavor.lower()}.db.tt"] = str(
            _file(site / f"{flavor.lower()}.db")
        )
    step = Step(
        "synthesis",
        "synopsys.dc",
        {
            "runner": runner.relative_to(sources).as_posix(),
            "constraints": "impl/syn/constraints.sdc",
            "variant": "test",
            "corner": "tt",
            "evaluator": "tools/evaluate.py",
            "rtl_sources": [{"component": "fixture", "source": "rtl/design.sv"}],
            "timeout_seconds": 10,
            "reports": ("check_design.rpt", "area.rpt"),
            "verdict_report": "verdict.json",
        },
        sources=(
            "impl/syn/run_dc.sh",
            "impl/syn/constraints.sdc",
            "rtl/design.sv",
            "tools/evaluate.py",
        ),
        runtime=RuntimeEnvironment(
            tools={
                "SIGILICON_RUNNER_SHELL": "runtime.bash",
                "SIGILICON_SYNOPSYS_DC_SHELL": "synopsys.dc-shell",
            },
            files={
                f"SIGILICON_STDCELL_{flavor}_DB": f"stdcell.{flavor.lower()}.db.tt"
                for flavor in ("RVT", "HVT", "LVT")
            },
        ),
    )
    context = _context(
        tmp_path,
        step,
        Resources(
            tools={
                "runtime.bash": "/bin/bash",
                "synopsys.dc-shell": str(executable),
            },
            files=files,
            environment=dict(os.environ),
        ),
    )
    adapter = DcAdapter()

    if mismatch is not None:
        expected = {"owner": "fixture", "stage": "synthesis", "variant": "test", "corner": "tt", "plan_identity": "1" * 64, "run_id": "2" * 32, "step_id": "synthesis"}
        runner.write_text(runner.read_text().replace(
            f'"{mismatch}":"{expected[mismatch]}"', f'"{mismatch}":"unrelated"'
        ))
        result = adapter.run(context)
        assert result.status == "failed"
        assert f"verdict {mismatch} mismatch" in result.message
        assert {artifact.role for artifact in result.artifacts} >= {"mapped-netlist", "execution-verdict"}
        return

    assert all(
        check.status == "ready"
        for check in adapter.preflight(context.step, context.runtime)
    )
    result = adapter.run(context)

    assert result.status == "succeeded"
    assert {artifact.role for artifact in result.artifacts} == {
        "log",
        "mapped-netlist",
        "mapped-constraints",
        "checkpoint",
        "report",
        "execution-verdict",
    }
    assert len(result.artifacts) == 8
    verdict = next(
        artifact for artifact in result.artifacts
        if artifact.role == "execution-verdict"
    )
    assert json.loads(verdict.path.read_text())["passed"] is True
    assert not (context.work_directory / "tool").exists()

    failed_context = _context(tmp_path / "failed-verdict", step, context.runtime)
    failed_runner = _file(
        failed_context.source_directory / "impl/syn/run_dc.sh",
        runner.read_text(encoding="utf-8").replace(
            '"passed":true', '"passed":false'
        ).replace('"timing_clean":true', '"timing_clean":false'),
        executable=True,
    )
    _file(failed_context.source_directory / "rtl/design.sv")
    _file(failed_context.source_directory / "impl/syn/constraints.sdc")
    _file(failed_context.source_directory / "tools/evaluate.py")
    failed = adapter.run(failed_context)

    assert failed.status == "failed"
    assert failed.message == "DC execution completed but owner evidence failed"
    assert {artifact.role for artifact in failed.artifacts} >= {
        "mapped-netlist",
        "execution-verdict",
    }
    failed_verdict = next(
        artifact for artifact in failed.artifacts
        if artifact.role == "execution-verdict"
    )
    assert json.loads(failed_verdict.path.read_text())["passed"] is False


def test_hspice_failure_preserves_campaign_and_qualification_evidence(
    tmp_path: Path,
) -> None:
    sources = tmp_path / "run/inputs/sources"
    runner = _file(
        sources / "verification/hspice/run_hspice.sh",
        """#!/usr/bin/env bash
set -euo pipefail
mkdir -p "$SIGILICON_HSPICE_OUTPUT_ROOT/common_mode_qualified"
printf '{}\n' >"$SIGILICON_HSPICE_OUTPUT_ROOT/common_mode_qualified/statistics.json"
printf 'simulator diagnostic\n' >"$SIGILICON_HSPICE_OUTPUT_ROOT/formal.lis"
ln -s statistics.json "$SIGILICON_HSPICE_OUTPUT_ROOT/common_mode_qualified/latest.json"
"$SIGILICON_PYTHON" "$SIGILICON_FIXTURE_QUALIFICATION_EVALUATOR"
""",
        executable=True,
    )
    _file(
        sources / "tools/evaluate.py",
        """import os
from pathlib import Path
Path(os.environ["SIGILICON_FIXTURE_QUALIFICATION_OUTPUT"]).write_text(
    '{"passed":false}\\n', encoding="utf-8"
)
raise SystemExit(1)
""",
    )
    _file(sources / "configs/qualification.toml")
    site = tmp_path / "site"
    executable = _file(site / "hspice", "#!/bin/sh\nexit 0\n", executable=True)
    files = {
        name: str(_file(site / f"{name}.sp"))
        for name in (
            "hspice.model.nominal",
            "hspice.model.mismatch",
            "stdcell.rvt.spice",
            "stdcell.hvt.spice",
            "stdcell.lvt.spice",
        )
    }
    step = Step(
        "qualification",
        "synopsys.hspice",
        {
            "runner": runner.relative_to(sources).as_posix(),
            "target": "formal",
            "variant": "test",
            "corner": "tt",
            "model_section": "TT",
            "timeout_seconds": 10,
            "environment_prefix": "FIXTURE_",
            "environment": {"FIXTURE_MISMATCH_SAMPLES": 2},
            "requires_python": True,
            "source_environment": {
                "SIGILICON_FIXTURE_QUALIFICATION_EVALUATOR": "tools/evaluate.py",
                "SIGILICON_FIXTURE_QUALIFICATION_SPEC": "configs/qualification.toml",
            },
            "output_environment": {
                "SIGILICON_FIXTURE_QUALIFICATION_OUTPUT": "qualification.json",
            },
            "diagnostics": {"log": "**/*.lis"},
            "collect": {
                "campaign-summary": "common_mode_qualified/statistics.json",
                "qualification-evidence": "qualification.json",
            },
        },
        sources=(
            "verification/hspice/run_hspice.sh",
            "tools/evaluate.py",
            "configs/qualification.toml",
        ),
        runtime=RuntimeEnvironment(
            tools={
                "SIGILICON_RUNNER_SHELL": "runtime.bash",
                "SIGILICON_PYTHON": "runtime.python",
                "SIGILICON_SYNOPSYS_HSPICE": "synopsys.hspice",
            },
            files={
                "SIGILICON_HSPICE_NOMINAL_MODEL": "hspice.model.nominal",
                "SIGILICON_HSPICE_MISMATCH_MODEL": "hspice.model.mismatch",
                "SIGILICON_STDCELL_RVT_SPICE": "stdcell.rvt.spice",
                "SIGILICON_STDCELL_HVT_SPICE": "stdcell.hvt.spice",
                "SIGILICON_STDCELL_LVT_SPICE": "stdcell.lvt.spice",
            },
        ),
    )
    context = _context(
        tmp_path,
        step,
        Resources(
            tools={
                "runtime.bash": "/bin/bash",
                "runtime.python": str(Path(sys.executable).resolve()),
                "synopsys.hspice": str(executable),
            },
            files=files,
            environment=dict(os.environ),
        ),
    )
    adapter = HspiceAdapter()

    assert all(
        check.status == "ready"
        for check in adapter.preflight(context.step, context.runtime)
    )
    with pytest.raises(ContractError, match="unknown config fields: requires_mismatch"):
        adapter.prepare(
            Project.open(tmp_path),
            replace(
                step,
                config={**step.config, "requires_mismatch": True},
            ),
            context.runtime,
        )
    result = adapter.run(context)

    assert result.status == "failed"
    assert {artifact.role for artifact in result.artifacts} == {
        "log",
        "campaign-summary",
        "qualification-evidence",
    }
    simulator_log = next(
        artifact for artifact in result.artifacts if artifact.path.name == "formal.lis"
    )
    assert simulator_log.read_text() == "simulator diagnostic\n"
    assert not (context.work_directory / "tool").exists()


def test_fc_backend_keeps_tool_scratch_outside_the_managed_run(
    tmp_path: Path,
) -> None:
    sources = tmp_path / "run/inputs/sources"
    runner = _file(
        sources / "impl/pnr/run_fc.sh",
        """#!/usr/bin/env bash
set -euo pipefail
test "$1" = library
mkdir -p "$SIGILICON_FC_WORK_ROOT/cache" "$SIGILICON_FC_REFERENCE_NDM" \
  "$(dirname "$SIGILICON_FC_LIBRARY_CHECK_REPORT")"
printf 'cache\n' >"$SIGILICON_FC_WORK_ROOT/cache/data"
ln -s data "$SIGILICON_FC_WORK_ROOT/cache/current"
printf 'ndm\n' >"$SIGILICON_FC_REFERENCE_NDM/library.ndm"
printf 'clean\n' >"$SIGILICON_FC_LIBRARY_CHECK_REPORT"
""",
        executable=True,
    )
    site = tmp_path / "site"
    lm_shell = _file(site / "lm_shell", "#!/bin/sh\nexit 0\n", executable=True)
    files = {
        name: str(_file(site / name, "fixture\n"))
        for name in (
            "synopsys.fc.tech-file",
            "synopsys.fc.tech-lef",
            "stdcell.rvt.lef",
            "stdcell.hvt.lef",
            "stdcell.lvt.lef",
            "stdcell.rvt.db.tt",
            "stdcell.hvt.db.tt",
            "stdcell.lvt.db.tt",
        )
    }
    step = Step(
        "reference-library",
        "synopsys.fc",
        {
            "runner": runner.relative_to(sources).as_posix(),
            "target": "library",
            "variant": "test",
            "corner": "tt",
            "top": "design",
            "reference_library_output": "test.ndm",
            "timeout_seconds": 10,
        },
        sources=("impl/pnr/run_fc.sh",),
        runtime=RuntimeEnvironment(
            tools={
                "SIGILICON_RUNNER_SHELL": "runtime.bash",
                "SIGILICON_SYNOPSYS_LM_SHELL": "synopsys.lm-shell",
            },
            files={
                "SIGILICON_FC_TECH_FILE": "synopsys.fc.tech-file",
                "SIGILICON_FC_TECH_LEF": "synopsys.fc.tech-lef",
                "SIGILICON_STDCELL_RVT_LEF": "stdcell.rvt.lef",
                "SIGILICON_STDCELL_HVT_LEF": "stdcell.hvt.lef",
                "SIGILICON_STDCELL_LVT_LEF": "stdcell.lvt.lef",
                "SIGILICON_STDCELL_RVT_DB": "stdcell.rvt.db.tt",
                "SIGILICON_STDCELL_HVT_DB": "stdcell.hvt.db.tt",
                "SIGILICON_STDCELL_LVT_DB": "stdcell.lvt.db.tt",
            },
        ),
    )
    context = _context(
        tmp_path,
        step,
        Resources(
            tools={
                "runtime.bash": "/bin/bash",
                "synopsys.lm-shell": str(lm_shell),
            },
            files=files,
            environment=dict(os.environ),
        ),
    )
    adapter = FcAdapter()

    assert all(
        check.status == "ready"
        for check in adapter.preflight(context.step, context.runtime)
    )
    result = adapter.run(context)

    assert result.status == "succeeded"
    assert {artifact.role for artifact in result.artifacts} == {
        "log",
        "reference-library",
        "library-check-report",
    }
    assert not (context.work_directory / "tool").exists()

    failed_context = _context(tmp_path / "failed", step, context.runtime)
    _file(
        failed_context.source_directory / "impl/pnr/run_fc.sh",
        """#!/usr/bin/env bash
mkdir -p "$SIGILICON_FC_REFERENCE_NDM"
printf 'partial\n' >"$SIGILICON_FC_REFERENCE_NDM/partial.ndm"
exit 1
""",
        executable=True,
    )

    failed = adapter.run(failed_context)

    assert failed.status == "failed"
    assert {artifact.role for artifact in failed.artifacts} == {"log", "reference-library"}
    partial = next(artifact for artifact in failed.artifacts if artifact.role == "reference-library")
    assert partial.read_text() == "partial\n"


@pytest.mark.parametrize("failure", ("runner", "missing-checkpoint"))
def test_fc_failed_implementation_preserves_generated_reports(tmp_path: Path, failure: str) -> None:
    sources = tmp_path / "run/inputs/sources"
    outputs = {
        "routed-netlist": "routed.v", "routed-constraints": "routed.sdc",
        "layout-stream": "routed.gds", "checkpoint": "routed.ndm",
        "design-check-report": "check.rpt", "structural-report": "structural.rpt",
        "qor-report": "qor.rpt", "timing-report": "timing.rpt",
        "area-report": "area.rpt", "power-report": "power.rpt",
        "drc-report": "drc.rpt", "physical-completion-report": "completion.rpt",
        "tie-off-check-report": "tie.rpt", "execution-verdict": "verdict.json",
    }
    script = """#!/bin/bash
set -eu
for name in SIGILICON_FC_ROUTED_NETLIST SIGILICON_FC_ROUTED_CONSTRAINTS SIGILICON_FC_GDS \
  SIGILICON_FC_DESIGN_CHECK_REPORT SIGILICON_FC_STRUCTURAL_REPORT SIGILICON_FC_QOR_REPORT \
  SIGILICON_FC_TIMING_REPORT SIGILICON_FC_AREA_REPORT SIGILICON_FC_POWER_REPORT \
  SIGILICON_FC_DRC_REPORT SIGILICON_FC_PHYSICAL_COMPLETION_REPORT \
  SIGILICON_FC_TIE_OFF_CHECK_REPORT SIGILICON_FC_EXECUTION_VERDICT; do
  mkdir -p "$(dirname "${!name}")"
  printf 'generated report\n' > "${!name}"
done
"""
    if failure == "runner":
        script += 'mkdir -p "$SIGILICON_FC_CHECKPOINT"\nprintf checkpoint > "$SIGILICON_FC_CHECKPOINT/cell"\nexit 1\n'
    _file(sources / "run.sh", script, executable=True)
    _file(sources / "evaluate.py", "raise SystemExit(1)\n")
    dependencies = {
        "synthesis": StepResult.succeeded(artifacts=(
            Artifact("mapped-netlist", "netlist.verilog", _file(tmp_path / "deps/mapped.v")),
            Artifact("mapped-constraints", "constraints.sdc", _file(tmp_path / "deps/mapped.sdc")),
        )),
        "reference": StepResult.succeeded(artifacts=(
            Artifact("reference-library", "library.synopsys-ndm", _file(tmp_path / "run/outputs/reference/reference-library/test.ndm/lib", ""),
                     size=0, sha256=hashlib.sha256(b"").hexdigest()),
        )),
    }
    step = Step("pnr", "synopsys.fc", {
        "runner": "run.sh", "evaluator": "evaluate.py", "target": "pnr",
        "variant": "test", "corner": "tt", "top": "top", "timeout_seconds": 10,
        "reference_library_output": "test.ndm", "synthesis_step": "synthesis",
        "reference_step": "reference", "outputs": outputs,
    }, needs=("synthesis", "reference"), sources=("run.sh", "evaluate.py"),
        runtime=RuntimeEnvironment(tools={
            "SIGILICON_RUNNER_SHELL": "runtime.bash", "SIGILICON_SYNOPSYS_FC_SHELL": "fc.shell",
        }))
    context = _context(tmp_path, step, Resources(tools={
        "runtime.bash": "/bin/bash",
        "fc.shell": str(_file(tmp_path / "bin/fc", "#!/bin/sh\nexit 0\n", executable=True)),
    }), dependencies)

    result = FcAdapter().run(context)

    assert result.status == "failed"
    report = next(artifact for artifact in result.artifacts if artifact.role == "timing-report")
    assert report.read_text() == "generated report\n"
    assert next(artifact for artifact in result.artifacts if artifact.role == "layout-stream").kind == "layout.gds"
    if failure == "runner":
        checkpoint = next(artifact for artifact in result.artifacts if artifact.role == "checkpoint")
        import tarfile
        with tarfile.open(checkpoint.path) as archive:
            assert archive.extractfile("routed.ndm/cell").read() == b"checkpoint"
