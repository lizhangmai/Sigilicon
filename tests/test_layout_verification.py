from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from sigilicon.domain.physical_verification import PhysicalVerificationPolicy
from sigilicon.workflows import layout_verification
from sigilicon.execution.step_files import StepFiles


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


def test_layout_verification_binds_before_lease_and_commits_typed_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "run"
    artifacts = StepFiles(
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
        project=project,
        library="example",
        cell="TOP",
        view="layout",
        pdk=SimpleNamespace(
            oa=SimpleNamespace(technology_library="example-tech")
        ),
        layout_pdk=SimpleNamespace(
            layermap=layermap,
            drc_deck=drc_deck,
            lvs_deck=lvs_deck,
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
        xstream=tmp_path / "strmout",
        calibre=tmp_path / "calibre",
        environment={},
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
