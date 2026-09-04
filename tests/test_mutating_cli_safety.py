from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from sigilicon.cli.main import main as sigilicon_main
from sigilicon.virtuoso.oa import WindowCloseResult
from sigilicon.adapters.cadence.virtuoso_operations import (
    close_cell,
    update_instance_parameters,
)


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
        "sigilicon.adapters.cadence.virtuoso_operations.workspace_operation",
        workspace,
    )
    monkeypatch.setattr(
        "sigilicon.adapters.cadence.virtuoso_operations.require_project_library_path",
        lambda *_args: events.append("project-library") or tmp_path / "virtuoso/lib",
    )
    monkeypatch.setattr(
        "sigilicon.adapters.cadence.virtuoso_operations.close_visible_cell_windows",
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
        "sigilicon.adapters.cadence.virtuoso_operations.workspace_operation",
        workspace,
    )
    reads = iter(({"w": "1u"}, {"w": "2u"}))
    monkeypatch.setattr(
        "sigilicon.adapters.cadence.virtuoso_operations.read_instance_parameters",
        lambda *_args, **_kwargs: events.append("read") or next(reads),
    )
    monkeypatch.setattr(
        "sigilicon.adapters.cadence.virtuoso_operations.set_instance_parameters",
        lambda *_args, **_kwargs: events.append("write") or {"w": "2u"},
    )
    monkeypatch.setattr(
        "sigilicon.adapters.cadence.virtuoso_operations.assert_cell_has_no_open_views",
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
        "sigilicon.cli._oa.open_project_cell",
        record("open-cell", None),
    )
    monkeypatch.setattr(
        "sigilicon.cli._oa.close_cell",
        record("close-cell", WindowCloseResult(0, 0)),
    )
    clients: list[object] = []

    def client_factory(resources):
        clients.append(resources)
        return object()

    assert sigilicon_main(
        ["oa", "open", "design", "top", "symbol"],
        oa_client_factory=client_factory,
    ) == 0
    assert sigilicon_main(
        ["oa", "close", "design", "top", "symbol"],
        oa_client_factory=client_factory,
    ) == 0
    assert [name for name, _args, _kwargs in events] == [
        "open-cell",
        "close-cell",
    ]
    assert events[0][1][2:] == ("design", "top", "symbol")
    assert events[1][1][2:] == ("design", "top", "symbol")
    assert len(clients) == 2
