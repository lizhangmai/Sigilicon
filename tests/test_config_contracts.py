from dataclasses import replace
from pathlib import Path
import tomllib
from types import MappingProxyType, SimpleNamespace

import pytest

import sigilicon.domain.config_contracts as config_contracts
from conftest import write_project_context, write_test_platform
from sigilicon.domain.config_contracts import (
    RepositorySourceLedger,
    freeze_toml_document,
    inspect_project_configuration_sources,
    inspect_project_configurations,
    require_config_header,
)
from sigilicon.domain.platform import load_platform, load_platform_catalog
from sigilicon.domain.repository import OwnerCatalogSnapshot, RepositoryContext


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


def test_repository_source_ledger_is_operation_bound_and_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_selected_catalogs(tmp_path)
    seeded = (tmp_path / "ip/alpha/seeded.toml").resolve()
    fallback = (tmp_path / "ip/beta/fallback.toml").resolve()
    _write(
        tmp_path,
        seeded.relative_to(tmp_path).as_posix(),
        '''schema = 1
contract_kind = "seeded-contract"
path_scope = "owner"
owner = "alpha"
''',
    )
    _write(
        tmp_path,
        fallback.relative_to(tmp_path).as_posix(),
        '''schema = 1
contract_kind = "fallback-contract"
path_scope = "owner"
owner = "beta"
''',
    )
    context = RepositoryContext.from_project_root(tmp_path)
    catalogs = context.flow_catalog_inventory()
    seeded_document = freeze_toml_document(
        tomllib.loads(seeded.read_text(encoding="utf-8"))
    )
    ledger = RepositorySourceLedger.for_project(
        context,
        catalog_inventory=catalogs,
    ).merge("seeded fixture", {seeded: seeded_document})

    component = context.owners[0].component
    conflicting_catalog = OwnerCatalogSnapshot(
        owner=component.owner,
        path=component.path,
        contract_kind="flow-catalog",
        document=freeze_toml_document({"schema": 999}),
    )
    with pytest.raises(ValueError, match="disagrees with another source"):
        RepositorySourceLedger.for_project(
            context,
            catalog_inventory=(*catalogs, conflicting_catalog),
        )

    assert ledger.resolve(seeded) is seeded_document
    assert ledger.resolve(fallback) is None
    with pytest.raises(AttributeError, match="immutable"):
        ledger._documents = {}
    with pytest.raises(ValueError, match="must be frozen"):
        ledger.merge("mutable fixture", {seeded: {"schema": 1}})
    with pytest.raises(ValueError, match="disagrees with another source"):
        ledger.merge(
            "conflicting fixture",
            {seeded: freeze_toml_document({"schema": 2})},
        )

    reads: list[Path] = []
    original_read_toml = config_contracts.read_toml

    def counted_read_toml(path: Path):
        if path.resolve() in {seeded, fallback}:
            reads.append(path.resolve())
        return original_read_toml(path)

    monkeypatch.setattr(config_contracts, "read_toml", counted_read_toml)
    report = inspect_project_configuration_sources(
        context,
        owner_roots=_owner_roots(tmp_path),
        catalog_inventory=catalogs,
        sources=ledger,
    )

    assert report["passed"] is True
    assert "seeded-contract" in report["contract_kinds"]
    assert "fallback-contract" in report["contract_kinds"]
    assert seeded not in reads
    assert fallback in reads
    with pytest.raises(ValueError, match="another operation"):
        inspect_project_configuration_sources(
            RepositoryContext.from_project_root(tmp_path),
            owner_roots=_owner_roots(tmp_path),
            catalog_inventory=catalogs,
            sources=ledger,
        )


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


def test_legacy_configuration_inventory_conflicts_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_selected_catalogs(tmp_path)
    ip_catalog_path = tmp_path / "catalogs/ip.toml"
    ip_catalog_path.write_text(
        ip_catalog_path.read_text(encoding="utf-8")
        + '''
[targets.collision]
contract = "ip/alpha/component.toml"
''',
        encoding="utf-8",
    )
    context = RepositoryContext.from_project_root(tmp_path)
    component = context.owners[0].component
    conflicting_document = freeze_toml_document({"schema": 999})
    release = SimpleNamespace(
        project=context,
        name="collision",
        path=component.path,
        document=conflicting_document,
    )

    with pytest.raises(ValueError, match="IP release contract snapshot disagrees"):
        inspect_project_configurations(
            context,
            owner_roots=_owner_roots(tmp_path),
            release_inventory={"collision": release},
        )

    platform_catalog = load_platform_catalog(context)
    resolved_conflict = replace(
        platform_catalog,
        path=component.path,
        document=conflicting_document,
    )
    monkeypatch.setattr(
        "sigilicon.domain.platform.resolve_platform_catalog",
        lambda _context, *, snapshot: resolved_conflict,
    )
    with pytest.raises(ValueError, match="platform catalog snapshot disagrees"):
        inspect_project_configurations(
            context,
            owner_roots=_owner_roots(tmp_path),
            platform_catalog=platform_catalog,
        )


def test_project_configuration_reuses_validated_platform_source_documents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_project_context(tmp_path)
    write_test_platform(tmp_path)
    context = RepositoryContext.from_project_root(tmp_path)
    catalog = load_platform_catalog(context)
    platform = load_platform(context, "testpdk", catalog=catalog)
    _write(tmp_path, "configs/platform/testpdk/native.toml", "schema = 3\n")
    platform_reads: list[Path] = []
    original_read_toml = config_contracts.read_toml

    def counted_read_toml(path: Path):
        if path.resolve() in platform.source_documents:
            platform_reads.append(path.resolve())
        return original_read_toml(path)

    monkeypatch.setattr(config_contracts, "read_toml", counted_read_toml)

    report = inspect_project_configurations(
        context,
        owner_roots={"test-platform": platform.path.parent},
        platform_catalog=catalog,
        platform_inventory={"testpdk": platform},
    )

    assert report["passed"] is True
    assert report["native_documents"] == 1
    assert platform_reads == []

    with pytest.raises(ValueError, match="requires its platform catalog"):
        inspect_project_configurations(
            context,
            owner_roots={"test-platform": platform.path.parent},
            platform_inventory={"testpdk": platform},
        )

    with pytest.raises(ValueError, match="manifest disagrees with its catalog"):
        inspect_project_configurations(
            context,
            owner_roots={"test-platform": platform.path.parent},
            platform_catalog=catalog,
            platform_inventory={
                "testpdk": replace(platform, path=platform.simulation.path)
            },
        )


def test_project_configuration_reuses_oa_simulation_source_documents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_selected_catalogs(tmp_path)
    simulation_path = (
        tmp_path / "ip/alpha/verification/tb_fixture/simulation.toml"
    )
    rdb_path = simulation_path.with_name("native_rdb.toml")
    setup_path = simulation_path.with_name("setup.il")
    non_oa_simulation = (
        tmp_path / "ip/beta/verification/hspice/simulation.toml"
    )
    _write(
        tmp_path,
        simulation_path.relative_to(tmp_path).as_posix(),
        "schema = 3\n",
    )
    _write(
        tmp_path,
        rdb_path.relative_to(tmp_path).as_posix(),
        "schema = 2\n",
    )
    _write(
        tmp_path,
        setup_path.relative_to(tmp_path).as_posix(),
        "procedure(fixtureConfig() t)\nprocedure(fixtureMaestro() t)\n",
    )
    _write(
        tmp_path,
        non_oa_simulation.relative_to(tmp_path).as_posix(),
        "schema = 3\n",
    )
    context = RepositoryContext.from_project_root(tmp_path)
    simulation_document = freeze_toml_document(
        {
            "schema": 3,
            "testbench": {
                "library": "fixture",
                "cell": "tb_fixture",
                "dut": "dut",
                "source_view": "schematic",
                "simulator": "spectre",
            },
            "platform": {"pdk": "testpdk"},
            "setup": {
                "source": "setup.il",
                "config_procedure": "fixtureConfig",
                "maestro_procedure": "fixtureMaestro",
            },
        }
    )
    rdb_document = MappingProxyType({"schema": 2})
    rdb_contract = SimpleNamespace(
        path=rdb_path.resolve(),
        source_document=rdb_document,
    )
    simulation = SimpleNamespace(
        path=simulation_path.resolve(),
        project=context,
        library="fixture",
        cell="tb_fixture",
        dut="dut",
        top_view="schematic",
        simulator="spectre",
        contract_schema=3,
        native_setup=SimpleNamespace(
            pdk=SimpleNamespace(key="testpdk"),
            source=setup_path.resolve(),
            config_procedure="fixtureConfig",
            maestro_procedure="fixtureMaestro",
            rdb_contract=rdb_contract,
        ),
        source_documents=MappingProxyType(
            {
                simulation_path.resolve(): simulation_document,
                rdb_path.resolve(): rdb_document,
            }
        ),
    )
    reads: list[Path] = []
    non_oa_reads = 0
    original_read_toml = config_contracts.read_toml

    def counted_read_toml(path: Path):
        nonlocal non_oa_reads
        if path.resolve() in simulation.source_documents:
            reads.append(path.resolve())
        if path.resolve() == non_oa_simulation.resolve():
            non_oa_reads += 1
        return original_read_toml(path)

    monkeypatch.setattr(config_contracts, "read_toml", counted_read_toml)

    report = inspect_project_configurations(
        context,
        owner_roots=_owner_roots(tmp_path),
        oa_simulation_inventory={simulation_path.resolve(): simulation},
    )

    assert report["passed"] is True
    assert report["native_documents"] == 3
    assert reads == []
    assert non_oa_reads == 1

    with pytest.raises(ValueError, match="snapshot identity drift"):
        inspect_project_configurations(
            context,
            owner_roots=_owner_roots(tmp_path),
            oa_simulation_inventory={rdb_path.resolve(): simulation},
        )

    component_path = (tmp_path / "ip/alpha/component.toml").resolve()
    conflicting_document = freeze_toml_document(
        {
            **simulation_document,
            "setup": {
                **simulation_document["setup"],
                "source": "verification/tb_fixture/setup.il",
            },
        }
    )
    conflicting_simulation = SimpleNamespace(
        **{
            **simulation.__dict__,
            "path": component_path,
            "native_setup": SimpleNamespace(
                **{
                    **simulation.native_setup.__dict__,
                    "rdb_contract": None,
                }
            ),
            "source_documents": {component_path: conflicting_document},
        }
    )
    with pytest.raises(
        ValueError,
        match="OA simulation snapshot disagrees with another source",
    ):
        inspect_project_configurations(
            context,
            owner_roots=_owner_roots(tmp_path),
            oa_simulation_inventory={component_path: conflicting_simulation},
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


def test_project_configuration_reuses_scanned_verification_cell_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_selected_catalogs(tmp_path)
    cell = tmp_path / "ip/alpha/verification/example/cell.toml"
    _write(
        tmp_path,
        "ip/alpha/verification/example/testbench.sv",
        "module tb; endmodule\n",
    )
    declared_contracts = (
        tmp_path / "ip/alpha/qualification.toml",
        tmp_path / "ip/alpha/verification/z_contract.toml",
    )
    for declared in declared_contracts:
        _write(
            tmp_path,
            declared.relative_to(tmp_path).as_posix(),
            '''schema = 1
contract_kind = "test-contract"
path_scope = "owner"
owner = "alpha"
''',
        )
    _write(
        tmp_path,
        "ip/alpha/verification/example/cell.toml",
        """schema = 1
contract_kind = "verification-cell"
path_scope = "cell"
owner = "alpha"

cell = "example"
role = "rtl-testbench"
canonical_source = "testbench.sv"
dut = "dut"
simulator = "xcelium"
contracts = ["../../qualification.toml", "../z_contract.toml"]
""",
    )
    original_load = tomllib.load
    reads = {path.resolve(): 0 for path in (cell, *declared_contracts)}

    def counted_load(stream):
        path = Path(stream.name).resolve()
        if path in reads:
            reads[path] += 1
        return original_load(stream)

    monkeypatch.setattr(tomllib, "load", counted_load)

    context = RepositoryContext.from_project_root(tmp_path)
    inspect_project_configurations(
        context,
        owner_roots=_owner_roots(tmp_path),
    )

    assert set(reads.values()) == {1}


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
