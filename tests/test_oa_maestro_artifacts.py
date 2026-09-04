from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from sigilicon.execution._model import Resources
from sigilicon.adapters.cadence import oa_simulation


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
