from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path, PurePosixPath
from types import MappingProxyType, SimpleNamespace

import pytest

from sigilicon.domain.physical_verification import PhysicalVerificationPolicy
from sigilicon.domain.platform import PlatformAsset
from sigilicon.adapters.cadence import layout_verification
from sigilicon.execution._workspace import ExecutionWorkspace
from sigilicon.execution._model import Resources


def _drc_summary(*, violation_count: int = 0) -> str:
    return (
        "RULECHECK CONFIG_WARNING .... TOTAL Result Count = 2 (2)\n"
        f"RULECHECK M1.SPACING .... TOTAL Result Count = {violation_count} "
        f"({violation_count})\n"
        f"TOTAL DRC Results Generated: {violation_count + 2} "
        f"({violation_count + 2})\n"
        "LAYER SRAMDMY .... TOTAL Original Geometry Count = 0 (0)\n"
        "LAYER SRM_3 .... TOTAL Original Geometry Count = 0 (0)\n"
    )


def test_drc_parser_separates_warnings_violations_and_waiver_layers() -> None:
    clean = layout_verification.parse_drc_summary(
        _drc_summary(),
        configuration_warnings=("CONFIG_WARNING",),
        waiver_layers=("SRAMDMY", "SRM_3"),
    )
    violated = layout_verification.parse_drc_summary(
        _drc_summary(violation_count=3),
        configuration_warnings=("CONFIG_WARNING",),
        waiver_layers=("SRAMDMY", "SRM_3"),
    )

    assert clean["passed"] is True
    assert clean["configuration_warning_count"] == 2
    assert violated["passed"] is False
    assert violated["violation_count"] == 3


def test_lvs_parser_requires_the_exact_top_cell_comparison() -> None:
    assert layout_verification.parse_lvs_report(
        " CORRECT TOP TOP\n", primary="TOP"
    ) == {"passed": True, "comparison_result": "CORRECT"}
    assert layout_verification.parse_lvs_report(
        " INCORRECT TOP TOP\n", primary="TOP"
    ) == {"passed": False, "comparison_result": "INCORRECT"}
    with pytest.raises(RuntimeError, match="top-cell"):
        layout_verification.parse_lvs_report("CORRECT OTHER OTHER\n", primary="TOP")


def test_calibre_environment_uses_the_resource_snapshot(tmp_path: Path) -> None:
    executable = tmp_path / "calibre/bin/calibre"
    executable.parent.mkdir(parents=True)
    executable.write_text("fixture\n", encoding="utf-8")

    environment = layout_verification.calibre_environment(
        executable,
        {"PATH": "/snapshot/bin", "LM_LICENSE_FILE": "wrong", "MGLS_LICENSE_FILE": "ok"},
    )

    assert environment["PATH"] == "/snapshot/bin"
    assert "LM_LICENSE_FILE" not in environment
    assert environment["MGLS_LICENSE_FILE"] == "ok"
    assert environment["CALIBRE_HOME"] == str(executable.parent.parent)


def test_calibre_reaches_the_managed_process_seam(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "run"
    record = ExecutionWorkspace(
        run_id="calibre-managed-process",
        root=root,
        input_root=root / "inputs",
        work_root=root / "work",
        output_root=root / "outputs",
        log_root=root / "logs",
        source={},
    )
    executable = tmp_path / "calibre/bin/calibre"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    gds = tmp_path / "layout.gds"
    gds.write_bytes(b"gds")
    policy = PhysicalVerificationPolicy(
        path=tmp_path / "physical-verification.toml",
        drc_disabled_defines=MappingProxyType({}),
        drc_configuration_warnings=(),
        drc_waiver_layers=(),
    )
    spec = SimpleNamespace(
        cell="TOP",
        physical_verification=policy,
    )
    plan = SimpleNamespace(stage="routed")

    class Submitted(RuntimeError):
        pass

    class FakeProcess:
        @staticmethod
        def run(request):
            assert request.environment["MGLS_LICENSE_FILE"] == "fixture-license"
            assert request.before_spawn is not None
            request.before_spawn()
            raise Submitted("process submitted")

    monkeypatch.setattr(layout_verification, "managed_process", FakeProcess())

    with pytest.raises(Submitted, match="process submitted"):
        layout_verification._run_calibre(
            record,
            spec,
            plan,
            check="drc",
            deck_source=(
                'LAYOUT PATH "GDSFILENAME"\n'
                'LAYOUT PRIMARY "TOPCELLNAME"\n'
                'DRC RESULTS DATABASE "DRC_RES.db"\n'
                'DRC SUMMARY REPORT "DRC.rep"  // HIER\n'
            ),
            resources=Resources(
                tools={"mentor.calibre": str(executable)},
                environment={"MGLS_LICENSE_FILE": "fixture-license"},
            ),
            gds=gds,
            timeout=5,
        )


def test_xstream_artifacts_preserve_separate_output_streams(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "run"
    artifacts = ExecutionWorkspace(
        run_id="xstream-streams",
        root=root,
        input_root=root / "inputs",
        work_root=root / "work",
        output_root=root / "outputs",
        log_root=root / "logs",
        source={},
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "cds.lib").write_text("DEFINE example ./example\n", encoding="utf-8")
    native_log = tmp_path / "strmout.log"
    summary = tmp_path / "strmout.sum"
    gds = tmp_path / "layout.gds"
    native_log.write_text("native\n", encoding="utf-8")
    summary.write_text("summary\n", encoding="utf-8")
    gds.write_bytes(b"gds")
    monkeypatch.setattr(
        layout_verification,
        "run_xstream_export",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout="translator output\n",
            stderr="translator diagnostic\n",
            native_log_path=native_log,
            summary_path=summary,
            gds_path=gds,
        ),
    )
    spec = SimpleNamespace(
        workspace_root=workspace,
        library="example",
        cell="TOP",
        view="layout",
        pdk=SimpleNamespace(oa=SimpleNamespace(technology_library="technology")),
        layout_pdk=SimpleNamespace(
            xstream_flatten_pcells=True,
            xstream_suppressed_warnings=(),
        ),
    )

    layout_verification._run_xstream(
        artifacts,
        spec,
        layermap_source="M1 drawing 1 0\n",
        resources=Resources(tools={"cadence.xstream": "/bin/true"}),
        timeout=5,
    )

    assert (artifacts.output_root / "xstream-stdout.log").read_text() == (
        "translator output\n"
    )
    assert (artifacts.output_root / "xstream-stderr.log").read_text() == (
        "translator diagnostic\n"
    )


def test_layout_verification_binds_before_lease_and_commits_typed_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "run"
    artifacts = ExecutionWorkspace(
        run_id="1" * 32,
        root=root,
        input_root=root / "work/verify/inputs",
        work_root=root / "tool",
        output_root=root / "outputs/verify/verification",
        log_root=root / "work/verify/logs",
        source={},
    )
    policy = PhysicalVerificationPolicy(
        path=tmp_path / "policy.toml",
        drc_disabled_defines=MappingProxyType({}),
        drc_configuration_warnings=("CONFIG_WARNING",),
        drc_waiver_layers=("SRAMDMY", "SRM_3"),
    )
    project = SimpleNamespace(workspace_root=tmp_path / "workspace")
    layermap = tmp_path / "external/layermap"
    drc_deck = tmp_path / "external/drc.deck"
    lvs_deck = tmp_path / "external/lvs.deck"
    spec = SimpleNamespace(
        physical_verification=policy,
        oa_assembly_manifest=tmp_path / "oa.toml",
        workspace_root=project.workspace_root,
        library="example",
        cell="TOP",
        view="layout",
        pdk=SimpleNamespace(
            oa=SimpleNamespace(technology_library="example-tech")
        ),
        layout_pdk=SimpleNamespace(
            layermap=PlatformAsset(PurePosixPath("layermap"), layermap),
            drc_deck=PlatformAsset(PurePosixPath("drc.deck"), drc_deck),
            lvs_deck=PlatformAsset(PurePosixPath("lvs.deck"), lvs_deck),
        ),
    )
    plan = SimpleNamespace(
        stage="routed",
        canonical_json=lambda: '{"stage":"routed"}\n',
    )
    planning = SimpleNamespace(spec=spec, plan=plan)
    bound: list[object] = []

    class Operation:
        uncertain_reason = None

        def __init__(self) -> None:
            self.commits = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            if exc_type is None:
                for deferred in self.commits:
                    deferred.callback()
                    deferred.completed = True
            return False

        def view_lease(self, *_args, **_kwargs):
            assert bound == [self]
            return nullcontext()

        def require_project_library_target(self, *_args, **_kwargs):
            return project.workspace_root / "example"

        def defer_commit(self, callback):
            deferred = SimpleNamespace(callback=callback, completed=False)
            self.commits.append(deferred)
            return deferred

    operation = Operation()
    client = SimpleNamespace(
        library=SimpleNamespace(
            get=lambda *_args, **_kwargs: SimpleNamespace(
                technology_library="example-tech"
            )
        )
    )
    monkeypatch.setattr(
        layout_verification,
        "workspace_operation",
        lambda *_args, **_kwargs: operation,
    )
    monkeypatch.setattr(
        layout_verification, "validate_layout_plan", lambda *_args, **_kwargs: None
    )
    gds = tmp_path / "layout.gds"
    gds.write_bytes(b"canonical-gds")
    monkeypatch.setattr(
        layout_verification,
        "_run_xstream",
        lambda *_args, **_kwargs: gds,
    )
    evidence = layout_verification._parsed_evidence(
        spec,
        plan,
        gds,
        check="drc",
        report=_drc_summary(),
    )
    monkeypatch.setattr(
        layout_verification,
        "_run_calibre",
        lambda *_args, **_kwargs: evidence,
    )

    result = layout_verification.run_layout_verification(
        planning,
        client,
        check="drc",
        artifacts=artifacts,
        resources=Resources(),
        external_sources={
            layermap.resolve(): "bound layermap\n",
            drc_deck.resolve(): "bound drc deck\n",
        },
        operation_id="2" * 64,
        bind_operation=bound.append,
    )

    assert result.passed is True
    assert (artifacts.output_root / "completion.json").is_file()
    assert (artifacts.output_root / "typed-evidence.json").is_file()
