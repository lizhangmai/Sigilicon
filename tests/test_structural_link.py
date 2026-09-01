from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

import pytest

from sigilicon.workflows.run_artifacts import RunArtifacts
from sigilicon.workflows.structural_link import (
    _owned_executable,
    execute_structural_link,
    plan_structural_link,
)
from sigilicon.external_tools import run_process_group_capture


def _write(path: Path, text: str, *, executable: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if executable:
        path.chmod(0o755)
    return path


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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
cim-compute-v2 = ["raw_macro_liberty_or_db"]
[integration]
variant = "no-recovery"
''',
    )
    rtl = (_write(owner / "rtl/top.sv", "module consumer_top; endmodule\n"),)
    release = (
        tmp_path
        / "artifacts/exports/cim-compute-v2/package/development-0123456789ab"
    )
    liberty = _write(
        release / "exports/native/synthesis/native.lib",
        "library(native_macro) {}\n",
    )
    manifest = release / "manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "release_id": "development-0123456789ab",
                "source_commit": "0" * 40,
                "ip_name": "cim-compute-v2",
                "owner": "cim-compute-v2",
                "maturity": {"level": "development"},
                "provenance": {"working_tree_dirty": False},
                "views": [
                    {
                        "export": "mx-block-v2",
                        "role": "raw_macro_liberty_or_db",
                        "path": "exports/native/synthesis/native.lib",
                        "size": liberty.stat().st_size,
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    monkeypatch.setattr(
        "sigilicon.workflows.structural_link.audit_ip_release_manifest",
        lambda selected: json.loads(selected.read_text(encoding="utf-8")),
    )
    lock = _write(
        owner / "configs/dependency.lock.toml",
        f'''schema = 1
contract_kind = "ip-dependency-lock"
path_scope = "owner"
owner = "consumer"
[[dependency]]
name = "cim-compute-v2"
release_id = "development-0123456789ab"
manifest = "exports/cim-compute-v2/package/development-0123456789ab/manifest.json"
source_commit = "{'0' * 40}"
manifest_sha256 = "{digest}"
maturity = "development"
''',
    )
    plan = plan_structural_link(
        owner="consumer",
        dependency="cim-compute-v2",
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
        release_export="mx-block-v2",
        liberty_role="raw_macro_liberty_or_db",
        release_manifest=manifest,
        release_liberty=liberty,
    )
    root = tmp_path / "run"
    artifacts = RunArtifacts(
        run_id="managed-run",
        root=root,
        input_root=root / "work/link/inputs",
        work_root=root / "tool",
        output_root=root / "outputs/link/structural-link",
        log_root=root / "work/link/logs",
        source={},
    )
    return plan, artifacts


def test_structural_link_executes_both_tools_and_publishes_typed_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan, artifacts = _fixture(tmp_path, monkeypatch)
    library_compiler = _write(
        tmp_path / "tools/lc_shell",
        '''#!/bin/sh
set -eu
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

    result = execute_structural_link(
        plan,
        artifacts=artifacts,
        library_compiler=library_compiler,
        design_compiler=design_compiler,
        environment={"PATH": "/bin"},
        timeout=10,
    )

    assert result.passed
    assert result.status == "linked"
    evidence = json.loads(
        (artifacts.output_root / "structural-link-evidence.json").read_text()
    )
    assert evidence["release_id"] == "development-0123456789ab"
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
        library_compiler=incomplete,
        design_compiler=incomplete,
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
        library_compiler=library_compiler,
        design_compiler=design_compiler,
        environment={"PATH": "/bin"},
        timeout=10,
    )

    assert result.passed


def test_structural_link_can_hold_a_binary_tool_mode_symlink(tmp_path: Path) -> None:
    dispatcher = tmp_path / "tools/snps_shell"
    dispatcher.parent.mkdir(parents=True)
    shutil.copyfile("/bin/true", dispatcher)
    dispatcher.chmod(0o755)
    launcher = dispatcher.with_name("dc_shell")
    launcher.symlink_to(dispatcher.name)

    with _owned_executable(launcher) as held:
        completed = run_process_group_capture(
            held.command,
            cwd=tmp_path,
            env={"PATH": "/bin"},
            timeout=10,
            before_spawn=held.require_visible,
        )

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
            library_compiler=tool,
            design_compiler=tool,
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
        library_compiler=library_compiler,
        design_compiler=design_compiler,
        environment={"PATH": "/bin"},
        timeout=10,
    )

    assert not result.passed
    assert result.facts["macro_instance_count"] is None
