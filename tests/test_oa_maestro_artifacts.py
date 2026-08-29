from __future__ import annotations

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace

import pytest

from sigilicon.artifacts import load_manifest
from sigilicon.paths import ProjectContext
from sigilicon.workflows import oa_simulation


def _write_project_context(root: Path) -> Path:
    artifact_root = root / "configured-artifacts"
    (root / "sigilicon.toml").write_text(
        '''schema = 1
contract_kind = "sigilicon-project"
path_scope = "repository"
owner = "fixture"

[paths]
project_root = "."
workspace_root = "virtuoso"
artifact_root = "configured-artifacts"
''',
        encoding="utf-8",
    )
    (root / "virtuoso").mkdir()
    return artifact_root


def _oa_plan(root: Path) -> tuple[SimpleNamespace, SimpleNamespace]:
    canonical_source = root / "testbench.scs"
    canonical_source.write_text("simulator lang=spectre\n", encoding="utf-8")
    native_setup = SimpleNamespace(rdb_contract=object())
    simulation = SimpleNamespace(
        native_setup=native_setup,
        dut="fixture_dut",
    )
    step = SimpleNamespace(
        cell="tb_fixture",
        canonical_source=canonical_source,
        simulation=simulation,
    )
    source = SimpleNamespace(
        project=ProjectContext.from_project_root(root),
        project_root=root,
        cells=(SimpleNamespace(cell=step.cell, owner="fixture-owner"),),
    )
    plan = SimpleNamespace(
        source=source,
        library="fixture_lib",
        testbenches=(step,),
    )
    return plan, step


def test_oa_maestro_writes_directly_to_configured_artifact_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    artifact_root = _write_project_context(tmp_path)
    plan, step = _oa_plan(tmp_path)
    monkeypatch.setattr(
        tempfile,
        "mkdtemp",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("OA Maestro must not allocate temporary work")
        ),
    )

    def run_impl(_plan, _step, _client, *, timeout, record):
        assert timeout == 17
        assert record.paths.root.is_relative_to(artifact_root)
        result_export = record.write_text(
            "outputs", ("maestro-rdb.tsv",), "fixture\n"
        )
        normalized = record.write_json(
            "outputs", ("maestro-rdb.json",), {"passed": True}
        )
        summary = record.write_json(
            "outputs", ("run-summary.json",), {"simulation_completed": True}
        )
        elaborated_netlist = record.write_text(
            "outputs", ("elaborated-netlist.vams",), "module fixture; endmodule\n"
        )
        return oa_simulation.OAMaestroRunResult(
            run_id=record.paths.identity,
            run_dir=record.paths.root,
            manifest_path=record.paths.manifest,
            library="fixture_lib",
            testbench="tb_fixture",
            history="Interactive.1",
            elaborated_netlist=elaborated_netlist,
            result_database_export=result_export,
            normalized_result_database=normalized,
            run_summary=summary,
            scalar_output_count=1,
        )

    monkeypatch.setattr(
        oa_simulation,
        "_run_native_oa_maestro_testbench_impl",
        run_impl,
    )

    result = oa_simulation.run_oa_maestro_testbench(
        plan,
        step,
        object(),
        timeout=17,
    )

    assert result.run_dir.is_relative_to(artifact_root)
    assert result.run_dir.parent == (
        artifact_root
        / "runs"
        / "fixture-owner"
        / "tb_fixture"
        / "oa-maestro"
        / "native-rdb"
    )
    manifest = load_manifest(result.manifest_path)
    assert manifest["status"] == "succeeded"
    assert manifest["artifact_kind"] == "oa_maestro_simulation"
    assert manifest["run_id"] == result.run_id
    assert manifest["completion_evidence"] == [
        "outputs/run-summary.json",
        "outputs/maestro-rdb.json",
    ]


def test_oa_maestro_records_failure_in_configured_artifact_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    artifact_root = _write_project_context(tmp_path)
    plan, step = _oa_plan(tmp_path)
    monkeypatch.setattr(
        oa_simulation,
        "_run_native_oa_maestro_testbench_impl",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("fixture failure")),
    )

    with pytest.raises(RuntimeError, match="fixture failure"):
        oa_simulation.run_oa_maestro_testbench(plan, step, object())

    manifests = tuple(artifact_root.rglob("manifest.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["details"]["error"] == "fixture failure"
