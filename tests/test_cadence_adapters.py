from __future__ import annotations

from pathlib import Path
import sys

import pytest

from sigilicon.adapters.cadence import cadence_adapters
from sigilicon.adapters.cadence.oa_adapter import NativeOaAdapter
from sigilicon.adapters.cadence.rtl_adapter import XceliumAdapter
from sigilicon.execution import Step
from sigilicon.execution._resources import Resources

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
    root = tmp_path
    _file(root / "ip/example/rtl/design.sv", "module design; endmodule\n")
    _file(root / "ip/example/dv/testbench.sv", "module testbench; endmodule\n")
    _file(root / "ip/example/operations.toml", f'''schema = 4
contract_kind = "owner-operations"
path_scope = "owner"
owner = "example"
[operations.rtl]
uses = "cadence.xcelium"
filesets = ["rtl"]
config = {{ success_marker = "{marker}", timeout_seconds = 10 }}
''')
    component = write_component_owner(root, "example", filesets={
        "rtl": ("ip/example/rtl/design.sv", "ip/example/dv/testbench.sv"),
        "operations": ("ip/example/operations.toml",),
    })
    component.write_text(component.read_text().replace("[sources]", 'operation_catalog = "source_2"\n[sources]'))
    with (root / "sigilicon.toml").open("a") as stream:
        stream.write(f'\n[runtime.tools]\n"cadence.xrun" = "{executable}"\n')
    project = Project.open(root)
    plan = project.plan("example:rtl")
    assert project.preflight(plan).status == "ready"
    result = project.run(plan)
    assert result.status == "succeeded"
    assert {artifact.kind for artifact in result.outcomes[0].result.artifacts} >= {"summary.cadence-xcelium"}


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
    spectre = _file(tmp_path / "tools/spectre", executable=True)
    ready = NativeOaAdapter().preflight(
        step,
        Resources(
            capabilities=capabilities,
            tools={
                "cadence.virtuoso": str(executable),
                "cadence.spectre": str(spectre),
                "runtime.python": sys.executable,
            },
            values=values,
        ),
    )

    assert all(check.status == "ready" for check in ready)
