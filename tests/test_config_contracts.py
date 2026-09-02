from pathlib import Path
import tomllib
from typing import Any, Mapping

import pytest

import sigilicon.domain.config_contracts as config_contracts
from sigilicon.domain.config_contracts import (
    RepositorySourceInventory,
    inspect_project_configuration_sources,
)
from sigilicon.contracts import freeze_toml_document, require_config_header
from sigilicon.project import Project


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
        "ip/example/configs/operations.toml",
        '''schema = 2
contract_kind = "owner-operations"
path_scope = "owner"
owner = "example"

[operations.check]
uses = "fake.check"
filesets = ["flow"]
''',
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
owner = "example"
name = "example"
kind = "rtl-ip"

operation_catalog = "ip/example/configs/operations.toml"

[filesets]
flow = [
  "ip/example/configs/operations.toml",
]
''',
    )


def _inspect(
    project: Project,
    *,
    documents: Mapping[Path, Mapping[str, Any]] | None = None,
) -> dict[str, object]:
    sources = RepositorySourceInventory.for_project(project)
    catalogs = {
        owner.name: project.project_root.joinpath(
            *owner.component.operation_catalog.parts
        ).resolve()
        for owner in project.owners
        if owner.component.operation_catalog is not None
    }
    if documents:
        sources.verify("test source snapshot", documents)
    return inspect_project_configuration_sources(
        project,
        operation_catalog_inventory=catalogs,
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

    report = _inspect(Project.open(tmp_path))

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
    context = Project.open(tmp_path)
    inventory = RepositorySourceInventory.for_project(context)
    catalogs = {
        "example": (
            context.project_root / "ip/example/configs/operations.toml"
        ).resolve()
    }
    seeded_document = freeze_toml_document(
        tomllib.loads(seeded.read_text(encoding="utf-8"))
    )
    inventory.verify("seeded fixture", {seeded: seeded_document})

    with pytest.raises(ValueError, match="disagrees with another source"):
        inventory.verify(
            "conflicting owner catalog snapshot",
            {catalogs["example"]: freeze_toml_document({"schema": 999})},
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
    original_read_source = config_contracts.read_nofollow_text

    def counted_read_source(path: Path):
        if path.resolve() in {seeded, fallback}:
            reads.append(path.resolve())
        return original_read_source(path)

    monkeypatch.setattr(config_contracts, "read_nofollow_text", counted_read_source)
    report = inspect_project_configuration_sources(
        context,
        operation_catalog_inventory=catalogs,
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
            Project.open(tmp_path),
            operation_catalog_inventory=catalogs,
            sources=inventory,
        )


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
    context = Project.open(tmp_path)

    with pytest.raises(ValueError, match="is a symlink"):
        RepositorySourceInventory.for_project(context)


def test_project_configuration_rejects_partial_common_header(tmp_path: Path) -> None:
    _write_selected_catalogs(tmp_path)
    _write(tmp_path, "ip/alpha/broken.toml", 'contract_kind = "broken"\n')

    with pytest.raises(ValueError, match="incomplete configuration header"):
        _inspect(
            Project.open(tmp_path),
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
            Project.open(tmp_path),
        )


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
