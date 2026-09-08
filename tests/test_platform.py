from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

from conftest import write_project_context, write_test_layout_platform, write_test_platform
from sigilicon.domain.platform import (
    Platform,
    load_platform,
    load_platform_catalog,
    load_platforms,
    resolve_platform_snapshot,
)
from sigilicon.execution._resources import Resources
from sigilicon.project import Project


def test_platform_loads_typed_immutable_project_capabilities(tmp_path: Path) -> None:
    write_project_context(tmp_path)
    model = write_test_platform(tmp_path)

    platform = load_platform(
        Project.open(tmp_path), "fixture", "testpdk", resources=Resources()
    )

    assert platform.simulation.default.file.require_path() == model
    assert platform.simulation.default.single_section == "tt"
    assert platform.oa.technology_library == "techLib"
    assert platform.oa.reference_libraries == ("deviceLib",)
    assert platform.asset_root == tmp_path / "ip/fixture/configs/platform/testpdk"
    assert platform.asset_root_resource is None
    assert platform.source_paths == (
        tmp_path / "ip/fixture/configs/platform/catalog.toml",
        tmp_path / "ip/fixture/configs/platform/testpdk/platform.toml",
        tmp_path / "ip/fixture/configs/platform/testpdk/simulation.toml",
        tmp_path / "ip/fixture/configs/platform/testpdk/oa.toml",
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
    manifest = tmp_path / "ip/fixture/configs/platform/testpdk/platform.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            f'{omitted} = "{omitted}.toml"\n', ""
        ),
        encoding="utf-8",
    )

    platform = load_platform(
        Project.open(tmp_path), "fixture", "testpdk", resources=Resources()
    )
    contract = load_platform(Project.open(tmp_path), "fixture", "testpdk")

    assert getattr(platform, omitted) is None
    assert getattr(contract, omitted) is None
    assert getattr(platform, present) is not None
    assert getattr(contract, present) is not None


def test_platform_inventory_reuses_one_owner_snapshot(tmp_path: Path) -> None:
    write_project_context(tmp_path)
    write_test_platform(tmp_path)
    project = Project.open(tmp_path)
    inventory = load_platforms(project, "fixture", resources=Resources())

    assert (
        resolve_platform_snapshot(project, "fixture", "testpdk", snapshot=inventory)
        is inventory["testpdk"]
    )
    with pytest.raises(TypeError):
        inventory["testpdk"].simulation.model_sets["forged"] = object()
    with pytest.raises(ValueError, match="has no 'other' entry"):
        resolve_platform_snapshot(project, "fixture", "other", snapshot=inventory)

    reopened = Project.open(tmp_path)
    assert (
        resolve_platform_snapshot(reopened, "fixture", "testpdk", snapshot=inventory)
        is inventory["testpdk"]
    )


def test_same_platform_key_is_isolated_by_component_owner(tmp_path: Path) -> None:
    write_project_context(tmp_path)
    alpha_model = write_test_platform(tmp_path, owner="alpha")
    beta_model = write_test_platform(tmp_path, owner="beta")
    project = Project.open(tmp_path)

    alpha = load_platform(project, "alpha", "testpdk", resources=Resources())
    beta = load_platform(project, "beta", "testpdk", resources=Resources())

    assert alpha.owner == "alpha"
    assert beta.owner == "beta"
    assert alpha.path != beta.path
    assert alpha.simulation.default.file.require_path() == alpha_model
    assert beta.simulation.default.file.require_path() == beta_model
    with pytest.raises(ValueError, match="snapshot owner"):
        resolve_platform_snapshot(
            project,
            "beta",
            "testpdk",
            snapshot=alpha,
        )


@pytest.mark.parametrize("snapshot_kind", ("inventory", "platform"))
def test_runtime_platform_snapshot_rejects_project_manifest_drift(
    tmp_path: Path,
    snapshot_kind: str,
) -> None:
    contract = write_project_context(tmp_path)
    write_test_platform(tmp_path)
    platform_manifest = tmp_path / "ip/fixture/configs/platform/testpdk/platform.toml"
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
        + f'\n[runtime.directories]\n"platform.fixture.testpdk" = "{first}"\n',
        encoding="utf-8",
    )
    project = Project.open(tmp_path)
    inventory = load_platforms(project, "fixture", resources=project.resources())
    snapshot = inventory if snapshot_kind == "inventory" else inventory["testpdk"]

    contract.write_text(
        contract.read_text(encoding="utf-8").replace(str(first), str(second)),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="project manifest snapshot"):
        resolve_platform_snapshot(project, "fixture", "testpdk", snapshot=snapshot)


def test_resolve_platform_rejects_typed_and_source_drift(tmp_path: Path) -> None:
    write_project_context(tmp_path)
    write_test_layout_platform(tmp_path)
    project = Project.open(tmp_path)
    snapshot = load_platform(project, "fixture", "testpdk", resources=Resources())
    assert snapshot.layout is not None

    with pytest.raises((TypeError, ValueError), match="_authority.*specified"):
        replace(
            snapshot,
            layout=replace(
                snapshot.layout,
                dbu_per_micron=snapshot.layout.dbu_per_micron + 1,
            ),
        )

    snapshot.layout.layout_path.unlink()
    with pytest.raises(ValueError, match="source identity drift"):
        resolve_platform_snapshot(project, "fixture", "testpdk", snapshot=snapshot)


def test_platform_contract_rejects_unknown_fields(tmp_path: Path) -> None:
    write_project_context(tmp_path)
    write_test_platform(tmp_path)
    simulation = tmp_path / "ip/fixture/configs/platform/testpdk/simulation.toml"
    simulation.write_text(
        simulation.read_text(encoding="utf-8") + "\nmodel_sects = [\"tt\"]\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsupported fields.*model_sects"):
        load_platform(
            Project.open(tmp_path), "fixture", "testpdk", resources=Resources()
        )


def test_platform_manifest_rejects_unknown_table(tmp_path: Path) -> None:
    write_project_context(tmp_path)
    write_test_platform(tmp_path)
    manifest = tmp_path / "ip/fixture/configs/platform/testpdk/platform.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + "\n[unexpected]\nvalue = true\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsupported fields.*unexpected"):
        load_platform(
            Project.open(tmp_path), "fixture", "testpdk", resources=Resources()
        )


@pytest.mark.parametrize("capability", ["layout", "verification"])
def test_platform_capabilities_are_independent(tmp_path: Path, capability: str) -> None:
    write_test_layout_platform(tmp_path)
    manifest = tmp_path / "ip/fixture/configs/platform/testpdk/platform.toml"
    source = manifest.read_text().split("[contracts]")[0]
    manifest.write_text(source + f'[contracts]\n{capability} = "{capability}.toml"\n')
    platform = load_platform(Project.open(tmp_path), "fixture", "testpdk", resources=Resources())
    assert platform.simulation is None
    assert platform.oa is None
    if capability == "verification":
        assert platform.layout is None
        assert platform.verification.require_check("drc").asset.require_path().name == "drc.deck"
    else:
        assert platform.verification is None
        assert platform.layout.dbu_per_micron == 1000


def test_layout_platform_resolves_optional_and_materialization_capabilities(
    tmp_path: Path,
) -> None:
    write_project_context(tmp_path)
    write_test_layout_platform(tmp_path)
    verification = tmp_path / "ip/fixture/configs/platform/testpdk/verification.toml"
    verification.write_text(
        verification.read_text(encoding="utf-8").replace(
            'qrc_tech_file = "qrc.tech"\n', ""
        ),
        encoding="utf-8",
    )
    layout = tmp_path / "ip/fixture/configs/platform/testpdk/layout.toml"
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
        Project.open(tmp_path), "fixture", "testpdk", resources=Resources()
    )

    assert platform.layout is not None
    assert platform.verification.qrc_tech_file is None
    mapping = platform.layout.oa_materialization
    assert mapping is not None
    assert mapping.layers["routing1"].layer == "M1"
    assert mapping.vias == {"routing1_routing2": "M2_M1c"}


def test_platform_catalog_selects_explicit_manifest_only(tmp_path: Path) -> None:
    write_project_context(tmp_path)
    write_test_platform(tmp_path, key="custom")
    catalog = tmp_path / "ip/fixture/configs/platform/catalog.toml"
    catalog.write_text(
        catalog.read_text(encoding="utf-8").replace(
            'custom/platform.toml', 'custom/platform-contract.toml'
        )
        + 'other = "../outside.toml"\n',
        encoding="utf-8",
    )
    manifest = tmp_path / "ip/fixture/configs/platform/custom/platform.toml"
    manifest.rename(manifest.with_name("platform-contract.toml"))
    project = Project.open(tmp_path)

    assert (
        load_platform(project, "fixture", "custom", resources=Resources()).path.name
        == "platform-contract.toml"
    )
    with pytest.raises(ValueError, match="safe relative path"):
        load_platform_catalog(project, "fixture")


def test_platform_catalog_owner_must_match_the_project(tmp_path: Path) -> None:
    write_project_context(tmp_path)
    write_test_platform(tmp_path)
    catalog = tmp_path / "ip/fixture/configs/platform/catalog.toml"
    catalog.write_text(
        catalog.read_text(encoding="utf-8").replace(
            'owner = "fixture"', 'owner = "another-owner"'
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="owner must be 'fixture'"):
        load_platform_catalog(Project.open(tmp_path), "fixture")


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
    manifest = tmp_path / "ip/fixture/configs/platform/testpdk/platform.toml"
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
        directories={"platform.fixture.testpdk": str(package)}
    )

    platform = load_platform(Project.open(tmp_path), "fixture", "testpdk", resources=resources)

    assert platform.simulation.default.file.require_path() == model
    assert all(path.is_relative_to(tmp_path) for path in platform.source_documents)


def test_external_platform_contract_inventory_needs_no_runtime_root(
    tmp_path: Path,
) -> None:
    write_project_context(tmp_path)
    write_test_platform(tmp_path)
    manifest = tmp_path / "ip/fixture/configs/platform/testpdk/platform.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "\n[contracts]\n",
            '\nasset_scope = "external"\n\n[contracts]\n',
        ),
        encoding="utf-8",
    )

    project = Project.open(tmp_path)
    inventory = load_platforms(project, "fixture")
    platform = inventory["testpdk"]

    assert isinstance(platform, Platform)
    assert (
        resolve_platform_snapshot(
            project,
            "fixture",
            "testpdk",
            snapshot=inventory,
        )
        is platform
    )
    assert platform.asset_root_resource == "platform.fixture.testpdk"
    assert tuple(path.as_posix() for path in platform.asset_paths) == ("model.scs",)
    assert platform.simulation.default.file.logical == PurePosixPath("model.scs")
    assert not platform.simulation.default.file.bound


def test_asset_free_external_platform_is_not_runtime_bound(
    tmp_path: Path,
) -> None:
    write_project_context(tmp_path)
    write_test_platform(tmp_path)
    manifest = tmp_path / "ip/fixture/configs/platform/testpdk/platform.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        .replace('simulation = "simulation.toml"\n', "")
        .replace(
            "\n[contracts]\n",
            '\nasset_scope = "external"\n\n[contracts]\n',
        ),
        encoding="utf-8",
    )

    platform = load_platform(Project.open(tmp_path), "fixture", "testpdk")

    assert platform.assets == ()
    assert not platform.runtime_bound


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
    manifest = tmp_path / "ip/fixture/configs/platform/testpdk/platform.toml"
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
        directories={"platform.fixture.testpdk": str(package)}
    )

    with pytest.raises(ValueError, match="must not traverse a symlink"):
        load_platform(Project.open(tmp_path), "fixture", "testpdk", resources=resources)


def test_external_platform_requires_explicit_resource_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_project_context(tmp_path)
    write_test_platform(tmp_path)
    manifest = tmp_path / "ip/fixture/configs/platform/testpdk/platform.toml"
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

    with pytest.raises(ValueError, match="platform.fixture.testpdk"):
        load_platform(
            Project.open(tmp_path), "fixture", "testpdk", resources=Resources()
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
    manifest = tmp_path / "ip/fixture/configs/platform/testpdk/platform.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "\n[contracts]\n",
            '\nasset_scope = "external"\n\n[contracts]\n',
        ),
        encoding="utf-8",
    )
    resource = "platform.fixture.testpdk"
    resources = Resources(directories={resource: str(package)})
    project = Project.open(tmp_path)
    snapshot = load_platform(project, "fixture", "testpdk", resources=resources)

    monkeypatch.setenv("SIGILICON_PLATFORM_TESTPDK_ROOT", str(tmp_path / "ambient"))
    assert resolve_platform_snapshot(project, "fixture", "testpdk", snapshot=snapshot) is snapshot

def test_platform_asset_resources_preserve_distinct_catalog_keys(tmp_path: Path) -> None:
    write_project_context(tmp_path)
    write_test_platform(tmp_path, key="a-b")
    write_test_platform(tmp_path, key="a_b")
    catalog = tmp_path / "ip/fixture/configs/platform/catalog.toml"
    catalog.write_text(
        catalog.read_text(encoding="utf-8").replace(
            '[platforms]\na_b = "a_b/platform.toml"',
            '[platforms]\na-b = "a-b/platform.toml"\na_b = "a_b/platform.toml"',
        ),
        encoding="utf-8",
    )

    assert set(load_platform_catalog(Project.open(tmp_path), "fixture").manifests) == {"a-b", "a_b"}


def test_platform_contract_owner_matches_manifest(tmp_path: Path) -> None:
    write_project_context(tmp_path)
    write_test_platform(tmp_path)
    simulation = tmp_path / "ip/fixture/configs/platform/testpdk/simulation.toml"
    simulation.write_text(
        simulation.read_text(encoding="utf-8").replace(
            'owner = "fixture"', 'owner = "another-owner"'
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="owner must be 'fixture'"):
        load_platform(
            Project.open(tmp_path), "fixture", "testpdk", resources=Resources()
        )


def test_platform_manifest_owner_is_its_component_owner(tmp_path: Path) -> None:
    write_project_context(tmp_path)
    write_test_platform(tmp_path)
    manifest = tmp_path / "ip/fixture/configs/platform/testpdk/platform.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            'owner = "fixture"', 'owner = "another-owner"', 1
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="platform manifest owner"):
        load_platform(Project.open(tmp_path), "fixture", "testpdk")


@pytest.mark.parametrize("changed", ("catalog", "platform"))
def test_platform_set_rejects_source_drift(
    tmp_path: Path,
    changed: str,
) -> None:
    write_project_context(tmp_path)
    write_test_platform(tmp_path)
    project = Project.open(tmp_path)
    inventory = load_platforms(project, "fixture")
    path = (
        tmp_path / "ip/fixture/configs/platform/catalog.toml"
        if changed == "catalog"
        else tmp_path / "ip/fixture/configs/platform/testpdk/platform.toml"
    )
    source = path.read_text(encoding="utf-8")
    path.write_text(
        source.replace(
            'owner = "fixture"' if changed == "catalog" else 'name = "Test PDK"',
            'owner = "changed"' if changed == "catalog" else 'name = "Changed PDK"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match=(
            "project composition|source identity drift|identity drift|"
            "catalog snapshot|owner must be"
        ),
    ):
        resolve_platform_snapshot(project, "fixture", "testpdk", snapshot=inventory)
