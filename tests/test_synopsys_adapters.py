from __future__ import annotations

import json
import hashlib
import os
from dataclasses import replace
from pathlib import Path
import sys

import pytest

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
    adapters = {adapter.name: adapter for adapter in (VcsAdapter(), FcAdapter(), HspiceAdapter())}
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


@pytest.mark.parametrize("adapter", (VcsAdapter(), FcAdapter()))
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
            "success_marker": "passed",
            "hdl": {"top": "design", "sources": [{"component": "fixture", "source": "rtl/design.sv"}] + [{"component": "fixture", "source": "dv/testbench.sv"}]}
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
test -s "$SIGILICON_HDL_FILELIST"
mapfile -t rtl < "$SIGILICON_HDL_FILELIST"
test "${rtl[0]##*/}" = design.sv
test "${rtl[1]##*/}" = aaa_legacy.v
test -s "$SIGILICON_HDL_CONTRACT"
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
            "success_marker": "managed vcs",
            "timeout_seconds": 10,
            "hdl": {"top": "design", "sources": [{"component": "fixture", "source": name} for name in ("rtl/design.sv", "rtl/aaa_legacy.v")] + [{"component": "fixture", "source": "dv/testbench.sv"}]}
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
source_file="$(head -n 1 "$SIGILICON_HDL_FILELIST")"
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
            "success_marker": "must not reach completion",
            "timeout_seconds": 10,
            "hdl": {"top": "design", "sources": [{"component": "fixture", "source": "rtl/design.sv"}] + [{"component": "fixture", "source": "dv/testbench.sv"}]}
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
