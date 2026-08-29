from dataclasses import replace
from pathlib import Path

import pytest

from sigilicon.domain.config_contracts import (
    inspect_project_configurations,
    require_config_header,
)
from sigilicon.domain.platform import load_platform_catalog
from sigilicon.domain.repository import RepositoryContext


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_selected_catalogs(root: Path) -> None:
    header = """schema = 1
contract_kind = "{kind}"
path_scope = "repository"
owner = "test"
"""
    _write(
        root,
        "catalogs/ip.toml",
        header.format(kind="ip-catalog")
        + '''
[targets]

[components.alpha]
contract = "ip/alpha/component.toml"
root = "ip/alpha"

[components.beta]
contract = "ip/beta/component.toml"
root = "ip/beta"

[components.compute]
contract = "ip/compute/component.toml"
root = "ip/compute"

[components.example]
contract = "ip/example/component.toml"
root = "ip/example"
''',
    )
    _write(
        root,
        "configs/platform/catalog.toml",
        header.format(kind="platform-catalog") + "\n[platforms]\n",
    )
    _write(
        root,
        "ip/example/configs/flows/design_targets.toml",
        header.format(kind="flow-design-registry")
        .replace('path_scope = "repository"', 'path_scope = "owner"')
        + "\n[targets]\n",
    )
    _write(
        root,
        "ip/example/configs/flows/layout_targets.toml",
        header.format(kind="flow-layout-registry")
        .replace('path_scope = "repository"', 'path_scope = "owner"')
        + "\n[targets]\n",
    )
    for owner in ("alpha", "beta", "compute"):
        _write(
            root,
            f"ip/{owner}/component.toml",
            f'''schema = 1
contract_kind = "ip-component"
path_scope = "owner"
owner = "{owner}"
name = "{owner}"
kind = "rtl-ip"

[filesets]
source = ["ip/{owner}/component.toml"]
''',
        )
    _write(
        root,
        "ip/example/component.toml",
        '''schema = 1
contract_kind = "ip-component"
path_scope = "owner"
owner = "test"
name = "example"
kind = "rtl-ip"

[filesets]
flow = [
  "ip/example/configs/flows/design_targets.toml",
  "ip/example/configs/flows/layout_targets.toml",
]
''',
    )


def _owner_roots(root: Path) -> dict[str, Path]:
    return {
        "alpha": root / "ip/alpha",
        "beta": root / "ip/beta",
        "compute": root / "ip/compute",
        "test": root / "ip/example",
    }


def test_project_configuration_follows_context_owner_roots(
    tmp_path: Path,
) -> None:
    _write_selected_catalogs(tmp_path)
    _write(
        tmp_path,
        "ip/alpha/contract.toml",
        """schema = 1
contract_kind = "test-contract"
path_scope = "owner"
owner = "alpha"
""",
    )
    _write(tmp_path, "ip/beta/native.toml", "schema = 3\n")

    report = inspect_project_configurations(
        RepositoryContext.from_project_root(tmp_path),
        owner_roots=_owner_roots(tmp_path),
    )

    assert report["passed"] is True
    assert report["contracts"] == report["documents"] - 1
    assert report["native_documents"] == 1
    assert "test-contract" in report["contract_kinds"]
    assert "ip/alpha" in report["roots"]


def test_project_configuration_rejects_platform_catalog_snapshot_drift(
    tmp_path: Path,
) -> None:
    _write_selected_catalogs(tmp_path)
    context = RepositoryContext.from_project_root(tmp_path)
    catalog = load_platform_catalog(context)

    with pytest.raises(ValueError, match="snapshot identity drift"):
        inspect_project_configurations(
            context,
            owner_roots=_owner_roots(tmp_path),
            platform_catalog=replace(catalog, owner="drift"),
        )


def test_project_configuration_rejects_partial_common_header(tmp_path: Path) -> None:
    _write_selected_catalogs(tmp_path)
    _write(tmp_path, "ip/alpha/broken.toml", 'contract_kind = "broken"\n')

    with pytest.raises(ValueError, match="incomplete configuration header"):
        inspect_project_configurations(
            RepositoryContext.from_project_root(tmp_path),
            owner_roots=_owner_roots(tmp_path),
        )


def test_project_configuration_rejects_owner_outside_its_root(tmp_path: Path) -> None:
    _write_selected_catalogs(tmp_path)
    _write(
        tmp_path,
        "ip/alpha/wrong-owner.toml",
        """schema = 1
contract_kind = "test-contract"
path_scope = "owner"
owner = "beta"
""",
    )

    with pytest.raises(ValueError, match="owner must be 'alpha'"):
        inspect_project_configurations(
            RepositoryContext.from_project_root(tmp_path),
            owner_roots=_owner_roots(tmp_path),
        )


def test_project_configuration_dispatches_verification_cells(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_selected_catalogs(tmp_path)
    cell = tmp_path / "ip/alpha/verification/example/cell.toml"
    _write(
        tmp_path,
        "ip/alpha/verification/example/cell.toml",
        """schema = 1
contract_kind = "verification-cell"
path_scope = "cell"
owner = "alpha"
""",
    )
    loaded: list[tuple[Path, RepositoryContext]] = []
    monkeypatch.setattr(
        "sigilicon.domain.verification_cell.load_verification_cell",
        lambda path, *, project: loaded.append((path, project)),
    )

    context = RepositoryContext.from_project_root(tmp_path)
    inspect_project_configurations(
        context,
        owner_roots=_owner_roots(tmp_path),
    )

    assert loaded == [(cell, context)]


def test_common_header_rejects_wrong_scope() -> None:
    with pytest.raises(ValueError, match="path_scope"):
        require_config_header(
            {
                "schema": 1,
                "contract_kind": "test-contract",
                "path_scope": "cell",
                "owner": "test",
            },
            Path("fixture.toml"),
            contract_kind="test-contract",
            path_scope="owner",
            owner="test",
        )
