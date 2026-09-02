from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import sigilicon.workflows.hierarchy_import as hierarchy
from sigilicon.artifacts import RunRecord
from sigilicon.execution.model import Resources
from sigilicon.paths import ArtifactLayout
from sigilicon.virtuoso.workspace import OperationPolicy


SPICEIN_RESOURCES = Resources(
    tools={"cadence.spice-in": "/bin/true"}
)


def _artifact(tmp_path: Path, identity: str = "1" * 32) -> RunRecord:
    return RunRecord.begin(
        ArtifactLayout(tmp_path / "artifacts").operation_run(
            owner="lib",
            operation="netlist-import",
            variant="hierarchy",
            run_id=identity,
        ),
        adapter="offline",
    )


def test_netlist_is_parsed_once_and_preplanned_order_drives_import(
    monkeypatch,
    tmp_path,
    workspace_factory,
) -> None:
    netlist = tmp_path / "hierarchy.scs"
    netlist.write_text(
        """subckt top A Y
X0 (A Y) leaf
ends top
subckt leaf A Y
R0 (A Y) resistor r=1k
ends leaf
""",
        encoding="utf-8",
    )
    original_load = hierarchy.load_netlist_snapshot
    parse_calls = 0

    def load_once(path):
        nonlocal parse_calls
        parse_calls += 1
        return original_load(path)

    events: list[tuple[str, str, object]] = []
    monkeypatch.setattr(hierarchy, "load_netlist_snapshot", load_once)
    monkeypatch.setattr(
        "sigilicon.virtuoso.workspace.require_quiescent_project_cell",
        lambda _operation, _library, cell, **_kwargs: (
            events.append(("quiescent", cell, None)) or (tmp_path / cell)
        ),
    )

    def import_cell(_client, _library, cell, _netlist, **kwargs):
        kwargs["operation"].require_active_mutation(
            _client, _library, cell, phase="test schematic adapter"
        )
        events.append(("schematic", cell, kwargs["reference_libraries"]))

    monkeypatch.setattr(hierarchy, "import_schematic", import_cell)
    monkeypatch.setattr(
        hierarchy,
        "generate_symbol",
        lambda _client, _library, cell, **kwargs: (
            kwargs["operation"].require_active_mutation(
                _client, _library, cell, phase="test symbol adapter"
            ),
            events.append(("symbol", cell, None)),
        )[-1],
    )

    plan = hierarchy.plan_hierarchy(netlist, top="top")
    client = object()
    artifact = _artifact(tmp_path)
    with workspace_factory(
        client,
        library="designLib",
        policy=OperationPolicy.RECURSIVE_OA,
    ) as operation:
        completed = hierarchy.import_hierarchy(
            client,
            plan=plan,
            library="designLib",
            reference_libraries=("deviceLib", "deviceLib"),
            overwrite=True,
            artifact=artifact,
            source_role="inputs",
            work_role="work",
            cell_evidence_role="outputs",
            timeout=30,
            operation=operation,
            resources=SPICEIN_RESOURCES,
        )

    assert parse_calls == 1
    assert plan.ordered_cells == ("leaf", "top")
    assert completed == plan.ordered_cells
    assert (artifact.paths.root / "work/leaf").is_dir()
    assert (artifact.paths.root / "work/top").is_dir()
    assert (artifact.paths.root / "outputs/leaf.json").is_file()
    assert (artifact.paths.root / "outputs/top.json").is_file()
    writes = [event for event in events if event[0] != "quiescent"]
    assert writes == [
        (
            "schematic",
            "leaf",
            ("designLib", "deviceLib", "analogLib", "basic"),
        ),
        ("symbol", "leaf", None),
        (
            "schematic",
            "top",
            ("designLib", "deviceLib", "analogLib", "basic"),
        ),
        ("symbol", "top", None),
    ]


@pytest.mark.parametrize("failed_stage", ["schematic", "symbol"])
def test_partial_failure_reports_exact_completed_cells_and_stage(
    monkeypatch,
    tmp_path,
    workspace_factory,
    failed_stage: str,
) -> None:
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "sigilicon.virtuoso.workspace.require_quiescent_project_cell",
        lambda _operation, _library, cell, **_kwargs: (
            events.append(("quiescent", cell)) or (tmp_path / cell)
        ),
    )

    def import_cell(_client, _library, cell, _netlist, **_kwargs):
        events.append(("schematic", cell))
        if failed_stage == "schematic" and cell == "mid":
            raise RuntimeError("schematic boom")

    def symbol_cell(_client, _library, cell, **_kwargs):
        events.append(("symbol", cell))
        if failed_stage == "symbol" and cell == "mid":
            raise RuntimeError("symbol boom")

    monkeypatch.setattr(hierarchy, "import_schematic", import_cell)
    monkeypatch.setattr(hierarchy, "generate_symbol", symbol_cell)

    netlist = tmp_path / "partial.scs"
    netlist.write_text(
        "subckt leaf A Y\nends leaf\n"
        "subckt mid A Y\nends mid\n"
        "subckt top A Y\nends top\n",
        encoding="utf-8",
    )
    plan = hierarchy.plan_hierarchy(netlist, top="top")

    client = object()
    with pytest.raises(hierarchy.HierarchyImportError) as raised:
        with workspace_factory(
            client,
            library="lib",
            policy=OperationPolicy.RECURSIVE_OA,
        ) as operation:
            hierarchy.import_hierarchy(
                client,
                plan=plan,
                library="lib",
                overwrite=True,
                artifact=_artifact(tmp_path),
                source_role="inputs",
                work_role="work",
                timeout=30,
                operation=operation,
                resources=SPICEIN_RESOURCES,
            )

    error = raised.value
    assert error.cell == "mid"
    assert error.stage == failed_stage
    assert error.completed == ("leaf",)
    assert error.schematic_completed == (
        ("leaf", "mid") if failed_stage == "symbol" else ("leaf",)
    )
    assert all(event[1] != "top" for event in events)


def test_hierarchy_write_adapters_reject_forged_workspace_operation(
    monkeypatch,
    tmp_path,
) -> None:
    writes: list[str] = []
    monkeypatch.setattr(
        hierarchy,
        "import_schematic",
        lambda *_args, **_kwargs: writes.append("schematic"),
    )
    monkeypatch.setattr(
        hierarchy,
        "generate_symbol",
        lambda *_args, **_kwargs: writes.append("symbol"),
    )

    netlist = tmp_path / "cell.scs"
    netlist.write_text("subckt cell A Y\nends cell\n", encoding="utf-8")
    plan = hierarchy.plan_hierarchy(netlist, top="cell")

    with pytest.raises(RuntimeError, match="workspace safety boundary"):
        hierarchy.import_hierarchy(
            object(),
            plan=plan,
            library="lib",
            overwrite=True,
            artifact=_artifact(tmp_path),
            source_role="inputs",
            work_role="work",
            timeout=30,
            operation=SimpleNamespace(),
            resources=SPICEIN_RESOURCES,
        )

    assert writes == []


def test_hierarchy_planning_rejects_path_like_subckt_name(tmp_path) -> None:
    netlist = tmp_path / "unsafe.scs"
    netlist.write_text(
        "subckt ../../outside A Y\nR0 (A Y) resistor r=1k\nends ../../outside\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid hierarchy cell identifier"):
        hierarchy.plan_hierarchy(netlist, top=None)


def test_hierarchy_plan_cannot_claim_cells_not_in_its_snapshot(tmp_path) -> None:
    netlist = tmp_path / "valid.scs"
    netlist.write_text("subckt valid A Y\nends valid\n", encoding="utf-8")
    valid = hierarchy.plan_hierarchy(netlist, top="valid")

    with pytest.raises(ValueError, match="top subckt not found|do not match"):
        hierarchy.HierarchyPlan(valid.snapshot, ("other",))


def test_interrupt_is_rethrown_with_partial_provenance(
    monkeypatch,
    tmp_path,
    workspace_factory,
) -> None:
    monkeypatch.setattr(
        "sigilicon.virtuoso.workspace.require_quiescent_project_cell",
        lambda *_args, **_kwargs: tmp_path / "cell",
    )
    monkeypatch.setattr(
        hierarchy,
        "import_schematic",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    netlist = tmp_path / "interrupt.scs"
    netlist.write_text("subckt cell A Y\nends cell\n", encoding="utf-8")
    plan = hierarchy.plan_hierarchy(netlist, top="cell")
    client = object()

    with pytest.raises(KeyboardInterrupt) as raised:
        with workspace_factory(
            client,
            library="lib",
            policy=OperationPolicy.RECURSIVE_OA,
        ) as operation:
            hierarchy.import_hierarchy(
                client,
                plan=plan,
                library="lib",
                overwrite=True,
                artifact=_artifact(tmp_path),
                source_role="inputs",
                work_role="work",
                timeout=30,
                operation=operation,
                resources=SPICEIN_RESOURCES,
            )

    assert any("completed cells: ()" in note for note in raised.value.__notes__)
