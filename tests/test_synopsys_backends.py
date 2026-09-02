from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from sigilicon.backends.synopsys import (
    DcBackend,
    FcBackend,
    HspiceBackend,
    StructuralLinkBackend,
    VcsBackend,
)
from sigilicon.execution import PreparedStep, Resources, StepContext, StepResult
from sigilicon.execution.model import resource_materialization_key


def _file(path: Path, text: str = "fixture\n", *, executable: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if executable:
        path.chmod(0o755)
    return path


def _context(
    tmp_path: Path,
    step: PreparedStep,
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
mkdir -p "$SIGILICON_VCS_OUTPUT_ROOT/csrc" "$SIGILICON_VCS_OUTPUT_ROOT/simv.daidir"
printf 'archive\n' >"$SIGILICON_VCS_OUTPUT_ROOT/simv.daidir/archive.so"
ln -s ../simv.daidir/archive.so "$SIGILICON_VCS_OUTPUT_ROOT/csrc/archive.so"
printf 'managed vcs\n'
""",
        executable=True,
    )
    _file(sources / "rtl/design.sv")
    _file(sources / "dv/testbench.sv")
    executable = _file(tmp_path / "site/vcs", "#!/bin/sh\nexit 0\n", executable=True)
    step = PreparedStep(
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
    result = backend.run(context, step)

    assert result.status == "succeeded"
    assert {artifact.role for artifact in result.artifacts} == {"log"}
    assert "managed vcs" in result.artifacts[0].path.read_text()
    assert not (context.work_root / "tool").exists()


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
    step = PreparedStep(
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
        VcsBackend().run(_context(tmp_path, step, environment), step)
    assert rtl.read_text(encoding="utf-8").endswith("tampered\n")


def test_structural_link_run_consumes_the_prepared_record_without_replanning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest_resource = "release:fixture:object/manifest"
    liberty_resource = "release:fixture:object/role/raw-macro-liberty"
    manifest_text = "{}\n"
    liberty_text = "library(fixture) {}\n"
    manifest_digest = hashlib.sha256(manifest_text.encode()).hexdigest()
    liberty_digest = hashlib.sha256(liberty_text.encode()).hexdigest()
    owner_sources = (
        "configs/dependency.lock.toml",
        "configs/variant.toml",
        "compile.tcl",
        "link.tcl",
        "rtl/top.sv",
    )
    source_root = tmp_path / "run/inputs/sources"
    for name in owner_sources:
        _file(source_root / name)
    resource_root = tmp_path / "run/inputs/resources"
    _file(resource_root / resource_materialization_key(manifest_resource), manifest_text)
    _file(resource_root / resource_materialization_key(liberty_resource), liberty_text)
    config = {
        "owner": "example",
        "dependency": "provider",
        "dependency_lock": owner_sources[0],
        "variant": "default",
        "variant_contract": owner_sources[1],
        "compile_script": owner_sources[2],
        "link_script": owner_sources[3],
        "library_name": "fixture",
        "macro_cell": "MACRO",
        "parameter_overrides": {"ROWS": 1},
        "expected_macro_instances": 1,
        "expected_unresolved_references": 0,
        "library_compiler_version": "U-2022.12-SP6-T-20250827",
        "release_export": "macro",
        "liberty_role": "raw_macro_liberty_or_db",
        "timeout_seconds": 10,
    }
    prepared = {
        "owner": "example",
        "variant": "default",
        "top": "top",
        "rtl_sources": [owner_sources[4]],
        "compile_script": owner_sources[2],
        "link_script": owner_sources[3],
        "library_name": "fixture",
        "macro_cell": "MACRO",
        "parameter_overrides": {"ROWS": 1},
        "expected_macro_instances": 1,
        "expected_unresolved_references": 0,
        "library_compiler_version": "U-2022.12-SP6-T-20250827",
        "release_id": "development-" + "a" * 40,
        "release_source_commit": "a" * 40,
        "release_store": "fixture",
        "release_object": "sha256-" + manifest_digest,
        "release_manifest_resource": manifest_resource,
        "release_manifest_sha256": manifest_digest,
        "release_liberty_resource": liberty_resource,
        "release_liberty_sha256": liberty_digest,
    }
    step = PreparedStep(
        "link",
        "synopsys.structural-link",
        {"config": config, "prepared": prepared},
        sources=owner_sources,
        resources=(manifest_resource, liberty_resource),
    )
    project_root = tmp_path / "project"
    owner_root = project_root / "ip/example"
    workspace_root = tmp_path / "workspace"
    owner_root.mkdir(parents=True)
    workspace_root.mkdir()
    context = StepContext(
        "1" * 64,
        step,
        "2" * 32,
        "3" * 64,
        tmp_path / "run/work/link",
        tmp_path / "run/outputs/link",
        source_root,
        Resources(),
        {},
        project_root=project_root,
        owner_root=owner_root,
        workspace_root=workspace_root,
        source_scopes={name: "owner" for name in owner_sources},
        resource_root=resource_root,
        resource_digests={
            manifest_resource: manifest_digest,
            liberty_resource: liberty_digest,
        },
    )
    backend = StructuralLinkBackend()
    observed = []
    monkeypatch.setattr(
        "sigilicon.workflows.structural_link.plan_structural_link",
        lambda **_kwargs: pytest.fail("run replanned a prepared structural step"),
    )
    monkeypatch.setattr(
        backend,
        "_execute",
        lambda _context, plan: observed.append(plan) or StepResult.succeeded(),
    )

    result = backend.run(context, step)

    assert result.status == "succeeded"
    assert observed[0].top == "top"
    assert observed[0].release_liberty == (
        resource_root / resource_materialization_key(liberty_resource)
    )


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
mkdir -p "$SIGILICON_DC_OUTPUT_ROOT/cache"
ln -s ../mapped.ddc "$SIGILICON_DC_OUTPUT_ROOT/cache/current.ddc"
""",
        executable=True,
    )
    _file(sources / "rtl/design.sv")
    _file(sources / "impl/syn/constraints.sdc")
    site = tmp_path / "site"
    executable = site / "dc_shell"
    target = _file(
        site / "snps_shell",
        f'''#!/bin/sh
test "$0" = "{executable}"
''',
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
    environment = dict(os.environ)
    environment["SIGILICON_SYNOPSYS_DC_SHELL"] = str(executable)
    for flavor in ("RVT", "HVT", "LVT"):
        environment[f"SIGILICON_STDCELL_{flavor}_DB"] = str(
            _file(site / f"{flavor.lower()}.db")
        )
    step = PreparedStep(
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
    result = backend.run(context, step)

    assert result.status == "succeeded"
    assert {artifact.role for artifact in result.artifacts} == {
        "log",
        "mapped-netlist",
        "mapped-constraints",
        "checkpoint",
        "report",
    }
    assert len(result.artifacts) == 7
    assert not (context.work_root / "tool").exists()


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
ln -s statistics.json "$SIGILICON_HSPICE_OUTPUT_ROOT/common_mode_qualified/latest.json"
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
    step = PreparedStep(
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
    result = backend.run(context, step)

    assert result.status == "failed"
    assert {artifact.role for artifact in result.artifacts} == {
        "log",
        "campaign-summary",
        "qualification-evidence",
    }
    assert not (context.work_root / "tool").exists()


def test_fc_backend_keeps_tool_scratch_outside_the_managed_run(
    tmp_path: Path,
) -> None:
    sources = tmp_path / "run/inputs/sources"
    runner = _file(
        sources / "impl/pnr/run_fc.sh",
        """#!/usr/bin/env bash
set -euo pipefail
test "$1" = library
mkdir -p "$SIGILICON_FC_WORK_ROOT/cache" "$SIGILICON_FC_REFERENCE_NDM"
printf 'cache\n' >"$SIGILICON_FC_WORK_ROOT/cache/data"
ln -s data "$SIGILICON_FC_WORK_ROOT/cache/current"
printf 'ndm\n' >"$SIGILICON_FC_REFERENCE_NDM/library.ndm"
printf 'clean\n' >"$SIGILICON_FC_LIBRARY_CHECK_REPORT"
""",
        executable=True,
    )
    site = tmp_path / "site"
    environment = dict(os.environ)
    for name in (
        "SIGILICON_SYNOPSYS_LM_SHELL",
        "SIGILICON_FC_TECH_FILE",
        "SIGILICON_FC_TECH_LEF",
        "SIGILICON_STDCELL_RVT_LEF",
        "SIGILICON_STDCELL_HVT_LEF",
        "SIGILICON_STDCELL_LVT_LEF",
        "SIGILICON_STDCELL_RVT_DB",
        "SIGILICON_STDCELL_HVT_DB",
        "SIGILICON_STDCELL_LVT_DB",
    ):
        environment[name] = str(
            _file(
                site / name.lower(),
                "#!/bin/sh\nexit 0\n" if name.endswith("LM_SHELL") else "fixture\n",
                executable=name.endswith("LM_SHELL"),
            )
        )
    step = PreparedStep(
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
    )
    context = _context(tmp_path, step, environment)
    backend = FcBackend()

    assert all(
        check.status == "ready"
        for check in backend.preflight(step, context.resources)
    )
    result = backend.run(context, step)

    assert result.status == "succeeded"
    assert {artifact.role for artifact in result.artifacts} == {
        "log",
        "reference-library",
        "library-check-report",
    }
    assert not (context.work_root / "tool").exists()
