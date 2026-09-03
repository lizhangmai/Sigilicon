from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

from conftest import (
    write_project_context,
    write_test_layout_platform,
    write_test_platform,
)
from sigilicon.domain.platform import (
    PdkConfig,
    PlatformContract,
    load_platform,
    load_platform_catalog,
    load_platform_contract,
    load_platforms,
    resolve_platform_snapshot,
)
from sigilicon.execution.model import Resources
from sigilicon.project import Project


def test_platform_loads_typed_immutable_project_capabilities(tmp_path: Path) -> None:
    write_project_context(tmp_path)
    model = write_test_platform(tmp_path)

    platform = load_platform(
        Project.open(tmp_path), "testpdk", resources=Resources()
    )

    assert platform.simulation.default.file == model
    assert platform.simulation.default.single_section == "tt"
    assert platform.oa.technology_library == "techLib"
    assert platform.oa.reference_libraries == ("deviceLib",)
    assert platform.asset_root == tmp_path / "configs/platform/testpdk"
    assert platform.asset_root_resource is None
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


@pytest.mark.parametrize(
    ("omitted", "present"),
    (("oa", "simulation"), ("simulation", "oa")),
)
def test_platform_capabilities_are_independently_optional(
    tmp_path: Path,
    omitted: str,
    present: str,
) -> None:
    write_project_context(tmp_path)
    write_test_platform(tmp_path)
    manifest = tmp_path / "configs/platform/testpdk/platform.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            f'{omitted} = "{omitted}.toml"\n', ""
        ),
        encoding="utf-8",
    )

    platform = load_platform(
        Project.open(tmp_path), "testpdk", resources=Resources()
    )
    contract = load_platform_contract(Project.open(tmp_path), "testpdk")

    assert getattr(platform, omitted) is None
    assert getattr(contract, omitted) is None
    assert getattr(platform, present) is not None
    assert getattr(contract, present) is not None


def test_operation_inventory_reuses_one_project_snapshot(tmp_path: Path) -> None:
    write_project_context(tmp_path)
    write_test_platform(tmp_path)
    project = Project.open(tmp_path)
    inventory = load_platforms(project, resources=Resources())

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


@pytest.mark.parametrize("snapshot_kind", ("inventory", "platform"))
def test_runtime_platform_snapshot_rejects_project_manifest_drift(
    tmp_path: Path,
    snapshot_kind: str,
) -> None:
    contract = write_project_context(tmp_path)
    write_test_platform(tmp_path)
    platform_manifest = tmp_path / "configs/platform/testpdk/platform.toml"
    platform_manifest.write_text(
        platform_manifest.read_text(encoding="utf-8").replace(
            "\n[contracts]\n",
            '\nasset_scope = "external"\n\n[contracts]\n',
        ),
        encoding="utf-8",
    )
    first = tmp_path / "installed/first"
    second = tmp_path / "installed/second"
    for root in (first, second):
        root.mkdir(parents=True)
        (root / "model.scs").write_text("// installed model\n", encoding="utf-8")
    contract.write_text(
        contract.read_text(encoding="utf-8")
        + f'\n[runtime.directories]\n"platform.testpdk" = "{first}"\n',
        encoding="utf-8",
    )
    project = Project.open(tmp_path)
    inventory = load_platforms(project, resources=project.resources())
    snapshot = inventory if snapshot_kind == "inventory" else inventory["testpdk"]

    contract.write_text(
        contract.read_text(encoding="utf-8").replace(str(first), str(second)),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="project manifest snapshot"):
        resolve_platform_snapshot(project, "testpdk", snapshot=snapshot)


def test_resolve_platform_rejects_typed_and_source_drift(tmp_path: Path) -> None:
    write_project_context(tmp_path)
    write_test_layout_platform(tmp_path)
    project = Project.open(tmp_path)
    snapshot = load_platform(project, "testpdk", resources=Resources())
    assert snapshot.layout is not None

    with pytest.raises(ValueError, match="_authority.*specified"):
        replace(
            snapshot,
            layout=replace(
                snapshot.layout,
                dbu_per_micron=snapshot.layout.dbu_per_micron + 1,
            ),
        )

    snapshot.layout.layout_path.unlink()
    with pytest.raises(ValueError, match="source identity drift"):
        resolve_platform_snapshot(project, "testpdk", snapshot=snapshot)


def test_platform_contract_rejects_unknown_fields(tmp_path: Path) -> None:
    write_project_context(tmp_path)
    write_test_platform(tmp_path)
    simulation = tmp_path / "configs/platform/testpdk/simulation.toml"
    simulation.write_text(
        simulation.read_text(encoding="utf-8") + "\nmodel_sects = [\"tt\"]\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsupported fields.*model_sects"):
        load_platform(
            Project.open(tmp_path), "testpdk", resources=Resources()
        )


def test_platform_manifest_rejects_legacy_installation_schema(tmp_path: Path) -> None:
    write_project_context(tmp_path)
    write_test_platform(tmp_path)
    manifest = tmp_path / "configs/platform/testpdk/platform.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + '\n[installation]\nroot_environment = "OLD_ROOT"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsupported fields.*installation"):
        load_platform(
            Project.open(tmp_path), "testpdk", resources=Resources()
        )


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

    platform = load_platform(
        Project.open(tmp_path), "testpdk", resources=Resources()
    )

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

    assert (
        load_platform(project, "custom", resources=Resources()).path.name
        == "platform-contract.toml"
    )
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
asset_scope = "external"

[contracts]
''',
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "SIGILICON_PLATFORM_TESTPDK_ROOT", str(tmp_path / "wrong")
    )
    resources = Resources(
        directories={"platform.testpdk": str(package)}
    )

    platform = load_platform(Project.open(tmp_path), "testpdk", resources=resources)

    assert platform.simulation.default.file == model
    assert all(path.is_relative_to(tmp_path) for path in platform.source_documents)


def test_external_platform_contract_inventory_needs_no_runtime_root(
    tmp_path: Path,
) -> None:
    write_project_context(tmp_path)
    write_test_platform(tmp_path)
    manifest = tmp_path / "configs/platform/testpdk/platform.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "\n[contracts]\n",
            '\nasset_scope = "external"\n\n[contracts]\n',
        ),
        encoding="utf-8",
    )

    inventory = load_platforms(Project.open(tmp_path))
    platform = inventory["testpdk"]

    assert isinstance(platform, PlatformContract)
    assert not isinstance(platform, PdkConfig)
    assert not hasattr(platform, "_planning")
    assert (
        resolve_platform_snapshot(
            inventory.project,
            "testpdk",
            snapshot=inventory,
        )
        is platform
    )
    assert platform.asset_root_resource == "platform.testpdk"
    assert tuple(path.as_posix() for path in platform.asset_paths) == ("model.scs",)
    assert isinstance(platform.simulation.default.file, PurePosixPath)


def test_external_platform_model_cannot_traverse_a_symlink(
    tmp_path: Path,
) -> None:
    write_project_context(tmp_path)
    write_test_platform(tmp_path)
    package = tmp_path / "installed/testpdk"
    package.mkdir(parents=True)
    target = package / "real-model.scs"
    target.write_text("// installed model\n", encoding="utf-8")
    (package / "model.scs").symlink_to(target.name)
    manifest = tmp_path / "configs/platform/testpdk/platform.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "\n[contracts]\n",
            '''
asset_scope = "external"

[contracts]
''',
        ),
        encoding="utf-8",
    )
    resources = Resources(
        directories={"platform.testpdk": str(package)}
    )

    with pytest.raises(ValueError, match="must not traverse a symlink"):
        load_platform(Project.open(tmp_path), "testpdk", resources=resources)


def test_external_platform_requires_explicit_resource_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_project_context(tmp_path)
    write_test_platform(tmp_path)
    manifest = tmp_path / "configs/platform/testpdk/platform.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "\n[contracts]\n",
            '\nasset_scope = "external"\n\n[contracts]\n',
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "SIGILICON_PLATFORM_TESTPDK_ROOT", str(tmp_path / "ambient")
    )

    with pytest.raises(ValueError, match="platform.testpdk"):
        load_platform(
            Project.open(tmp_path), "testpdk", resources=Resources()
        )


def test_external_platform_snapshot_ignores_ambient_and_detects_explicit_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_project_context(tmp_path)
    write_test_platform(tmp_path)
    package = tmp_path / "installed/testpdk"
    package.mkdir(parents=True)
    (package / "model.scs").write_text("// installed model\n", encoding="utf-8")
    manifest = tmp_path / "configs/platform/testpdk/platform.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "\n[contracts]\n",
            '\nasset_scope = "external"\n\n[contracts]\n',
        ),
        encoding="utf-8",
    )
    resource = "platform.testpdk"
    resources = Resources(directories={resource: str(package)})
    project = Project.open(tmp_path)
    snapshot = load_platform(project, "testpdk", resources=resources)

    monkeypatch.setenv("SIGILICON_PLATFORM_TESTPDK_ROOT", str(tmp_path / "ambient"))
    assert resolve_platform_snapshot(project, "testpdk", snapshot=snapshot) is snapshot

def test_platform_asset_resources_preserve_distinct_catalog_keys(tmp_path: Path) -> None:
    write_project_context(tmp_path)
    write_test_platform(tmp_path, key="a-b")
    write_test_platform(tmp_path, key="a_b")
    catalog = tmp_path / "configs/platform/catalog.toml"
    catalog.write_text(
        catalog.read_text(encoding="utf-8").replace(
            '[platforms]\na_b = "a_b/platform.toml"',
            '[platforms]\na-b = "a-b/platform.toml"\na_b = "a_b/platform.toml"',
        ),
        encoding="utf-8",
    )

    assert set(load_platform_catalog(Project.open(tmp_path)).manifests) == {"a-b", "a_b"}


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
        load_platform(
            Project.open(tmp_path), "testpdk", resources=Resources()
        )
