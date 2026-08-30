from __future__ import annotations

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace

import pytest

from sigilicon.artifacts import load_manifest
from sigilicon.domain.netlist import NetlistSnapshot
from sigilicon.domain.source import load_text_source_snapshot
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
    simulation_source = root / "simulation.toml"
    simulation_source.write_text("schema = 3\n", encoding="utf-8")
    setup_source = root / "setup.il"
    setup_source.write_text("; setup\n", encoding="utf-8")
    rdb_source = root / "native_rdb.toml"
    rdb_source.write_text("schema = 2\n", encoding="utf-8")
    rdb_contract = SimpleNamespace(
        path=rdb_source.resolve(),
        source_snapshot=load_text_source_snapshot(rdb_source),
        support_sources=(),
        support_source_snapshots=(),
    )
    native_setup = SimpleNamespace(
        source=setup_source.resolve(),
        source_snapshot=load_text_source_snapshot(setup_source),
        rdb_contract=rdb_contract,
    )
    simulation = SimpleNamespace(
        path=simulation_source.resolve(),
        source_snapshot=load_text_source_snapshot(simulation_source),
        native_setup=native_setup,
        dut="fixture_dut",
    )
    step = SimpleNamespace(
        cell="tb_fixture",
        canonical_source=canonical_source.resolve(),
        source_snapshot=NetlistSnapshot(
            source_path=canonical_source.resolve(),
            text=canonical_source.read_text(encoding="utf-8"),
            interfaces={},
        ),
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
            evidence=oa_simulation.evaluate_oa_maestro_evidence(
                overall_spec_status="pass",
                per_output_spec_status=("pass",),
                diagnostic_equivalence=None,
            ),
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
    assert manifest["details"]["evidence_status"] == "pass"


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


def test_oa_maestro_records_only_plan_owned_source_bytes(tmp_path: Path) -> None:
    paths = {
        name: tmp_path / name
        for name in (
            "simulation.toml",
            "setup.il",
            "native_rdb.toml",
            "testbench.scs",
            "diagnostic.py",
        )
    }
    for name, path in paths.items():
        path.write_text(f"planned {name}\n", encoding="utf-8")
    snapshots = {
        name: load_text_source_snapshot(path)
        for name, path in paths.items()
        if name != "testbench.scs"
    }
    netlist = NetlistSnapshot(
        source_path=paths["testbench.scs"].resolve(),
        text=paths["testbench.scs"].read_text(encoding="utf-8"),
        interfaces={},
    )
    rdb_contract = SimpleNamespace(
        path=paths["native_rdb.toml"].resolve(),
        source_snapshot=snapshots["native_rdb.toml"],
        support_source_snapshots=(snapshots["diagnostic.py"],),
        support_sources=(paths["diagnostic.py"].resolve(),),
    )
    step = SimpleNamespace(
        canonical_source=paths["testbench.scs"].resolve(),
        source_snapshot=netlist,
        simulation=SimpleNamespace(
            path=paths["simulation.toml"].resolve(),
            source_snapshot=snapshots["simulation.toml"],
            native_setup=SimpleNamespace(
                source=paths["setup.il"].resolve(),
                source_snapshot=snapshots["setup.il"],
                rdb_contract=rdb_contract,
            ),
        ),
    )
    for path in paths.values():
        path.write_text("changed after planning\n", encoding="utf-8")

    recorded: dict[str, str] = {}

    class Record:
        def write_text(self, _role, relative, value, **_kwargs):
            recorded["/".join(relative)] = value

    oa_simulation._record_native_oa_maestro_inputs(Record(), step)

    assert recorded == {
        "simulation.toml": "planned simulation.toml\n",
        "setup.il": "planned setup.il\n",
        "native_rdb.toml": "planned native_rdb.toml\n",
        "testbench.scs": "planned testbench.scs\n",
        "support/01-diagnostic.py": "planned diagnostic.py\n",
    }


def test_oa_maestro_rejects_a_source_less_manual_contract() -> None:
    simulation_path = Path("/tmp/source-less-simulation.toml")
    step = SimpleNamespace(
        cell="tb_fixture",
        canonical_source=Path("/tmp/source-less-testbench.scs"),
        source_snapshot=SimpleNamespace(text="testbench\n"),
        simulation=SimpleNamespace(
            path=simulation_path,
            source_snapshot=None,
            native_setup=SimpleNamespace(
                source=Path("/tmp/source-less-setup.il"),
                source_snapshot=SimpleNamespace(text="setup\n"),
                rdb_contract=SimpleNamespace(
                    source_snapshot=None,
                    support_source_snapshots=(),
                    support_sources=(),
                ),
            ),
        ),
    )

    plan = SimpleNamespace(testbenches=(step,))
    with pytest.raises(ValueError, match="no simulation source snapshot"):
        oa_simulation.run_oa_maestro_testbench(plan, step, object())
