from __future__ import annotations

from pathlib import Path

import pytest

from sigilicon.paths import (
    ProjectContext,
    discover_project_context,
    validate_artifact_component,
)


RUN = "1" * 32
ATTEMPT = "2" * 32
FINGERPRINT = "3" * 64


def test_cli_discovery_uses_the_project_contract_not_pixi_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PIXI_PROJECT_ROOT", "/not/the/project")

    context = discover_project_context()

    assert context.project_root == tmp_path.resolve()
    assert context.config_root == (tmp_path / "configs").resolve()


def test_all_artifact_paths_match_the_single_layout(tmp_path: Path) -> None:
    paths = ProjectContext.from_project_root(tmp_path).artifacts

    assert paths.design_sync_attempt("lib", "inv", ATTEMPT).root == (
        tmp_path / "artifacts/designs/lib/inv/sync/attempts" / ATTEMPT
    )
    text_view = paths.oa_text_view_attempt("lib", "inv", "veriloga", ATTEMPT)
    assert text_view.root == (
        tmp_path
        / "artifacts/designs/lib/inv/views/veriloga/sync/attempts"
        / ATTEMPT
    )
    assert text_view.artifact_kind == "oa_text_view"
    assert paths.netlist_export_run(
        "lib", "inv", "schematic", "spectre", RUN
    ).root == (
        tmp_path
        / "artifacts/designs/lib/inv/exports/netlist/schematic/spectre/runs"
        / RUN
    )
    assert paths.standalone_run("lib", "tb_inv", RUN).root == (
        tmp_path / "artifacts/verification/lib/tb_inv/standalone/runs" / RUN
    )
    ade = paths.ade("lib", "tb_inv")
    assert ade.setup_attempt(FINGERPRINT, ATTEMPT).root == (
        tmp_path
        / "artifacts/verification/lib/tb_inv/ade/setups"
        / FINGERPRINT
        / "attempts"
        / ATTEMPT
    )
    assert ade.run(RUN).root == (
        tmp_path / "artifacts/verification/lib/tb_inv/ade/runs" / RUN
    )
    assert ade.run(RUN).roles == ("inputs", "results", "logs", "work")
    assert paths.import_attempt("lib", "source", ATTEMPT).root == (
        tmp_path / "artifacts/imports/lib/source/attempts" / ATTEMPT
    )
    analysis = paths.analysis_run("lib", "cell", "timing", "model", RUN)
    assert analysis.root == (
        tmp_path
        / "artifacts/designs/lib/cell/timing/model/runs"
        / RUN
    )
    assert analysis.artifact_kind == "analysis"
    assert paths.operation_incident(RUN).incident == (
        tmp_path / "artifacts/system/operations" / RUN / "incident.json"
    )


@pytest.mark.parametrize(
    "unsafe",
    ("", ".", "..", "../inv", "/absolute", "a/b", "a\\b", "has space"),
)
def test_artifact_components_reject_escape_and_separators(unsafe: str) -> None:
    with pytest.raises(ValueError, match="invalid artifact component"):
        validate_artifact_component(unsafe, "component")


def test_ids_fingerprints_and_role_components_are_validated(tmp_path: Path) -> None:
    paths = ProjectContext.from_project_root(tmp_path).artifacts
    with pytest.raises(ValueError, match="attempt id"):
        paths.design_sync_attempt("lib", "inv", "short")
    with pytest.raises(ValueError, match="setup fingerprint"):
        paths.ade("lib", "tb").setup_attempt("bad", ATTEMPT)
    execution = paths.standalone_run("lib", "tb", RUN)
    with pytest.raises(ValueError, match="path component"):
        execution.path("inputs", "../escape")


def test_project_root_cannot_be_reused_as_artifact_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must not be the project root"):
        ProjectContext.from_project_root(tmp_path, artifact_root=tmp_path)


def test_execution_creation_rejects_symlinked_structural_components(
    tmp_path: Path,
) -> None:
    artifacts = tmp_path / "artifacts"
    outside = tmp_path / "outside"
    artifacts.mkdir()
    outside.mkdir()
    (artifacts / "verification").mkdir()
    (artifacts / "verification" / "lib").symlink_to(
        outside,
        target_is_directory=True,
    )
    execution = ProjectContext.from_project_root(tmp_path).artifacts.standalone_run(
        "lib", "tb", RUN
    )

    with pytest.raises(RuntimeError, match="unsafe filesystem component"):
        execution.create()
    assert list(outside.iterdir()) == []
