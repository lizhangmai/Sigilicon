from __future__ import annotations

import os
from pathlib import Path
from types import MappingProxyType

import pytest

from sigilicon.flow import ActionContext
from sigilicon.flow.circuit_design import DESIGN_ELECTRICAL_DIAGNOSTIC_ACTION
from sigilicon.workflows.builtin import build_flow_registry
from sigilicon.workflows.run_artifacts import (
    managed_run_artifact_environment,
)
from sigilicon.workflows.spectre import (
    SpectreArtifactContext,
    SpectreExecution,
    StagedSpectreInput,
    find_spectre,
    run_spectre_measurement,
)

from conftest import write_component_owner
from sigilicon.domain.repository import Project


def test_find_spectre_preserves_the_public_launcher_symlink(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "spectre-real"
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    target.chmod(0o755)
    launcher = tmp_path / "spectre"
    launcher.symlink_to(target.name)
    monkeypatch.setenv("VB_SPECTRE_BIN", str(launcher))

    result = find_spectre()

    assert result == Path(os.path.abspath(launcher))
    assert result.is_symlink()


def test_spectre_measurement_inherits_one_parent_flow_lifecycle(
    tmp_path: Path,
    monkeypatch,
) -> None:
    write_component_owner(tmp_path, "example", filesets={})
    project = Project.from_project_root(tmp_path)
    run_root = tmp_path / "artifacts/run"
    action = build_flow_registry().action(
        DESIGN_ELECTRICAL_DIAGNOSTIC_ACTION
    )
    action_context = ActionContext(
        node_id="electrical",
        action=action,
        run_root=run_root,
        work_root=run_root / "work/electrical",
        output_root=run_root / "outputs/electrical",
        log_root=run_root / "logs/electrical",
        inputs=MappingProxyType({}),
        action_config=MappingProxyType({}),
        adapter_config=MappingProxyType({}),
        capabilities=MappingProxyType({}),
        platform_assets=MappingProxyType({}),
    )
    environment = managed_run_artifact_environment(
        action_context,
        "evidence",
        {"project": {"commit": "fixture"}},
    )
    source = tmp_path / "source.scs"
    source.write_text("simulator lang=spectre\n", encoding="utf-8")

    def execute(artifacts, **_kwargs):
        raw = artifacts.write_text("work", ("result.dat",), "1 2\n")
        log = artifacts.write_text("logs", ("spectre.stdout.log",), "ok\n")
        deck = artifacts.write_text("inputs", ("spectre.scs",), "deck\n")
        return SpectreExecution(
            executable=Path("/tool/spectre"),
            canonical_deck=deck,
            invocation_deck=deck,
            stdout_log=log,
            native_log=None,
            raw_outputs={"result.dat": raw},
        )

    monkeypatch.setattr("sigilicon.workflows.spectre.run_spectre_deck", execute)
    measurement = dict(
        kind="diagnostic",
        condition={"corner": "tt"},
        inputs=(StagedSpectreInput("source", source, ("source.scs",), "source"),),
        external_input_references={},
        render=lambda _paths: "deck\n",
        output_name="result.dat",
        raw_result_name="raw.dat",
        parse=lambda text: text,
        normalize=lambda text: text,
        normalized_name="waveform.csv",
        evaluate=lambda _value: {"passed": True},
        timeout=10,
    )
    context = SpectreArtifactContext(project, "example", "cell", "testbench")
    with pytest.raises(RuntimeError, match="require artifacts owned by a parent Flow"):
        run_spectre_measurement(context, **measurement)

    monkeypatch.setenv(
        "SIGILICON_MANAGED_RUN_ARTIFACTS",
        environment["SIGILICON_MANAGED_RUN_ARTIFACTS"],
    )
    result = run_spectre_measurement(context, **measurement)

    assert result.run_id == "run"
    assert result.run_dir == run_root
    assert result.manifest_path == run_root / "run_manifest.json"
    assert result.measurements.is_relative_to(
        action_context.output_root / "evidence"
    )
    assert list(run_root.rglob("run_manifest.json")) == []
