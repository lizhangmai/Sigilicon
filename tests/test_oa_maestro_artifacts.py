from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from sigilicon.domain.netlist import NetlistSnapshot
from sigilicon.domain.source import load_text_source_snapshot
from sigilicon.execution.model import Resources
from sigilicon.workflows import oa_simulation


def test_flow_action_remains_the_only_workspace_operation_record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    registered: list[object] = []
    bound: list[object] = []
    operation = SimpleNamespace(
        operation_id="a" * 32,
        register_artifact=registered.append,
    )

    @contextmanager
    def workspace_operation(*_args, **kwargs):
        assert kwargs["operation_id"] == "a" * 32
        yield operation

    monkeypatch.setattr(oa_simulation, "workspace_operation", workspace_operation)

    with oa_simulation._registered_oa_maestro_operation(
        object(),
        tmp_path,
        operation_id="a" * 32,
        bind_operation=bound.append,
    ) as selected:
        assert selected is operation

    assert bound == [operation]
    assert registered == []


def test_registered_oa_operation_reports_workspace_uncertainty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reasons: list[str] = []
    operation = SimpleNamespace(
        operation_id="a" * 32,
        uncertain_reason=None,
    )

    @contextmanager
    def workspace_operation(*_args, **_kwargs):
        try:
            yield operation
        except BaseException:
            operation.uncertain_reason = "workspace identity changed"
            raise

    monkeypatch.setattr(oa_simulation, "workspace_operation", workspace_operation)

    with pytest.raises(RuntimeError, match="fixture failure"):
        with oa_simulation._registered_oa_maestro_operation(
            object(),
            tmp_path,
            operation_id="a" * 32,
            bind_operation=lambda _operation: None,
            record_uncertainty=reasons.append,
        ):
            raise RuntimeError("fixture failure")

    assert reasons == ["workspace identity changed"]


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
        oa_simulation.execute_oa_maestro_testbench(
            plan,
            step,
            object(),
            artifacts=object(),
            operation_id="a" * 32,
            bind_operation=lambda _operation: None,
            resources=Resources(),
        )
