from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from sigilicon.execution._workspace import ExecutionWorkspace
from sigilicon.execution._model import Resources
from sigilicon.adapters.synopsys.structural_link import (
    execute_structural_link,
    plan_structural_link,
)
from sigilicon.adapters.synopsys import structural_link as structural_link_workflow
from sigilicon.external_tools import ProcessRequest, managed_process, owned_executable


_COMPAT_LC_VERSION = "U-2022.12-SP6-T-20250827"


def _write(path: Path, text: str, *, executable: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if executable:
        path.chmod(0o755)
    return path


def _tool_resources(library_compiler: Path, design_compiler: Path) -> Resources:
    return Resources(
        tools={
            "fixture.library-compiler": str(library_compiler),
            "fixture.design-compiler": str(design_compiler),
        }
    )


def _fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    tamper_liberty: bool = False,
    lock_extra: str = "",
):
    owner = tmp_path / "ip/consumer"
    compile_script = _write(owner / "implementation/compile.tcl", "exit\n")
    link_script = _write(owner / "implementation/link.tcl", "exit\n")
    variant = _write(
        owner / "configs/variants/no_recovery.toml",
        '''schema = 1
contract_kind = "ip-operating-variant"
path_scope = "variant"
owner = "consumer"
[filesets.synthesis]
top_module = "consumer_top"
[filesets.synthesis.dependency_roles]
macro-provider = ["raw_macro_liberty_or_db"]
[integration]
variant = "no-recovery"
''',
    )
    rtl = (_write(owner / "rtl/top.sv", "module consumer_top; endmodule\n"),)
    release = (
        tmp_path
        / "artifacts/exports/macro-provider/package/development-0123456789ab"
    )
    liberty = _write(
        release / "exports/native/synthesis/native.lib",
        "library(native_macro) {}\n",
    )
    interface = _write(
        release / "exports/native/interface.toml",
        '''schema = 1
contract_kind = "ip-interface"
path_scope = "owner"
owner = "macro-provider"

[physical]
library = "native_macro"
cell = "NATIVE_TOP"
port_count = 1
canonical_port_contract = "ip/macro_provider/configs/ports.toml"

[behavior]
result = "native response"

[supplies]
domains = []
''',
    )
    ports = _write(
        release / "exports/native/ports.toml",
        '''[ports]
order = ["A"]

[ports.directions]
A = "input"
''',
    )
    circuit = _write(
        release / "exports/native/circuit.scs",
        "subckt NATIVE_TOP A\nends NATIVE_TOP\n",
    )
    views = [
        {
            "export": "macro-top",
            "role": "interface_contract",
            "path": "exports/native/interface.toml",
            "source": "ip/macro_provider/configs/interface.toml",
            "format": "toml",
            "size": interface.stat().st_size,
            "sha256": hashlib.sha256(interface.read_bytes()).hexdigest(),
        },
        {
            "export": "macro-top",
            "role": "oa_port_contract",
            "path": "exports/native/ports.toml",
            "source": "ip/macro_provider/configs/ports.toml",
            "format": "toml",
            "size": ports.stat().st_size,
            "sha256": hashlib.sha256(ports.read_bytes()).hexdigest(),
        },
        {
            "export": "macro-top",
            "role": "circuit_netlist",
            "path": "exports/native/circuit.scs",
            "format": "spectre-source",
            "composition": "reachable-spectre-hierarchy",
            "subcircuits": ["NATIVE_TOP"],
            "primitive_masters": [],
            "size": circuit.stat().st_size,
            "sha256": hashlib.sha256(circuit.read_bytes()).hexdigest(),
        },
        {
            "export": "macro-top",
            "role": "raw_macro_liberty_or_db",
            "path": "exports/native/synthesis/native.lib",
            "format": "liberty",
            "size": liberty.stat().st_size,
            "sha256": hashlib.sha256(liberty.read_bytes()).hexdigest(),
        },
    ]
    manifest = release / "manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "schema": 2,
                "contract_kind": "ip-release-manifest",
                "release_kind": "source-package",
                "release_id": "development-0123456789ab",
                "source_commit": "0" * 40,
                "ip_name": "macro-provider",
                "owner": "macro-provider",
                "exports": [
                    {
                        "name": "macro-top",
                        "oa": {
                            "library": "native_macro",
                            "cell": "NATIVE_TOP",
                            "schematic_view": "schematic",
                            "layout_view": "layout",
                        },
                        "interface": {
                            "kind": "oa-native",
                            "contract": "ip/macro_provider/configs/interface.toml",
                        },
                        "maturity": {"required_roles": [
                            "interface_contract",
                            "oa_port_contract",
                            "circuit_netlist",
                            "raw_macro_liberty_or_db",
                        ]},
                        "availability": {"synthesis": True},
                    }
                ],
                "maturity": {"level": "development"},
                "views": views,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    object_id = f"sha256-{digest}"
    published = tmp_path / "artifacts/release-store/fixture/objects" / object_id
    published.parent.mkdir(parents=True)
    release.rename(published)
    release = published
    manifest = release / "manifest.json"
    liberty = release / "exports/native/synthesis/native.lib"
    lock = _write(
        owner / "configs/dependency.lock.toml",
        f'''schema = 3
contract_kind = "ip-dependency-lock"
path_scope = "owner"
owner = "consumer"
[[dependency]]
name = "macro-provider"
release_id = "development-0123456789ab"
store = "fixture"
source_commit = "{'0' * 40}"
manifest_sha256 = "{digest}"
maturity = "development"
{lock_extra}
''',
    )
    if tamper_liberty:
        payload = bytearray(liberty.read_bytes())
        payload[0] ^= 1
        liberty.write_bytes(payload)
    plan = plan_structural_link(
        owner="consumer",
        dependency="macro-provider",
        dependency_lock_path=lock,
        variant_path=variant,
        variant="no-recovery",
        rtl_sources=rtl,
        compile_script=compile_script,
        link_script=link_script,
        library_name="native_macro",
        macro_cell="NATIVE_TOP",
        parameter_overrides={"ROWS": 32},
        expected_macro_instances=1,
        expected_unresolved_references=0,
        library_compiler_version=_COMPAT_LC_VERSION,
        release_export="macro-top",
        liberty_role="raw_macro_liberty_or_db",
        resources=Resources(
            directories={
                "release-store.fixture": str(
                    tmp_path / "artifacts/release-store"
                )
            }
        ),
    )
    root = tmp_path / "run"
    artifacts = ExecutionWorkspace(
        run_id="managed-run",
        root=root,
        input_root=root / "work/link/inputs",
        work_root=root / "tool",
        output_root=root / "outputs/link/structural-link",
        log_root=root / "work/link/logs",
        source={},
    )
    return plan, artifacts


def test_structural_link_rejects_unknown_dependency_lock_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="fields must be exactly"):
        _fixture(tmp_path, monkeypatch, lock_extra="unexpected = true")


def test_structural_link_rejects_same_size_release_tampering(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match="content drifted"):
        _fixture(tmp_path, monkeypatch, tamper_liberty=True)


def test_structural_link_captures_tool_outputs_before_releasing_work_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan, artifacts = _fixture(tmp_path, monkeypatch)
    library_compiler = _write(
        tmp_path / "tools/lc_shell",
        '''#!/bin/sh
set -eu
printf 'Version U-2022.12-SP6-T-20250827 for linux64\n'
printf 'compiled-db\n' > "$SIGILICON_STRUCTURAL_DB"
printf 'SIGILICON_STRUCTURAL_DB_PASS library=%s\n' "$SIGILICON_STRUCTURAL_LIBRARY"
''',
        executable=True,
    )
    design_compiler = _write(
        tmp_path / "tools/dc_shell",
        '''#!/bin/sh
set -eu
cat > "$SIGILICON_STRUCTURAL_REPORT" <<EOF
scope=structural-link-only
timing_characterized=false
power_characterized=false
area_characterized=false
top=$SIGILICON_STRUCTURAL_TOP
parameter_overrides=$SIGILICON_STRUCTURAL_PARAMETERS
macro_cell=$SIGILICON_STRUCTURAL_MACRO_CELL
macro_instances=1
unresolved_references=0
EOF
printf 'checkpoint\n' > "$SIGILICON_STRUCTURAL_CHECKPOINT"
printf 'SIGILICON_STRUCTURAL_LINK_PASS top=%s macro_instances=1 unresolved=0\n' "$SIGILICON_STRUCTURAL_TOP"
''',
        executable=True,
    )
    original_owned_directory = structural_link_workflow.owned_directory

    @contextmanager
    def replace_report_after_guard(path: Path, *, create_missing: bool = False):
        with original_owned_directory(
            path,
            create_missing=create_missing,
        ) as owned:
            yield owned
        if Path(path) == artifacts.work_root:
            report = Path(path) / "structural-link.rpt"
            report.write_text(
                report.read_text(encoding="utf-8").replace(
                    "macro_instances=1",
                    "macro_instances=99",
                ),
                encoding="utf-8",
            )

    monkeypatch.setattr(
        structural_link_workflow,
        "owned_directory",
        replace_report_after_guard,
    )

    result = execute_structural_link(
        plan,
        artifacts=artifacts,
        resources=_tool_resources(library_compiler, design_compiler),
        library_compiler="fixture.library-compiler",
        design_compiler="fixture.design-compiler",
        environment={"PATH": "/bin"},
        timeout=10,
    )

    assert result.passed
    assert result.status == "linked"
    evidence = json.loads(
        (artifacts.output_root / "structural-link-evidence.json").read_text()
    )
    assert evidence["release_id"] == "development-0123456789ab"
    assert evidence["library_compiler_version"] == _COMPAT_LC_VERSION
    assert evidence["macro_instance_count"] == 1
    assert evidence["product_qualification_conclusion"] is False
    assert (artifacts.output_root / "consumer_top.ddc").is_file()


def test_structural_link_missing_completion_marker_is_failed_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan, artifacts = _fixture(tmp_path, monkeypatch)
    incomplete = _write(
        tmp_path / "tools/lc_shell",
        '#!/bin/sh\n: > "$SIGILICON_STRUCTURAL_DB"\n',
        executable=True,
    )

    result = execute_structural_link(
        plan,
        artifacts=artifacts,
        resources=_tool_resources(incomplete, incomplete),
        library_compiler="fixture.library-compiler",
        design_compiler="fixture.design-compiler",
        environment={"PATH": "/bin"},
        timeout=10,
    )

    assert not result.passed
    assert result.facts["macro_instance_count"] is None
    assert result.facts["unresolved_reference_count"] is None
    evidence = json.loads(
        (artifacts.output_root / "structural-link-evidence.json").read_text()
    )
    assert evidence["status"] == "execution_failed"


def test_structural_link_preserves_tool_mode_symlink_names(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan, artifacts = _fixture(tmp_path, monkeypatch)
    dispatcher = _write(
        tmp_path / "tools/snps_shell",
        '''#!/bin/sh
set -eu
case "${0##*/}" in
  lc_shell)
    printf 'Version U-2022.12-SP6-T-20250827 for linux64\n'
    printf 'compiled-db\n' > "$SIGILICON_STRUCTURAL_DB"
    printf 'SIGILICON_STRUCTURAL_DB_PASS library=%s\n' "$SIGILICON_STRUCTURAL_LIBRARY"
    ;;
  dc_shell)
    cat > "$SIGILICON_STRUCTURAL_REPORT" <<EOF
scope=structural-link-only
timing_characterized=false
power_characterized=false
area_characterized=false
top=$SIGILICON_STRUCTURAL_TOP
parameter_overrides=$SIGILICON_STRUCTURAL_PARAMETERS
macro_cell=$SIGILICON_STRUCTURAL_MACRO_CELL
macro_instances=1
unresolved_references=0
EOF
    printf 'checkpoint\n' > "$SIGILICON_STRUCTURAL_CHECKPOINT"
    printf 'SIGILICON_STRUCTURAL_LINK_PASS top=%s macro_instances=1 unresolved=0\n' "$SIGILICON_STRUCTURAL_TOP"
    ;;
  *) exit 2 ;;
esac
''',
        executable=True,
    )
    library_compiler = dispatcher.with_name("lc_shell")
    design_compiler = dispatcher.with_name("dc_shell")
    library_compiler.symlink_to(dispatcher.name)
    design_compiler.symlink_to(dispatcher.name)

    result = execute_structural_link(
        plan,
        artifacts=artifacts,
        resources=_tool_resources(library_compiler, design_compiler),
        library_compiler="fixture.library-compiler",
        design_compiler="fixture.design-compiler",
        environment={"PATH": "/bin"},
        timeout=10,
    )

    assert result.passed


def test_structural_link_rejects_a_different_library_compiler_release(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan, artifacts = _fixture(tmp_path, monkeypatch)
    dc_invoked = tmp_path / "dc-invoked"
    library_compiler = _write(
        tmp_path / "tools/lc_shell",
        '''#!/bin/sh
printf 'Version X-2025.06 for linux64\n'
printf 'compiled-db\n' > "$SIGILICON_STRUCTURAL_DB"
printf 'SIGILICON_STRUCTURAL_DB_PASS library=%s\n' "$SIGILICON_STRUCTURAL_LIBRARY"
''',
        executable=True,
    )
    design_compiler = _write(
        tmp_path / "tools/dc_shell",
        f"#!/bin/sh\nprintf invoked > {dc_invoked}\n",
        executable=True,
    )

    result = execute_structural_link(
        plan,
        artifacts=artifacts,
        resources=_tool_resources(library_compiler, design_compiler),
        library_compiler="fixture.library-compiler",
        design_compiler="fixture.design-compiler",
        environment={"PATH": "/bin"},
        timeout=10,
    )

    assert not result.passed
    assert result.facts["library_compiler_version"] == "X-2025.06"
    assert not dc_invoked.exists()


def test_structural_link_can_hold_a_binary_tool_mode_symlink(tmp_path: Path) -> None:
    dispatcher = tmp_path / "tools/snps_shell"
    dispatcher.parent.mkdir(parents=True)
    shutil.copyfile("/bin/true", dispatcher)
    dispatcher.chmod(0o755)
    launcher = dispatcher.with_name("dc_shell")
    launcher.symlink_to(dispatcher.name)

    with owned_executable(launcher) as held:
        completed = managed_process.run(ProcessRequest(
            argv=held.command,
            cwd=tmp_path,
            environment={"PATH": "/bin"},
            timeout_seconds=10,
            before_spawn=held.require_visible,
        ))

    assert completed.returncode == 0


def test_structural_link_rejects_release_drift_after_planning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan, artifacts = _fixture(tmp_path, monkeypatch)
    tool = _write(tmp_path / "tools/tool", "#!/bin/sh\nexit 0\n", executable=True)
    plan.release_liberty.write_text("library(drifted) {}\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="changed between planning"):
        execute_structural_link(
            plan,
            artifacts=artifacts,
            resources=_tool_resources(tool, tool),
            library_compiler="fixture.library-compiler",
            design_compiler="fixture.design-compiler",
            environment={"PATH": "/bin"},
            timeout=10,
        )


def test_structural_link_rejects_marker_without_report_facts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan, artifacts = _fixture(tmp_path, monkeypatch)
    library_compiler = _write(
        tmp_path / "tools/lc_shell",
        '''#!/bin/sh
printf 'Version U-2022.12-SP6-T-20250827 for linux64\n'
printf 'compiled-db\n' > "$SIGILICON_STRUCTURAL_DB"
printf 'SIGILICON_STRUCTURAL_DB_PASS library=%s\n' "$SIGILICON_STRUCTURAL_LIBRARY"
''',
        executable=True,
    )
    design_compiler = _write(
        tmp_path / "tools/dc_shell",
        '''#!/bin/sh
printf 'not structural evidence\n' > "$SIGILICON_STRUCTURAL_REPORT"
printf 'checkpoint\n' > "$SIGILICON_STRUCTURAL_CHECKPOINT"
printf 'SIGILICON_STRUCTURAL_LINK_PASS top=%s macro_instances=1 unresolved=0\n' "$SIGILICON_STRUCTURAL_TOP"
''',
        executable=True,
    )

    result = execute_structural_link(
        plan,
        artifacts=artifacts,
        resources=_tool_resources(library_compiler, design_compiler),
        library_compiler="fixture.library-compiler",
        design_compiler="fixture.design-compiler",
        environment={"PATH": "/bin"},
        timeout=10,
    )

    assert not result.passed
    assert result.facts["macro_instance_count"] is None
