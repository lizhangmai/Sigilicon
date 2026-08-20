from __future__ import annotations

from types import SimpleNamespace

from sigilicon.virtuoso.oa import (
    _instance_parameter_value_matches,
    _owned_db_open_cellview_skill,
    own_synchronous_cellview_delta_skill,
    set_cell_port_directions,
    validate_cell_fingerprint,
    validate_cell_port_directions,
    validate_instance_parameters,
)
from sigilicon.virtuoso.importer import check_and_save_schematic


class RecordingClient:
    def __init__(self) -> None:
        self.sources: list[str] = []
        self.calls: list[tuple[str, dict[str, object]]] = []

    def execute_skill(self, source: str, **_kwargs):
        self.sources.append(source)
        self.calls.append((source, dict(_kwargs)))
        if 'sprintf(nil "F|%s|%L' in source:
            rows = []
            for view in ("schematic", "symbol"):
                rows.extend(
                    (
                        f'F|{view}|{"a" * 64}',
                        f'T|{view}|IN|input',
                        f'T|{view}|OUT|output',
                    )
                )
            return SimpleNamespace(output="\n".join(rows), errors=[])
        if "actualFingerprint = cv~>flowDesignFingerprint" in source:
            return SimpleNamespace(output="a" * 64, errors=[])
        if 'sprintf(nil "P|%s|%s|%s' in source:
            return SimpleNamespace(
                output='I|X0|CHILD\nP|X0|lch|pPar("parent_l")\nP|X0|w|1e-07',
                errors=[],
            )
        return SimpleNamespace(output="t", errors=[])


def test_imported_schematic_is_checked_and_saved_under_mutation_lease(
    workspace_factory,
) -> None:
    client = RecordingClient()

    with workspace_factory(client, library="lib") as operation:
        with operation.mutation_scope(
            "lib", cells=("cell",), phase="test schematic check and save"
        ):
            check_and_save_schematic(
                client,
                "lib",
                "cell",
                timeout=30,
                operation=operation,
            )

    assert len(client.sources) == 1
    source = client.sources[0]
    assert 'dbOpenCellViewByType("lib" "cell" "schematic" "schematic" "a")' in source
    assert "schCheck(cv)" in source
    assert "dbSave(cv)" in source
    assert "status = schExtractStatus(cv)" in source
    assert 'unless(member(status list("clean" "dirty"))' in source
    assert "dbClose(cv)" in source
    assert "target became busy before atomic SKILL dispatch" in source


def test_exact_open_helper_closes_its_own_let_scope() -> None:
    source = _owned_db_open_cellview_skill(
        library="lib",
        cell="cell",
        view_expression='"schematic"',
        view_type="",
        mode="r",
        result_variable="cv",
        label="test open",
    )

    assert source.rstrip().endswith("\n))")


def test_synchronous_scope_closes_only_hidden_exact_delta_handles() -> None:
    source = own_synchronous_cellview_delta_skill("list(\"done\")", label="test")

    assert "flowSyncBefore = dbGetOpenCellViews()" in source
    assert "member(flowSyncCv flowSyncBefore)" in source
    assert "geGetWindowCellView(flowSyncWindow)" in source
    assert "if(flowSyncVisible" in source
    assert "flowSyncCloseAttempt = errset(dbClose(flowSyncCv) t)" in source
    assert 'equal(flowSyncCv~>mode "r")' in source
    assert "flowSyncPurgeAttempt = errset(dbPurge(flowSyncCv) t)" in source
    assert "member(flowSyncCv dbGetOpenCellViews())" in source
    assert "exact synchronous handle cleanup failed" in source
    assert "unwindProtect(" in source



def test_every_open_cellview_is_protected_by_unwind_cleanup(workspace_factory) -> None:
    client = RecordingClient()
    directions = {"IN": "input", "OUT": "output"}
    with workspace_factory(client, library="lib") as operation:
        with operation.mutation_scope(
            "lib", cells=("cell",), phase="test port update"
        ):
            set_cell_port_directions(
                client,
                "lib",
                "cell",
                directions,
                fingerprint="a" * 64,
                operation=operation,
            )
        validate_cell_port_directions(
            client,
            "lib",
            "cell",
            directions,
            fingerprint="a" * 64,
            operation=operation,
        )
        validate_cell_fingerprint(
            client,
            "lib",
            "cell",
            "a" * 64,
            operation=operation,
        )

    assert len(client.sources) == 3
    for source in client.sources:
        assert "unwindProtect" in source
        assert "dbClose" in source
    assert "schCheck(cv)" in client.sources[0]
    assert "flowBeforeViews = dbGetOpenCellViews()" in client.sources[0]
    assert "member(flowCv flowBeforeViews)" in client.sources[0]
    assert "target became busy before atomic SKILL dispatch" in client.sources[0]
    for source in client.sources[1:]:
        assert "target became busy before exact open" in source
        assert "flowOpenBefore = dbGetOpenCellViews()" in source
        assert "member(flowOpenCv flowOpenBefore)" in source
        assert "equal(flowOpenCv cv)" in source
        assert "flowCloseAttempt = errset(dbClose(flowOpenCv) t)" in source
        assert 'equal(flowOpenCv~>mode "r")' in source
        assert "flowPurgeAttempt = errset(dbPurge(flowOpenCv) t)" in source
        assert "implicit handle close failed" in source
    assert 'sprintf(nil "F|%s|%L' in client.sources[1]
    assert 'sprintf(nil "T|%s|%s|%s' in client.sources[1]
    assert "actualFingerprint = cv~>flowDesignFingerprint" in client.sources[2]


def test_generated_instance_parameter_values_compare_semantically() -> None:
    assert _instance_parameter_value_matches("100n", "1e-07") is True
    assert _instance_parameter_value_matches("parent_l", 'pPar("parent_l")') is True
    assert _instance_parameter_value_matches("240n", "1e-07") is False


def test_instance_parameter_validation_reads_parent_oa_properties(
    workspace_factory,
) -> None:
    client = RecordingClient()
    with workspace_factory(client, library="lib") as operation:
        report = validate_instance_parameters(
            client,
            "lib",
            "PARENT",
            {"X0": ("CHILD", {"lch": "parent_l", "w": "100n"})},
            operation=operation,
        )

    assert report == {"passed": True, "instances": 1, "parameters": 2}
    source = client.sources[0]
    assert 'dbFindAnyInstByName(cv "X0")' in source
    assert 'dbFindProp(inst "lch")' in source
    assert 'dbFindProp(inst "w")' in source
    assert "unwindProtect" in source
    assert "dbClose" in source
