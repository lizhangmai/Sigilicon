from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from sigilicon.virtuoso.ade import (
    build_ie_cards,
    create_config_view,
    create_maestro_view,
    export_oa_maestro_setup,
)
from sigilicon.virtuoso.oa import (
    _instance_parameter_value_matches,
    _owned_db_open_cellview_skill,
    own_synchronous_cellview_delta_skill,
    set_cell_port_directions,
    validate_cell_fingerprint,
    validate_cell_port_directions,
    validate_instance_parameters,
)
from sigilicon.virtuoso.legacy_ade import read_oa_load_instances
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


def test_maestro_capture_export_uses_synchronous_cellview_cleanup(
    workspace_factory,
) -> None:
    client = RecordingClient()

    with workspace_factory(client) as operation:
        export_oa_maestro_setup(
            client,
            library="lib",
            cell="tb",
            script_path=operation.root.parent / "capture.il",
            setup_path=operation.root.parent / "capture.sdb",
            operation=operation,
        )

    source = client.sources[0]
    assert "maeOpenSetup" in source
    assert "maeCloseSession(?session session ?forceClose nil)" in source
    assert "flowSyncBefore = dbGetOpenCellViews()" in source
    assert "flowSyncCloseAttempt = errset(dbClose(flowSyncCv) t)" in source
    assert '"Maestro setup export lib/tb"' in source
    assert "exact synchronous handle cleanup failed" in source


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


def test_config_handle_is_protected_by_unwind_cleanup(workspace_factory) -> None:
    client = RecordingClient()

    with workspace_factory(client, library="lib") as operation:
        with operation.mutation_scope(
            "lib", cells=("tb",), phase="test config creation"
        ):
            create_config_view(
                client,
                library="lib",
                testbench="tb",
                dut="dut",
                reference_libraries=("devices",),
                operation=operation,
            )

    source = client.sources[0]
    assert "unwindProtect" in source
    assert "hdbClose" in source
    assert "flowBeforeViews = dbGetOpenCellViews()" in source
    assert "member(flowCv flowBeforeViews)" in source
    assert "preserved exact dbIds" in source


def test_maestro_setup_owns_session_and_exact_new_cellviews(workspace_factory) -> None:
    client = RecordingClient()

    with workspace_factory(client, library='lib"unsafe') as operation:
        with operation.mutation_scope(
            'lib"unsafe', cells=('tb\\unsafe',), phase="test Maestro creation"
        ):
            create_maestro_view(
                client,
                library='lib"unsafe',
                testbench='tb\\unsafe',
                signals=('IN"unsafe', "OUT"),
                stop="16n",
                maxstep="20p",
                errpreset="conservative",
                model_file=Path('/model/unsafe"name.scs'),
                model_section='tt"unsafe',
                vdd=0.9,
                connect_rules="full",
                rise_time="20p",
                vthi=0.5,
                vtlo=0.3,
                operation=operation,
            )

    source = client.sources[0]
    assert "flowBeforeViews = dbGetOpenCellViews()" in source
    assert "maeCloseSession(?session session ?forceClose nil)" in source
    assert "flowMaestroOwnedScopes" in source
    assert "flowOwnedViews" in source
    assert "flowVisible" in source
    assert "preserved scope" in source
    assert "flowSyncBefore = dbGetOpenCellViews()" in source
    assert "flowSyncCloseAttempt = errset(dbClose(flowSyncCv) t)" in source
    assert "Maestro setup lib\\\"unsafe/tb\\\\unsafe" in source
    assert "lib\\\"unsafe" in source
    assert "tt\\\"unsafe" in source
    assert "flowOpenAttempt = errset(" in source
    assert source.index("flowAfterViews = dbGetOpenCellViews()") < source.index(
        "flowRecord = list"
    )
    assert source.index("flowRecord = list") < source.index(
        'unless(session error("maeOpenSetup failed after exact ownership capture"))'
    )


def test_ade_ie_card_contains_declared_thresholds_and_edges() -> None:
    card = build_ie_cards(
        vdd=0.9,
        connect_rules="full",
        rise_time="20p",
        vthi=0.5,
        vtlo=0.3,
    )

    assert "connectLib.CR_full_fast" in card
    assert "tr=20p;tf=20p;vthi=0.5;vtlo=0.3;" in card


def test_ade_ie_card_can_use_connect_rule_default_thresholds() -> None:
    card = build_ie_cards(
        vdd=0.9,
        connect_rules="full",
        rise_time="10p",
    )

    assert "connectLib.CR_full_fast" in card
    assert "tr=10p;tf=10p;" in card
    assert "vthi" not in card
    assert "vtlo" not in card


def test_oa_load_attestation_closes_its_exact_read_handle(workspace_factory) -> None:
    class Client:
        def __init__(self) -> None:
            self.source = ""

        def execute_skill(self, source, **_kwargs):
            self.source = source
            return SimpleNamespace(
                output='"CLOAD0|analogLib|cap|\\"2f\\"|OUT,0,\\n"',
                errors=[],
            )

    client = Client()
    with workspace_factory(client, library="lib") as operation:
        rows = read_oa_load_instances(
            client,
            "lib",
            "dut_ams",
            operation=operation,
        )

    assert rows == (
        {
            "instance": "CLOAD0",
            "library": "analogLib",
            "cell": "cap",
            "value": "2f",
            "nets": ("OUT", "0"),
        },
    )
    assert "unwindProtect" in client.source
    assert "dbClose(cv)" in client.source
