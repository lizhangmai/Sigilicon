from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from conftest import (
    write_project_context,
    write_test_layout_platform,
    write_test_platform,
)
from sigilicon.domain.platform import (
    load_platform,
    load_platform_catalog,
    load_platform_inventory,
    resolve_platform,
    resolve_platform_snapshot,
)
from sigilicon.project import Project


def test_platform_loads_typed_immutable_project_capabilities(tmp_path: Path) -> None:
    write_project_context(tmp_path)
    model = write_test_platform(tmp_path)

    platform = load_platform(Project.open(tmp_path), "testpdk")

    assert platform.simulation.default.file == model
    assert platform.simulation.default.single_section == "tt"
    assert platform.oa.technology_library == "techLib"
    assert platform.oa.reference_libraries == ("deviceLib",)
    assert platform.source_paths == (
        tmp_path / "configs/platform/catalog.toml",
        tmp_path / "configs/platform/testpdk/platform.toml",
        tmp_path / "configs/platform/testpdk/simulation.toml",
        tmp_path / "configs/platform/testpdk/oa.toml",
    )
    with pytest.raises(TypeError):
        platform.source_documents[platform.simulation.path][
            "default_model_set"
        ] = "other"


def test_operation_inventory_reuses_one_project_snapshot(tmp_path: Path) -> None:
    write_project_context(tmp_path)
    write_test_platform(tmp_path)
    project = Project.open(tmp_path)
    inventory = load_platform_inventory(project)

    assert (
        resolve_platform_snapshot(project, "testpdk", snapshot=inventory)
        is inventory["testpdk"]
    )
    with pytest.raises(TypeError):
        inventory["testpdk"].simulation.model_sets["forged"] = object()
    with pytest.raises(ValueError, match="has no 'other' entry"):
        resolve_platform_snapshot(project, "other", snapshot=inventory)

    with pytest.raises(ValueError, match="different operation"):
        resolve_platform_snapshot(
            Project.open(tmp_path),
            "testpdk",
            snapshot=inventory,
        )


def test_resolve_platform_rejects_typed_and_source_drift(tmp_path: Path) -> None:
    write_project_context(tmp_path)
    write_test_layout_platform(tmp_path)
    project = Project.open(tmp_path)
    snapshot = load_platform(project, "testpdk")
    assert snapshot.layout is not None

    with pytest.raises(ValueError, match="platform identity drift"):
        resolve_platform(
            project,
            "testpdk",
            snapshot=replace(
                snapshot,
                layout=replace(
                    snapshot.layout,
                    dbu_per_micron=snapshot.layout.dbu_per_micron + 1,
                ),
            ),
        )

    snapshot.layout.layout_path.unlink()
    with pytest.raises(ValueError, match="source identity drift"):
        resolve_platform(project, "testpdk", snapshot=snapshot)


def test_platform_contract_rejects_unknown_fields(tmp_path: Path) -> None:
    write_project_context(tmp_path)
    write_test_platform(tmp_path)
    simulation = tmp_path / "configs/platform/testpdk/simulation.toml"
    simulation.write_text(
        simulation.read_text(encoding="utf-8") + "\nmodel_sects = [\"tt\"]\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsupported fields.*model_sects"):
        load_platform(Project.open(tmp_path), "testpdk")


def test_layout_platform_resolves_optional_and_materialization_capabilities(
    tmp_path: Path,
) -> None:
    write_project_context(tmp_path)
    write_test_layout_platform(tmp_path)
    verification = tmp_path / "configs/platform/testpdk/verification.toml"
    verification.write_text(
        verification.read_text(encoding="utf-8").replace(
            'qrc_tech_file = "qrc.tech"\n', ""
        ),
        encoding="utf-8",
    )
    layout = tmp_path / "configs/platform/testpdk/layout.toml"
    layout.write_text(
        layout.read_text(encoding="utf-8")
        + '''
[oa_materialization.layers.routing1]
layer = "M1"
drawing_purpose = "drawing"
pin_purpose = "pin"
blockage_purpose = "drawing"

[oa_materialization.vias]
routing1_routing2 = "M2_M1c"
''',
        encoding="utf-8",
    )

    platform = load_platform(Project.open(tmp_path), "testpdk")

    assert platform.layout is not None
    assert platform.layout.qrc_tech_file is None
    mapping = platform.layout.oa_materialization
    assert mapping is not None
    assert mapping.layers["routing1"].layer == "M1"
    assert mapping.vias == {"routing1_routing2": "M2_M1c"}


def test_platform_catalog_selects_explicit_manifest_only(tmp_path: Path) -> None:
    write_project_context(tmp_path)
    write_test_platform(tmp_path, key="custom")
    catalog = tmp_path / "configs/platform/catalog.toml"
    catalog.write_text(
        catalog.read_text(encoding="utf-8").replace(
            'custom/platform.toml', 'custom/platform-contract.toml'
        )
        + 'other = "../outside.toml"\n',
        encoding="utf-8",
    )
    manifest = tmp_path / "configs/platform/custom/platform.toml"
    manifest.rename(manifest.with_name("platform-contract.toml"))
    project = Project.open(tmp_path)

    assert load_platform(project, "custom").path.name == "platform-contract.toml"
    with pytest.raises(ValueError, match="safe relative path"):
        load_platform_catalog(project)


def test_external_platform_assets_are_not_source_documents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_project_context(tmp_path)
    write_test_platform(tmp_path)
    package = tmp_path / "installed/testpdk"
    package.mkdir(parents=True)
    model = package / "model.scs"
    model.write_text("// installed model\n", encoding="utf-8")
    manifest = tmp_path / "configs/platform/testpdk/platform.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "\n[contracts]\n",
            '''
[installation]
root_environment = "TEST_PDK_ROOT"
package_root = "testpdk"

[contracts]
''',
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_PDK_ROOT", str(tmp_path / "installed"))

    platform = load_platform(Project.open(tmp_path), "testpdk")

    assert platform.simulation.default.file == model
    assert all(path.is_relative_to(tmp_path) for path in platform.source_documents)


def test_platform_contract_owner_matches_manifest(tmp_path: Path) -> None:
    write_project_context(tmp_path)
    write_test_platform(tmp_path)
    simulation = tmp_path / "configs/platform/testpdk/simulation.toml"
    simulation.write_text(
        simulation.read_text(encoding="utf-8").replace(
            'owner = "test-platform"', 'owner = "another-owner"'
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="owner must be 'test-platform'"):
        load_platform(Project.open(tmp_path), "testpdk")
