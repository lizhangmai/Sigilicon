from __future__ import annotations

from types import MappingProxyType
from pathlib import Path

import pytest

from sigilicon.flow import ActionContext
from sigilicon.flow.native import NATIVE_OA_SIMULATION_ACTION
from sigilicon.workflows.builtin import build_flow_registry
from sigilicon.workflows.run_artifacts import FlowRunArtifacts


def _artifacts(tmp_path: Path) -> FlowRunArtifacts:
    run_root = tmp_path / "run"
    context = ActionContext(
        node_id="simulate",
        action=build_flow_registry().action(NATIVE_OA_SIMULATION_ACTION),
        run_root=run_root,
        work_root=run_root / "work/simulate",
        output_root=run_root / "outputs/simulate",
        log_root=run_root / "logs/simulate",
        inputs=MappingProxyType({}),
        action_config=MappingProxyType({}),
        adapter_config=MappingProxyType({}),
        capabilities=MappingProxyType({}),
        platform_assets=MappingProxyType({}),
    )
    return FlowRunArtifacts(context, "evidence", MappingProxyType({}))


def test_flow_run_artifacts_rejects_symlinked_role_root(tmp_path: Path) -> None:
    artifacts = _artifacts(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    inputs = artifacts.context.work_root / "inputs"
    inputs.parent.mkdir(parents=True)
    inputs.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        artifacts.write_text("inputs", ("escaped.txt",), "unsafe\n")

    assert not (outside / "escaped.txt").exists()


def test_flow_run_artifacts_rejects_nested_parent_symlink(tmp_path: Path) -> None:
    artifacts = _artifacts(tmp_path)
    inputs = artifacts.directory("inputs")
    outside = tmp_path / "outside"
    outside.mkdir()
    (inputs / "nested").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        artifacts.write_text("inputs", ("nested", "escaped.txt"), "unsafe\n")

    assert not (outside / "escaped.txt").exists()


def test_flow_run_artifacts_never_replace_existing_destination(tmp_path: Path) -> None:
    artifacts = _artifacts(tmp_path)
    destination = artifacts.write_text("outputs", ("evidence.json",), "first\n")

    with pytest.raises(FileExistsError):
        artifacts.write_text("outputs", ("evidence.json",), "second\n")

    assert destination.read_text(encoding="utf-8") == "first\n"


def test_flow_run_artifacts_reads_sources_without_following_symlinks(
    tmp_path: Path,
) -> None:
    artifacts = _artifacts(tmp_path)
    source = tmp_path / "source.bin"
    source.write_bytes(b"trusted")
    alias = tmp_path / "source-link.bin"
    alias.symlink_to(source)

    with pytest.raises(OSError):
        artifacts.copy_file("inputs", ("copied.bin",), alias)

    assert not artifacts.path("inputs", "copied.bin").exists()
