from __future__ import annotations

import os
from pathlib import Path

import pytest

from sigilicon.backends.synopsys import DcBackend, HspiceBackend, VcsBackend
from sigilicon.execution import Resources, Step, StepContext


def _file(path: Path, text: str = "fixture\n", *, executable: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if executable:
        path.chmod(0o755)
    return path


def _context(
    tmp_path: Path,
    step: Step,
    environment: dict[str, str],
) -> StepContext:
    run_root = tmp_path / "run"
    roots = (
        run_root / "work" / step.id,
        run_root / "outputs" / step.id,
        run_root / "inputs/sources",
    )
    for root in roots:
        root.mkdir(parents=True, exist_ok=True)
    return StepContext(
        "1" * 64,
        step,
        "2" * 32,
        "3" * 64,
        roots[0],
        roots[1],
        roots[2],
        Resources(environment=environment),
        {},
    )


def test_vcs_backend_runs_one_sealed_owner_script(tmp_path: Path) -> None:
    sources = tmp_path / "run/inputs/sources"
    runner = _file(
        sources / "dv/run_vcs.sh",
        """#!/usr/bin/env bash
set -euo pipefail
test "$1" = rtl
test -x "$SIGILICON_SYNOPSYS_VCS"
test -s "$SIGILICON_VCS_RTL_FILELIST"
test -s "$SIGILICON_VCS_TESTBENCH_FILELIST"
printf 'managed vcs\n'
""",
        executable=True,
    )
    _file(sources / "rtl/design.sv")
    _file(sources / "dv/testbench.sv")
    executable = _file(tmp_path / "site/vcs", "#!/bin/sh\nexit 0\n", executable=True)
    step = Step(
        "rtl",
        "synopsys.vcs",
        {
            "runner": runner.relative_to(sources).as_posix(),
            "target": "rtl",
            "variant": "test",
            "rtl_root": "rtl",
            "testbench_root": "dv",
            "timeout_seconds": 10,
        },
        sources=("dv/run_vcs.sh", "rtl/design.sv", "dv/testbench.sv"),
    )
    environment = dict(os.environ)
    environment["SIGILICON_SYNOPSYS_VCS"] = str(executable)
    context = _context(tmp_path, step, environment)
    backend = VcsBackend()

    assert all(
        check.status == "ready"
        for check in backend.preflight(step, context.resources)
    )
    result = backend.run(context)

    assert result.status == "succeeded"
    assert {artifact.role for artifact in result.artifacts} == {"log"}
    assert "managed vcs" in result.artifacts[0].path.read_text()


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
            "rtl_root": "rtl",
            "testbench_root": "dv",
            "timeout_seconds": 10,
        },
        sources=("dv/run_vcs.sh", "rtl/design.sv", "dv/testbench.sv"),
    )
    environment = dict(os.environ)
    environment["SIGILICON_SYNOPSYS_VCS"] = str(executable)

    with pytest.raises(RuntimeError, match="changed during invocation"):
        VcsBackend().run(_context(tmp_path, step, environment))
    assert rtl.read_text(encoding="utf-8").endswith("tampered\n")


def test_dc_backend_collects_only_declared_delivery_files(tmp_path: Path) -> None:
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
""",
        executable=True,
    )
    _file(sources / "rtl/design.sv")
    _file(sources / "impl/syn/constraints.sdc")
    site = tmp_path / "site"
    executable = _file(site / "dc_shell", "#!/bin/sh\nexit 0\n", executable=True)
    environment = dict(os.environ)
    environment["SIGILICON_SYNOPSYS_DC_SHELL"] = str(executable)
    for flavor in ("RVT", "HVT", "LVT"):
        environment[f"SIGILICON_STDCELL_{flavor}_DB"] = str(
            _file(site / f"{flavor.lower()}.db")
        )
    step = Step(
        "synthesis",
        "synopsys.dc",
        {
            "runner": runner.relative_to(sources).as_posix(),
            "constraints": "impl/syn/constraints.sdc",
            "variant": "test",
            "rtl_root": "rtl",
            "timeout_seconds": 10,
            "reports": ("check_design.rpt", "area.rpt"),
        },
        sources=(
            "impl/syn/run_dc.sh",
            "impl/syn/constraints.sdc",
            "rtl/design.sv",
        ),
    )
    context = _context(tmp_path, step, environment)
    backend = DcBackend()

    assert all(
        check.status == "ready"
        for check in backend.preflight(step, context.resources)
    )
    result = backend.run(context)

    assert result.status == "succeeded"
    assert {artifact.role for artifact in result.artifacts} == {
        "log",
        "mapped-netlist",
        "mapped-constraints",
        "checkpoint",
        "report",
    }
    assert len(result.artifacts) == 7


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
"$SIGILICON_PYTHON" "$SIGILICON_COMPARATOR_QUALIFICATION_EVALUATOR"
""",
        executable=True,
    )
    _file(
        sources / "tools/evaluate.py",
        """import os
from pathlib import Path
Path(os.environ["SIGILICON_COMPARATOR_QUALIFICATION_OUTPUT"]).write_text(
    '{"passed":false}\\n', encoding="utf-8"
)
raise SystemExit(1)
""",
    )
    _file(sources / "configs/qualification.toml")
    site = tmp_path / "site"
    environment = dict(os.environ)
    environment["SIGILICON_SYNOPSYS_HSPICE"] = str(
        _file(site / "hspice", "#!/bin/sh\nexit 0\n", executable=True)
    )
    for name in (
        "SIGILICON_HSPICE_NOMINAL_MODEL",
        "SIGILICON_HSPICE_MISMATCH_MODEL",
        "SIGILICON_STDCELL_RVT_SPICE",
        "SIGILICON_STDCELL_HVT_SPICE",
        "SIGILICON_STDCELL_LVT_SPICE",
    ):
        environment[name] = str(_file(site / f"{name.lower()}.sp"))
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
            "environment_prefix": "COMPARATOR_",
            "environment": {"COMPARATOR_MISMATCH_SAMPLES": 2},
            "requires_mismatch": True,
            "requires_python": True,
            "source_environment": {
                "SIGILICON_COMPARATOR_QUALIFICATION_EVALUATOR": "tools/evaluate.py",
                "SIGILICON_COMPARATOR_QUALIFICATION_SPEC": "configs/qualification.toml",
            },
            "output_environment": {
                "SIGILICON_COMPARATOR_QUALIFICATION_OUTPUT": "qualification.json",
            },
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
    )
    context = _context(tmp_path, step, environment)
    backend = HspiceBackend()

    assert all(
        check.status == "ready"
        for check in backend.preflight(step, context.resources)
    )
    result = backend.run(context)

    assert result.status == "failed"
    assert {artifact.role for artifact in result.artifacts} == {
        "log",
        "campaign-summary",
        "qualification-evidence",
    }
