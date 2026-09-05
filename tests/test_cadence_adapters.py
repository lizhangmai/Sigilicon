from __future__ import annotations

import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from sigilicon.adapters.cadence import cadence_adapters
from sigilicon.adapters.cadence.layout_adapter import LayoutAdapter
from sigilicon.adapters.cadence.oa_adapter import NativeOaAdapter
from sigilicon.adapters.cadence.rtl_adapter import XceliumAdapter
from sigilicon.execution import Step
from sigilicon.execution._model import (
    ContractError,
    ExecutionIO,
)
from sigilicon.execution._model import Resources

from conftest import write_file as _file, write_component_owner
from sigilicon.project import Project
from sigilicon.cli.main import main


@pytest.mark.parametrize(("config", "hdl", "error"), (
    ('timeout_seconds = 1', True, "success_marker"),
    ('success_marker = "DONE", timeout_seconds = 0', True, "timeout_seconds"),
    ('success_marker = "DONE", timeout_seconds = 1, unknown = 1', True, "unknown"),
    ('success_marker = "DONE", timeout_seconds = 1', False, "no Verilog sources"),
))
def test_xcelium_static_contract_fails_during_plan_and_check(
    tmp_path: Path, config: str, hdl: bool, error: str,
) -> None:
    _file(tmp_path / "ip/fixture/top.sv", "module top; endmodule\n")
    _file(tmp_path / "ip/fixture/operations.toml", f'''schema = 4
contract_kind = "owner-operations"
path_scope = "owner"
owner = "fixture"
[operations.rtl]
uses = "cadence.xcelium"
filesets = ["rtl"]
config = {{ {config} }}
''')
    component = write_component_owner(tmp_path, "fixture", filesets={
        "operation": ("ip/fixture/operations.toml",),
        "rtl": ("ip/fixture/top.sv",) if hdl else ("ip/fixture/operations.toml",),
    })
    component.write_text(component.read_text().replace("[sources]", 'operation_catalog = "source_0"\n[sources]'))
    with (tmp_path / "sigilicon.toml").open("a") as stream:
        stream.write('\n[runtime.tools]\n"cadence.xrun" = "/bin/true"\n')
    with pytest.raises(ValueError, match=error):
        Project.open(tmp_path).plan("fixture:rtl")
    assert main(["check", "--project-root", str(tmp_path)]) != 0


def test_oa_operations_have_fixed_backend_identities() -> None:
    names = {adapter.name for adapter in cadence_adapters()}

    assert "cadence.spectre" in names
    assert {
        "cadence.oa-check",
        "cadence.oa-rebuild",
        "cadence.oa-attest",
    }.issubset(names)


def _context(
    tmp_path: Path,
    step: Step,
    resources: Resources,
    *,
    project_root: Path | None = None,
    owner_root: Path | None = None,
    workspace_root: Path | None = None,
    scopes: dict[str, str] | None = None,
    register_operation=None,
) -> ExecutionIO:
    runtime_step = step
    run_root = tmp_path / "run"
    work = run_root / "work" / runtime_step.id
    output = run_root / "outputs" / runtime_step.id
    sources = run_root / "inputs/sources"
    for root in (work, output, sources):
        root.mkdir(parents=True, exist_ok=True)
    return ExecutionIO(
        "1" * 64,
        runtime_step,
        "2" * 32,
        "3" * 64,
        run_root,
        resources,
        {},
        owner="fixture",
        _source_scopes={} if scopes is None else scopes,
        _register_mutation=register_operation,
    )


def _fake_xrun(tmp_path: Path, marker: str) -> Path:
    return _file(
        tmp_path / "site/xcelium/tools/bin/xrun",
        f"""#!/bin/sh
set -eu
log=''
while [ "$#" -gt 0 ]; do
  if [ "$1" = '-log' ]; then
    shift
    log="$1"
  fi
  shift
done
printf '%s\\n' {marker!r}
printf 'native complete\\n' >"$log"
""",
        executable=True,
    )


def test_cadence_executable_does_not_fall_back_to_ambient_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executable = _file(tmp_path / "ambient/xrun", executable=True)
    monkeypatch.setenv("PATH", str(executable.parent))
    resources = Resources(
        environment={"PATH": str(executable.parent)},
    )
    step = Step(
        "rtl",
        "cadence.xcelium",
        {"success_marker": "RTL_SUMMARY failures=0", "timeout_seconds": 10},
        sources=("rtl/design.sv",),
    )

    checks = XceliumAdapter().preflight(step, resources)

    assert any(
        check.subject == "cadence.xrun" and check.status == "blocked"
        for check in checks
    )


def test_xcelium_backend_requires_explicit_sources_and_completion_marker(
    tmp_path: Path,
) -> None:
    marker = "RTL_SUMMARY failures=0"
    executable = _fake_xrun(tmp_path, marker)
    step = Step(
        "rtl",
        "cadence.xcelium",
        {
            "success_marker": marker,
            "timeout_seconds": 10,
        },
        sources=("rtl/design.sv", "dv/testbench.sv"),
    )
    resources = Resources(
        tools={"cadence.xrun": str(executable)},
        environment=dict(os.environ),
    )
    project = tmp_path / "project"
    owner = project / "ip/example"
    workspace = project / "workspace"
    owner.mkdir(parents=True)
    workspace.mkdir()
    context = _context(
        tmp_path,
        step,
        resources,
        project_root=project,
        owner_root=owner,
        workspace_root=workspace,
        scopes={source: "owner" for source in step.sources},
    )
    _file(context.source_directory / "rtl/design.sv", "module design; endmodule\n")
    _file(context.source_directory / "dv/testbench.sv", "module testbench; endmodule\n")
    adapter = XceliumAdapter()

    assert all(check.status == "ready" for check in adapter.preflight(step, resources))
    result = adapter.run(context)

    assert result.status == "succeeded"
    assert len(result.artifacts) == 4
    assert not (context.work_directory / "xcelium.d").exists()


def _oa_context(
    tmp_path: Path,
    step: Step,
    *,
    registered: list[object],
    tools: dict[str, str] | None = None,
) -> ExecutionIO:
    project = tmp_path / "source-project"
    owner = project / "ip/example"
    workspace = tmp_path / "oa-workspace"
    owner.mkdir(parents=True)
    workspace.mkdir()
    runtime_step = step
    virtuoso = _file(tmp_path / "site/virtuoso", executable=True)
    context = _context(
        tmp_path,
        runtime_step,
        Resources(
            capabilities=frozenset(
                {"tool.virtuoso-bridge", "license.cadence-oa"}
            ),
            tools={
                "cadence.virtuoso": str(virtuoso),
                "runtime.python": sys.executable,
                **(tools or {}),
            },
            values={
                "virtuoso-bridge.host": "127.0.0.1",
                "virtuoso-bridge.port": "65432",
            },
        ),
        project_root=project,
        owner_root=owner,
        workspace_root=workspace,
        scopes={source: "owner" for source in runtime_step.sources},
        register_operation=registered.append,
    )
    for source in runtime_step.sources:
        _file(context.source_directory / source)
        _file(owner / source)
    return context


def test_native_oa_preflight_requires_explicit_virtuoso_executable(
    tmp_path: Path,
) -> None:
    step = Step(
        "native",
        "cadence.native-oa",
        {"owner": "example", "testbench": "tb_EXAMPLE", "timeout_seconds": 10},
        sources=("configs/oa.toml",),
    )
    values = {
        "virtuoso-bridge.host": "127.0.0.1",
        "virtuoso-bridge.port": "65432",
    }
    capabilities = frozenset({"tool.virtuoso-bridge", "license.cadence-oa"})

    blocked = NativeOaAdapter().preflight(
        step,
        Resources(capabilities=capabilities, values=values),
    )

    assert any(
        check.subject == "cadence.virtuoso"
        and check.status == "blocked"
        for check in blocked
    )

    executable = _file(tmp_path / "tools/virtuoso", executable=True)
    ready = NativeOaAdapter().preflight(
        step,
        Resources(
            capabilities=capabilities,
            tools={
                "cadence.virtuoso": str(executable),
                "runtime.python": sys.executable,
            },
            values=values,
        ),
    )

    assert all(check.status == "ready" for check in ready)


def test_layout_backend_rejects_typed_source_snapshot_drift(
    monkeypatch,
    tmp_path: Path,
) -> None:
    step = Step(
        "layout",
        "cadence.layout",
        {
            "owner": "example",
            "spec": "design/CELL/layout.toml",
            "timeout_seconds": 10,
        },
        sources=("design/CELL/layout.toml",),
    )
    context = _oa_context(tmp_path, step, registered=[])
    project_root = tmp_path / "source-project"
    owner_root = project_root / "ip/example"
    workspace_root = tmp_path / "oa-workspace"
    source = owner_root / "design/CELL/layout.toml"
    project = SimpleNamespace(
        project_root=project_root,
        artifact_root=project_root / "artifacts",
        workspace_root=workspace_root,
        owner=lambda _name: SimpleNamespace(root=owner_root),
    )
    planning = SimpleNamespace(
        source_records={source: "stale typed snapshot\n"},
        spec=SimpleNamespace(
            library="FIXTURE",
            cell="CELL",
            view="layout",
            generator="fixture",
            stage="source",
            pdk=object(),
        ),
    )
    monkeypatch.setattr(
        "sigilicon.domain.platform.load_platforms",
        lambda _project, *, resources: object(),
    )
    monkeypatch.setattr(
        "sigilicon.adapters.cadence.layout_generation.plan_layout_spec",
        lambda _spec, *, project, platform: planning,
    )

    with pytest.raises(ContractError, match="typed adapter source snapshot drift"):
        LayoutAdapter().prepare(project, step, context.runtime)
