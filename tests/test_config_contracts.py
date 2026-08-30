from pathlib import Path
import tomllib
from typing import Any, Mapping

import pytest

import sigilicon.domain.config_contracts as config_contracts
from conftest import write_project_context, write_test_platform
from sigilicon.domain.config_contracts import (
    RepositorySourceInventory,
    freeze_toml_document,
    inspect_project_configuration_sources,
    require_config_header,
)
from sigilicon.domain.platform import load_platform, load_platform_catalog
from sigilicon.domain.repository import OwnerCatalogSnapshot, Project


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


def _inspect(
    project: Project,
    *,
    documents: Mapping[Path, Mapping[str, Any]] | None = None,
) -> dict[str, object]:
    sources = RepositorySourceInventory.for_project(project)
    catalogs = project.flow_catalog_inventory()
    if documents:
        sources.verify("test source snapshot", documents)
    return inspect_project_configuration_sources(
        project,
        catalog_inventory=catalogs,
        sources=sources,
    )


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

    report = _inspect(Project.from_project_root(tmp_path))

    assert report["passed"] is True
    assert report["contracts"] == report["documents"] - 1
    assert report["native_documents"] == 1
    assert "test-contract" in report["contract_kinds"]
    assert "ip/alpha" in report["roots"]


def test_repository_source_inventory_is_operation_bound_and_closed(
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
    context = Project.from_project_root(tmp_path)
    inventory = RepositorySourceInventory.for_project(context)
    catalogs = context.flow_catalog_inventory()
    seeded_document = freeze_toml_document(
        tomllib.loads(seeded.read_text(encoding="utf-8"))
    )
    inventory.verify("seeded fixture", {seeded: seeded_document})

    component = context.owners[0].component
    conflicting_catalog = OwnerCatalogSnapshot(
        owner=component.owner,
        path=component.path,
        contract_kind="flow-catalog",
        record_text="schema = 999\n",
        document=freeze_toml_document({"schema": 999}),
    )
    with pytest.raises(ValueError, match="disagrees with another source"):
        inventory.verify(
            "conflicting owner catalog snapshot",
            {conflicting_catalog.path: conflicting_catalog.document},
        )

    assert inventory.resolve(seeded) == seeded_document
    assert inventory.resolve(fallback)["contract_kind"] == "fallback-contract"
    with pytest.raises(AttributeError, match="immutable"):
        inventory._documents = {}
    with pytest.raises(ValueError, match="must be frozen"):
        inventory.verify("mutable fixture", {seeded: {"schema": 1}})
    with pytest.raises(ValueError, match="disagrees with another source"):
        inventory.verify(
            "conflicting fixture",
            {seeded: freeze_toml_document({"schema": 2})},
        )

    late = (tmp_path / "ip/alpha/late.toml").resolve()
    _write(
        tmp_path,
        late.relative_to(tmp_path).as_posix(),
        '''schema = 1
contract_kind = "late-contract"
path_scope = "owner"
owner = "alpha"
''',
    )
    with pytest.raises(ValueError, match="outside the captured inventory"):
        inventory.resolve(late)
    fallback.write_text("not valid TOML = [\n", encoding="utf-8")

    reads: list[Path] = []
    original_read_toml = config_contracts.read_toml

    def counted_read_toml(path: Path):
        if path.resolve() in {seeded, fallback}:
            reads.append(path.resolve())
        return original_read_toml(path)

    monkeypatch.setattr(config_contracts, "read_toml", counted_read_toml)
    report = inspect_project_configuration_sources(
        context,
        catalog_inventory=catalogs,
        sources=inventory,
    )

    assert report["passed"] is True
    assert "seeded-contract" in report["contract_kinds"]
    assert "fallback-contract" in report["contract_kinds"]
    assert "late-contract" not in report["contract_kinds"]
    assert seeded not in reads
    assert fallback not in reads
    with pytest.raises(ValueError, match="another operation"):
        inspect_project_configuration_sources(
            Project.from_project_root(tmp_path),
            catalog_inventory=catalogs,
            sources=inventory,
        )


def test_project_configuration_reuses_validated_platform_source_documents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_project_context(tmp_path)
    write_test_platform(tmp_path)
    context = Project.from_project_root(tmp_path)
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

    report = _inspect(
        context,
        documents={catalog.path: catalog.document, **platform.source_documents},
    )

    assert report["passed"] is True
    assert report["native_documents"] == 1
    assert platform_reads == []


def test_repository_source_inventory_rejects_symlinked_toml(
    tmp_path: Path,
) -> None:
    _write_selected_catalogs(tmp_path)
    target = tmp_path / "unowned.toml"
    target.write_text("schema = 3\n", encoding="utf-8")
    link = tmp_path / "ip/alpha/linked.toml"
    link.symlink_to(target)
    context = Project.from_project_root(tmp_path)

    with pytest.raises(ValueError, match="is a symlink"):
        RepositorySourceInventory.for_project(context)


def test_repository_source_inventory_rejects_symlinked_directory(
    tmp_path: Path,
) -> None:
    _write_selected_catalogs(tmp_path)
    target = tmp_path / "unowned-configs"
    target.mkdir()
    (target / "hidden.toml").write_text("schema = 3\n", encoding="utf-8")
    (tmp_path / "ip/alpha/linked-configs").symlink_to(
        target,
        target_is_directory=True,
    )
    context = Project.from_project_root(tmp_path)

    with pytest.raises(ValueError, match="is a symlink"):
        RepositorySourceInventory.for_project(context)


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
        '''schema = 3

[testbench]
library = "fixture"
cell = "tb_fixture"
dut = "dut"
source_view = "schematic"
simulator = "spectre"

[platform]
pdk = "testpdk"

[setup]
source = "setup.il"
config_procedure = "fixtureConfig"
maestro_procedure = "fixtureMaestro"
''',
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
    context = Project.from_project_root(tmp_path)
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
    rdb_document = freeze_toml_document({"schema": 2})
    simulation_documents = {
        simulation_path.resolve(): simulation_document,
        rdb_path.resolve(): rdb_document,
    }
    reads: list[Path] = []
    non_oa_reads = 0
    original_read_toml = config_contracts.read_toml

    def counted_read_toml(path: Path):
        nonlocal non_oa_reads
        if path.resolve() in simulation_documents:
            reads.append(path.resolve())
        if path.resolve() == non_oa_simulation.resolve():
            non_oa_reads += 1
        return original_read_toml(path)

    monkeypatch.setattr(config_contracts, "read_toml", counted_read_toml)

    report = _inspect(
        context,
        documents=simulation_documents,
    )

    assert report["passed"] is True
    assert report["native_documents"] == 3
    assert reads == []
    assert non_oa_reads == 0

def test_project_configuration_rejects_partial_common_header(tmp_path: Path) -> None:
    _write_selected_catalogs(tmp_path)
    _write(tmp_path, "ip/alpha/broken.toml", 'contract_kind = "broken"\n')

    with pytest.raises(ValueError, match="incomplete configuration header"):
        _inspect(
            Project.from_project_root(tmp_path),
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
        _inspect(
            Project.from_project_root(tmp_path),
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
    original_read = config_contracts.read_nofollow_text
    reads = {path.resolve(): 0 for path in (cell, *declared_contracts)}

    def counted_read(path: Path):
        path = path.resolve()
        if path in reads:
            reads[path] += 1
        return original_read(path)

    monkeypatch.setattr(config_contracts, "read_nofollow_text", counted_read)

    context = Project.from_project_root(tmp_path)
    _inspect(context)

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
