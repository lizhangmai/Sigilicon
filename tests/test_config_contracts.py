from pathlib import Path
import tomllib
from typing import Any, Mapping

import pytest

from sigilicon.domain.config_contracts import (
    inspect_project_configuration_sources,
)
from sigilicon.contracts import (
    ContractReader,
    freeze_toml_document,
    require_config_header,
    require_relative_path,
)
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
        header.format(kind="ip-catalog").replace("schema = 1", "schema = 2")
        + '''
[components.alpha]
contract = "ip/alpha/component.toml"

[components.beta]
contract = "ip/beta/component.toml"

[components.compute]
contract = "ip/compute/component.toml"

[components.example]
contract = "ip/example/component.toml"
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
        '''schema = 5
contract_kind = "owner-operations"
path_scope = "owner"
owner = "example"

[operations.check]
uses = "fake.check"
filesets = [{ component = "example", fileset = "flow" }]
''',
    )
    for owner in ("alpha", "beta", "compute"):
        _write(
            root,
            f"ip/{owner}/component.toml",
            f'''schema = 5
contract_kind = "ip-component"
path_scope = "owner"
owner = "{owner}"
root = "ip/{owner}"
name = "{owner}"
kind = "rtl-ip"

[sources]
manifest = "ip/{owner}/component.toml"

[filesets]
source = ["manifest"]
''',
        )
    _write(
        root,
        "ip/example/component.toml",
        '''schema = 5
contract_kind = "ip-component"
path_scope = "owner"
owner = "example"
root = "ip/example"
name = "example"
kind = "rtl-ip"

operation_catalog = "operations"

[sources]
operations = "ip/example/configs/operations.toml"

[filesets]
flow = ["operations"]
''',
    )


def _inspect(
    project: Project,
    *,
    documents: Mapping[Path, Mapping[str, Any]] | None = None,
) -> dict[str, object]:
    sources = project.configuration_documents()
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


@pytest.mark.parametrize("owner_schema", (1, 2, 7))
def test_project_configuration_follows_context_owner_roots(
    tmp_path: Path, owner_schema: int,
) -> None:
    _write_selected_catalogs(tmp_path)
    _write(
        tmp_path,
        "ip/alpha/contract.toml",
        f"""schema = {owner_schema}
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


def test_project_document_store_is_reusable_and_closed(
    tmp_path: Path,
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
    inventory = context.configuration_documents()
    catalogs = {
        "example": (
            context.project_root / "ip/example/configs/operations.toml"
        ).resolve()
    }
    seeded_document = freeze_toml_document(
        tomllib.loads(seeded.read_text(encoding="utf-8"))
    )
    inventory.verify("seeded fixture", {seeded: seeded_document})

    with pytest.raises(ValueError, match="disagrees with captured source"):
        inventory.verify(
            "conflicting owner catalog snapshot",
            {catalogs["example"]: freeze_toml_document({"schema": 999})},
        )

    assert inventory.resolve(seeded) == seeded_document
    assert inventory.resolve(fallback)["contract_kind"] == "fallback-contract"
    with pytest.raises(AttributeError):
        inventory.documents = {}
    with pytest.raises(ValueError, match="must be frozen"):
        inventory.verify("mutable fixture", {seeded: {"schema": 1}})
    with pytest.raises(ValueError, match="disagrees with captured source"):
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
    with pytest.raises(ValueError, match="outside the captured store"):
        inventory.resolve(late)
    fallback.write_text("not valid TOML = [\n", encoding="utf-8")

    report = inspect_project_configuration_sources(
        context,
        operation_catalog_inventory=catalogs,
        sources=inventory,
    )

    assert report["passed"] is True
    assert "seeded-contract" in report["contract_kinds"]
    assert "fallback-contract" in report["contract_kinds"]
    assert "late-contract" not in report["contract_kinds"]
    assert inspect_project_configuration_sources(
        Project.open(tmp_path),
        operation_catalog_inventory=catalogs,
        sources=inventory,
    )["passed"] is True


def test_project_document_store_rejects_symlinked_directory(
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
        context.configuration_documents()


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


def test_contract_reader_consumes_strict_typed_fields() -> None:
    reader = ContractReader(
        {
            "name": "fixture",
            "enabled": True,
            "count": 2,
            "roles": ["rtl", "timing"],
            "settings": {"mode": "fast"},
        },
        "fixture",
    )

    assert reader.text("name") == "fixture"
    assert reader.boolean("enabled") is True
    assert reader.integer("count", minimum=1) == 2
    assert reader.strings("roles", nonempty=True) == ("rtl", "timing")
    assert reader.table("settings") == {"mode": "fast"}
    reader.finish()


def test_contract_reader_rejects_unconsumed_fields() -> None:
    reader = ContractReader({"name": "fixture", "typo": True}, "fixture")
    assert reader.text("name") == "fixture"

    with pytest.raises(ValueError, match="unknown fields.*typo"):
        reader.finish()


@pytest.mark.parametrize("value", ["../escape", "/absolute", "a\\b", "a/./b"])
def test_contract_relative_path_is_canonical(value: str) -> None:
    with pytest.raises(ValueError, match="safe relative path"):
        require_relative_path(value, "path")
