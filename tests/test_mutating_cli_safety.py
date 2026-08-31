from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import tomllib

from sigilicon.cli.open_cell import main as open_cell_main
from sigilicon.cli.close_cell import main as close_cell_main
from sigilicon.virtuoso.oa import close_visible_cell_windows
from sigilicon.virtuoso.oa import WindowCloseResult
from sigilicon.virtuoso.schematic import set_instance_parameters
from sigilicon.virtuoso.workspace import OperationPolicy
from sigilicon.workflows.virtuoso_operations import (
    close_cell,
    update_instance_parameters,
)


class RecordingClient:
    def __init__(self, output: str = "t") -> None:
        self.output = output
        self.sources: list[str] = []

    def execute_skill(self, source: str, **_kwargs):
        self.sources.append(source)
        return SimpleNamespace(output=self.output, errors=[])


def test_internal_oa_mutation_helpers_are_not_public_commands() -> None:
    with (Path(__file__).resolve().parents[1] / "pyproject.toml").open("rb") as stream:
        tasks = tomllib.load(stream)["project"]["scripts"]

    assert not {
        "create-library",
        "manual-set-params",
        "manual-import-netlist",
        "manual-generate-symbol",
    }.intersection(tasks)


def test_noncanonical_engineering_cli_modules_are_removed() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "sigilicon" / "cli"

    for module in (
        "create_library.py",
        "generate_layout.py",
        "generate_symbol.py",
        "import_netlist.py",
        "set_params.py",
        "verify_layout.py",
        "xcelium.py",
    ):
        assert not (root / module).exists()


def test_close_cell_workflow_enters_workspace_and_project_library_policy(
    monkeypatch, tmp_path: Path
) -> None:
    events: list[str] = []

    @contextmanager
    def workspace(client, root, name, **_kwargs):
        events.append(f"workspace:{name}")
        @contextmanager
        def lease(*_args, **_lease_kwargs):
            yield

        @contextmanager
        def mutation_scope(*_args, **_mutation_kwargs):
            events.append("quiescent")
            yield SimpleNamespace(quarantined=[])

        yield SimpleNamespace(
            client=client,
            root=root,
            name=name,
            view_lease=lease,
            mutation_scope=mutation_scope,
        )

    paths = SimpleNamespace(workspace_root=tmp_path / "virtuoso")
    client = object()
    monkeypatch.setattr(
        "sigilicon.workflows.virtuoso_operations.workspace_operation",
        workspace,
    )
    monkeypatch.setattr(
        "sigilicon.workflows.virtuoso_operations.require_project_library_path",
        lambda *_args: events.append("project-library") or tmp_path / "virtuoso/lib",
    )
    monkeypatch.setattr(
        "sigilicon.workflows.virtuoso_operations.close_visible_cell_windows",
        lambda *_args, **_kwargs: events.append("exact-close")
        or WindowCloseResult(1, 0),
    )

    assert close_cell(client, paths, "lib", "cell", None) == WindowCloseResult(1, 0)
    assert events == ["workspace:close-cell", "project-library", "exact-close"]


def test_manual_set_params_workflow_cannot_bypass_quiescent_cell_policy(
    monkeypatch, tmp_path: Path
) -> None:
    events: list[str] = []

    @contextmanager
    def workspace(client, root, name, **_kwargs):
        events.append(f"workspace:{name}")
        @contextmanager
        def lease(*_args, **_lease_kwargs):
            yield

        @contextmanager
        def mutation_scope(*_args, **_mutation_kwargs):
            events.append("quiescent")
            yield SimpleNamespace(quarantined=[])

        yield SimpleNamespace(
            client=client,
            root=root,
            name=name,
            view_lease=lease,
            mutation_scope=mutation_scope,
        )

    paths = SimpleNamespace(workspace_root=tmp_path / "virtuoso")
    client = object()
    monkeypatch.setattr(
        "sigilicon.workflows.virtuoso_operations.workspace_operation",
        workspace,
    )
    reads = iter(({"w": "1u"}, {"w": "2u"}))
    monkeypatch.setattr(
        "sigilicon.workflows.virtuoso_operations.read_instance_parameters",
        lambda *_args, **_kwargs: events.append("read") or next(reads),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.virtuoso_operations.set_instance_parameters",
        lambda *_args, **_kwargs: events.append("write") or {"w": "2u"},
    )
    monkeypatch.setattr(
        "sigilicon.workflows.virtuoso_operations.assert_cell_has_no_open_views",
        lambda *_args: events.append("closed"),
    )

    result = update_instance_parameters(
        client,
        paths,
        "lib",
        "cell",
        "M0",
        {"w": "2u"},
    )
    assert result.applied == {"w": "2u"}
    assert events == [
        "workspace:manual-set-params",
        "read",
        "quiescent",
        "write",
        "read",
        "closed",
    ]


def test_public_mutating_clis_delegate_to_application_workflows(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    events: list[tuple[str, tuple, dict]] = []

    def record(name, result):
        def workflow(*args, **kwargs):
            events.append((name, args, kwargs))
            return result

        return workflow

    monkeypatch.setattr(
        "sigilicon.cli.open_cell.open_project_cell",
        record("open-cell", None),
    )
    monkeypatch.setattr(
        "sigilicon.cli.close_cell.close_cell",
        record("close-cell", WindowCloseResult(0, 0)),
    )
    assert open_cell_main(["design", "top", "symbol"], client_factory=object) == 0
    assert close_cell_main(["design", "top", "symbol"], client_factory=object) == 0
    assert [name for name, _args, _kwargs in events] == [
        "open-cell",
        "close-cell",
    ]
    assert events[0][1][2:] == ("design", "top", "symbol")
    assert events[1][1][2:] == ("design", "top", "symbol")


def test_close_cell_skill_uses_exact_cellview_identity_and_escaping(
    workspace_factory,
) -> None:
    client = RecordingClient(output="(0 0)")

    with workspace_factory(client, policy=OperationPolicy.GUI_ACTION) as operation:
        result = close_visible_cell_windows(
            client,
            'lib"name',
            "cell\\name",
            "schematic",
            operation=operation,
        )

    assert result == WindowCloseResult(0, 0)

    source = client.sources[0]
    assert "libName ==" in source and "cellName ==" in source
    assert 'lib\\"name' in source
    assert "hidden or unowned open cellview" in source
    assert "target windows remained open" in source
    assert "window != hiGetCIWindow()" in source
    assert 'equal(hiGetWidgetType(window) "graphics")' in source


def test_parameter_update_escapes_values_and_releases_cdf_and_oa_state(
    workspace_factory,
) -> None:
    client = RecordingClient()

    with workspace_factory(client, library="lib") as operation:
        with operation.mutation_scope(
            "lib", cells=("cell",), phase="test parameter update"
        ):
            applied = set_instance_parameters(
                client,
                "lib",
                "cell",
                'M0"unsafe',
                {"w": '2u"unsafe'},
                operation=operation,
            )

    assert applied == {"w": '2u"unsafe'}
    source = client.sources[0]
    assert 'M0\\"unsafe' in source
    assert '2u\\"unsafe' in source
    assert "unwindProtect" in source
    assert "dbSave" in source and "dbClose" in source
    assert "flowSavedCdfValues" in source
    assert 'equal(param~>paramType "int")' in source
    assert "atoi(arrayref(paramVals name))" in source
    assert "flowBeforeViews = dbGetOpenCellViews()" in source
    assert "member(flowCv flowBeforeViews)" in source


def test_parameter_update_can_skip_gui_only_cdf_callbacks(
    workspace_factory,
) -> None:
    client = RecordingClient()

    with workspace_factory(client, library="lib") as operation:
        with operation.mutation_scope(
            "lib", cells=("cell",), phase="headless parameter update"
        ):
            set_instance_parameters(
                client,
                "lib",
                "cell",
                "V0",
                {"tvpairs": "2"},
                operation=operation,
                invoke_callbacks=False,
            )

    source = client.sources[0]
    assert "cdfUpdateInstParam(inst)" in source
    assert "evalstring(callback)" not in source
