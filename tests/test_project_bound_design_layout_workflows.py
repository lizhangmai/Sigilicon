from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from sigilicon.cli import generate_layout as generate_layout_cli
from sigilicon.cli import verify_layout as verify_layout_cli
from sigilicon.domain.repository import Project
from sigilicon.workflows import (
    design_lifecycle,
    layout_generation,
    layout_verification,
)

from conftest import write_component_owner


def _project(tmp_path: Path) -> Project:
    write_component_owner(tmp_path, "example", filesets={})
    return Project.from_project_root(tmp_path)


def test_layout_wrappers_preserve_an_explicit_project(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    spec_path = tmp_path / "ip/example/layout.toml"
    spec = SimpleNamespace(
        library="example",
        cell="cell",
        view="layout",
        generator="test",
        stage="placement_probe",
        layout_pdk=SimpleNamespace(dbu_per_micron=1000),
    )
    plan = SimpleNamespace(
        library="example",
        cell="cell",
        view="layout",
        generator="test",
        stage="placement_probe",
        dbu_per_micron=1000,
    )
    generation = object()
    verification = object()
    client = object()
    loaded: list[tuple[str, Project]] = []

    def load_for_generation(
        path: Path,
        *,
        project: Project,
    ) -> object:
        assert path == spec_path
        loaded.append(("generation", project))
        return spec

    def load_for_verification(
        path: Path,
        *,
        project: Project,
    ) -> object:
        assert path == spec_path
        loaded.append(("verification", project))
        return spec

    monkeypatch.setattr(layout_generation, "load_layout_spec", load_for_generation)
    monkeypatch.setattr(layout_generation, "build_layout_plan", lambda value: plan)
    monkeypatch.setattr(
        layout_generation,
        "generate_layout",
        lambda value, oa_client, *, timeout: generation,
    )
    monkeypatch.setattr(layout_verification, "load_layout_spec", load_for_verification)
    monkeypatch.setattr(
        layout_verification,
        "verify_layout",
        lambda value, oa_client, **kwargs: verification,
    )

    preview = layout_generation.plan_layout_spec(spec_path, project=project)
    generated_spec, generated = layout_generation.execute_layout_generation_spec(
        spec_path,
        client=client,
        project=project,
    )
    verified_spec, verified = layout_verification.execute_layout_verification_spec(
        spec_path,
        client=client,
        project=project,
        check="drc",
    )

    assert preview.spec is spec
    assert preview.plan is plan
    assert generated_spec is spec
    assert generated is generation
    assert verified_spec is spec
    assert verified is verification
    assert loaded == [
        ("generation", project),
        ("generation", project),
        ("verification", project),
    ]
    assert all(bound is project for _operation, bound in loaded)


def test_design_set_attestation_binds_project_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    paths = (tmp_path / "first.toml", tmp_path / "second.toml")
    client = object()
    inspections: list[tuple[Path, Project]] = []

    def inspect(path: Path, *, project: Project) -> Path:
        inspections.append((path, project))
        return path

    def attest(path: Path, oa_client: object, *, timeout: int) -> dict[str, object]:
        assert oa_client is client
        return {"path": path, "timeout": timeout}

    monkeypatch.setattr(design_lifecycle, "inspect_design", inspect)
    monkeypatch.setattr(design_lifecycle, "attest_oa_design", attest)

    result = design_lifecycle.attest_design_set(
        paths,
        client,
        project=project,
        timeout=17,
    )

    assert inspections == [(path, project) for path in paths]
    assert all(bound is project for _path, bound in inspections)
    assert result == {
        "passed": True,
        "designs": tuple({"path": path, "timeout": 17} for path in paths),
    }


def test_layout_clis_skip_discovery_and_reuse_one_loaded_spec(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _project(tmp_path)
    spec_path = tmp_path / "ip/example/layout.toml"
    spec = SimpleNamespace(library="example", cell="leaf", view="layout")
    plan = SimpleNamespace(canonical_json=lambda: '{"plan":true}\n')
    loads: list[Project] = []
    executed_checks: list[str] = []

    def fail_discovery(*_args: object) -> Path:
        pytest.fail("an explicit Project must bypass project discovery")

    def verify_set(
        path: Path,
        client: object,
        *,
        project: Project,
        checks: tuple[str, ...],
        xstream_timeout: int,
        calibre_timeout: int,
    ) -> tuple[tuple[object, object], ...]:
        assert path == spec_path
        loads.append(project)
        assert xstream_timeout == 12
        assert calibre_timeout == 34
        executed_checks.extend(checks)
        return tuple(
            (
                spec,
                SimpleNamespace(
                    check=check,
                    passed=True,
                    details={},
                    manifest_path=tmp_path / f"{check}.json",
                ),
            )
            for check in checks
        )

    monkeypatch.setattr(
        generate_layout_cli,
        "plan_layout_spec",
        lambda path, *, project: SimpleNamespace(spec=spec, plan=plan),
    )
    monkeypatch.setattr(
        verify_layout_cli,
        "execute_layout_verification_set",
        verify_set,
    )
    monkeypatch.setattr(generate_layout_cli, "discover_project_contract", fail_discovery)
    monkeypatch.setattr(verify_layout_cli, "discover_project_contract", fail_discovery)

    assert (
        generate_layout_cli.main(
            ["--spec", str(spec_path), "--preview"],
            client_factory=lambda: pytest.fail("preview must not open an OA client"),
            project=project,
        )
        == 0
    )
    assert (
        verify_layout_cli.main(
            [
                "--spec",
                str(spec_path),
                "--check",
                "all",
                "--xstream-timeout",
                "12",
                "--calibre-timeout",
                "34",
            ],
            client_factory=object,
            project=project,
        )
        == 0
    )

    assert loads == [project]
    assert executed_checks == ["drc", "lvs"]
    assert capsys.readouterr().out.startswith('{"plan":true}\n[drc] PASS')
