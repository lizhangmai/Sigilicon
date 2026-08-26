from __future__ import annotations

from pathlib import Path

import pytest

from conftest import (
    write_project_context,
    write_test_layout_platform,
    write_test_platform,
)
from sigilicon.domain.platform import load_platform
from sigilicon.domain.repository import RepositoryContext


def test_load_platform_resolves_typed_capabilities_from_the_project_catalog(
    tmp_path: Path,
) -> None:
    write_project_context(tmp_path)
    model = write_test_platform(tmp_path)

    platform = load_platform(RepositoryContext.from_project_root(tmp_path), "testpdk")

    assert platform.path == tmp_path / "configs/platform/testpdk/platform.toml"
    assert platform.simulation.default.file == model
    assert platform.simulation.default.single_section == "tt"
    assert platform.oa.technology_library == "techLib"
    assert platform.oa.reference_libraries == ("deviceLib",)
    assert platform.layout is None
    assert platform.source_paths == (
        tmp_path / "configs/platform/catalog.toml",
        platform.path,
        tmp_path / "configs/platform/testpdk/simulation.toml",
        tmp_path / "configs/platform/testpdk/oa.toml",
    )


def test_platform_contracts_reject_unknown_fields(tmp_path: Path) -> None:
    write_project_context(tmp_path)
    write_test_platform(tmp_path)
    simulation = tmp_path / "configs/platform/testpdk/simulation.toml"
    simulation.write_text(
        simulation.read_text(encoding="utf-8") + "\nmodel_sects = [\"tt\"]\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsupported fields.*model_sects"):
        load_platform(RepositoryContext.from_project_root(tmp_path), "testpdk")


def test_platform_oa_rejects_owner_primitive_selection(tmp_path: Path) -> None:
    write_project_context(tmp_path)
    write_test_platform(tmp_path)
    oa = tmp_path / "configs/platform/testpdk/oa.toml"
    oa.write_text(
        oa.read_text(encoding="utf-8") + '\nprimitive_masters = ["nch"]\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsupported fields.*primitive_masters"):
        load_platform(RepositoryContext.from_project_root(tmp_path), "testpdk")


def test_layout_platform_can_omit_optional_qrc_capability(tmp_path: Path) -> None:
    write_project_context(tmp_path)
    write_test_layout_platform(tmp_path)
    verification = tmp_path / "configs/platform/testpdk/verification.toml"
    verification.write_text(
        verification.read_text(encoding="utf-8").replace(
            'qrc_tech_file = "qrc.tech"\n', ""
        ),
        encoding="utf-8",
    )

    platform = load_platform(RepositoryContext.from_project_root(tmp_path), "testpdk")

    assert platform.layout is not None
    assert platform.layout.qrc_tech_file is None
    assert platform.layout.drc_deck.name == "drc.deck"
    assert platform.layout.lvs_deck.name == "lvs.deck"


def test_platform_layout_rejects_owner_specific_technology_roles(tmp_path: Path) -> None:
    write_project_context(tmp_path)
    write_test_layout_platform(tmp_path)
    layout = tmp_path / "configs/platform/testpdk/layout.toml"
    layout.write_text(
        layout.read_text(encoding="utf-8")
        + '\n[technology]\nprofile = "geometry.toml"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsupported fields.*technology"):
        load_platform(RepositoryContext.from_project_root(tmp_path), "testpdk")


def test_verification_contract_rejects_owner_drc_policy(tmp_path: Path) -> None:
    write_project_context(tmp_path)
    write_test_layout_platform(tmp_path)
    verification = tmp_path / "configs/platform/testpdk/verification.toml"
    verification.write_text(
        verification.read_text(encoding="utf-8")
        + "\n[drc_profile]\nwaiver_layers = []\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsupported fields.*drc_profile"):
        load_platform(RepositoryContext.from_project_root(tmp_path), "testpdk")


def test_platform_lookup_does_not_assume_a_named_pdk_file(tmp_path: Path) -> None:
    write_project_context(tmp_path)
    write_test_platform(tmp_path, key="custom")
    catalog = tmp_path / "configs/platform/catalog.toml"
    catalog.write_text(
        catalog.read_text(encoding="utf-8").replace(
            'custom/platform.toml', 'custom/platform-contract.toml'
        ),
        encoding="utf-8",
    )
    manifest = tmp_path / "configs/platform/custom/platform.toml"
    manifest.rename(manifest.with_name("platform-contract.toml"))

    platform = load_platform(RepositoryContext.from_project_root(tmp_path), "custom")

    assert platform.path.name == "platform-contract.toml"


def test_platform_contract_owners_must_match_the_manifest(tmp_path: Path) -> None:
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
        load_platform(RepositoryContext.from_project_root(tmp_path), "testpdk")
