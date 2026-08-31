from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

from sigilicon.domain.repository import Project
from sigilicon.domain.targets import (
    OwnerTargetCatalog,
    OwnerTarget,
    OwnerOperation,
    load_owner_target_catalog,
)

from conftest import write_component_owner, write_project_context


def _write_owner_project(
    root: Path,
    *,
    catalog_value: str = "ip/example/configs/targets.toml",
    recipe_value: str = "configs/recipe.toml",
    flow_files: tuple[str, ...] | None = None,
    catalog_text: str | None = None,
    recipe_text: str | None = None,
) -> Project:
    write_project_context(root)
    owner_root = root / "ip/example"
    recipe = owner_root / Path(recipe_value)
    recipe.parent.mkdir(parents=True, exist_ok=True)
    recipe.write_text(
        recipe_text
        or """schema = 1
contract_kind = "execution-recipe"
path_scope = "owner"
owner = "example"
""",
        encoding="utf-8",
    )
    catalog = root / Path(catalog_value)
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(
        catalog_text
        or """schema = 1
contract_kind = "owner-targets"
path_scope = "owner"
owner = "example"

[targets.adder]
description = "Adder target"

[targets.adder.operations.check]
recipe = "configs/recipe.toml"
goals = ["source-check", "recipe-check"]
""",
        encoding="utf-8",
    )
    files = flow_files or (f"ip/example/{recipe_value}",)
    component = write_component_owner(root, "example", filesets={"flow": files})
    component.write_text(
        component.read_text(encoding="utf-8").replace(
            "\n[filesets]\n",
            f'\ntarget_catalog = "{catalog_value}"\n\n[filesets]\n',
        ),
        encoding="utf-8",
    )
    return Project.from_project_root(root)


def test_component_target_catalog_is_optional(tmp_path: Path) -> None:
    write_project_context(tmp_path)
    component = write_component_owner(tmp_path, "example", filesets={})

    project = Project.from_project_root(tmp_path)

    assert project.owner("example").component.target_catalog is None
    assert "target_catalog" not in project.owner("example").component.document
    assert component.is_file()


def test_owner_target_catalog_loads_typed_targets_and_operations(
    tmp_path: Path,
) -> None:
    project = _write_owner_project(tmp_path)

    snapshot = project.owner_target_catalog("example")
    catalog = load_owner_target_catalog(project, "example")

    assert snapshot.contract_kind == "owner-targets"
    assert snapshot.owner == "example"
    assert catalog == load_owner_target_catalog(
        project,
        project.owner("example"),
        catalog_snapshot=snapshot,
    )
    assert isinstance(catalog, OwnerTargetCatalog)
    target = catalog.get("adder")
    assert isinstance(target, OwnerTarget)
    operation = target.operation("check")
    assert isinstance(operation, OwnerOperation)
    assert operation.recipe == PurePosixPath("configs/recipe.toml")
    assert operation.goals == ("source-check", "recipe-check")


def test_target_inputs_are_frozen_and_every_operation_binds_its_recipe(
    tmp_path: Path,
) -> None:
    project = _write_owner_project(
        tmp_path,
        catalog_text="""schema = 1
contract_kind = "owner-targets"
path_scope = "owner"
owner = "example"

[targets.adder]
description = "Adder target"
inputs = { variant = "paper", limits = { low = 1, high = 3 } }

[targets.adder.operations.check]
recipe = "configs/recipe.toml"
goals = ["check"]
""",
    )

    target = load_owner_target_catalog(project, "example").get("adder")
    operation = target.operation("check")

    assert operation.recipe == PurePosixPath("configs/recipe.toml")
    assert target.inputs == {
        "variant": "paper",
        "limits": {"low": 1, "high": 3},
    }
    with pytest.raises(TypeError):
        target.inputs["variant"] = "product"  # type: ignore[index]


def test_target_level_recipe_and_implicit_operation_recipe_are_rejected(
    tmp_path: Path,
) -> None:
    project = _write_owner_project(
        tmp_path,
        catalog_text="""schema = 1
contract_kind = "owner-targets"
path_scope = "owner"
owner = "example"

[targets.adder]
description = "Adder target"
recipe = "configs/recipe.toml"

[targets.adder.operations.check]
goals = ["check"]
""",
    )

    with pytest.raises(ValueError, match="targets.adder contains unknown fields"):
        load_owner_target_catalog(project, "example")

    catalog = tmp_path / "ip/example/configs/targets.toml"
    catalog.write_text(
        catalog.read_text(encoding="utf-8").replace(
            'recipe = "configs/recipe.toml"\n\n',
            "",
        ),
        encoding="utf-8",
    )
    project = Project.from_project_root(tmp_path)
    with pytest.raises(ValueError, match=r"operations\.check\.recipe is required"):
        load_owner_target_catalog(project, "example")


def test_target_catalog_rejects_unknown_fields_at_each_level(
    tmp_path: Path,
) -> None:
    project = _write_owner_project(
        tmp_path,
        catalog_text="""schema = 1
contract_kind = "owner-targets"
path_scope = "owner"
owner = "example"
unexpected = true

[targets.adder]
description = "Adder target"
operations = {}
""",
    )

    with pytest.raises(ValueError, match="unknown fields"):
        load_owner_target_catalog(project, "example")

    project = _write_owner_project(
        tmp_path / "target",
        catalog_text="""schema = 1
contract_kind = "owner-targets"
path_scope = "owner"
owner = "example"

[targets.adder]
description = "Adder target"
unexpected = true

[targets.adder.operations.check]
recipe = "configs/recipe.toml"
goals = ["check"]
""",
    )
    with pytest.raises(ValueError, match="targets.adder contains unknown fields"):
        load_owner_target_catalog(project, "example")

    project = _write_owner_project(
        tmp_path / "operation",
        catalog_text="""schema = 1
contract_kind = "owner-targets"
path_scope = "owner"
owner = "example"

[targets.adder]
description = "Adder target"

[targets.adder.operations.check]
recipe = "configs/recipe.toml"
goals = ["check"]
unexpected = true
""",
    )
    with pytest.raises(
        ValueError,
        match="targets.adder.operations.check contains unknown fields",
    ):
        load_owner_target_catalog(project, "example")


@pytest.mark.parametrize(
    ("field", "catalog_text"),
    (
        (
            "header",
            """schema = 1
contract_kind = "flow-catalog"
path_scope = "owner"
owner = "example"

[targets.adder]
description = "Adder target"

[targets.adder.operations.check]
recipe = "configs/recipe.toml"
goals = ["check"]
""",
        ),
        (
            "goals",
            """schema = 1
contract_kind = "owner-targets"
path_scope = "owner"
owner = "example"

[targets.adder]
description = "Adder target"

[targets.adder.operations.check]
recipe = "configs/recipe.toml"
goals = ["check", "check"]
""",
        ),
        (
            "empty goals",
            """schema = 1
contract_kind = "owner-targets"
path_scope = "owner"
owner = "example"

[targets.adder]
description = "Adder target"

[targets.adder.operations.check]
recipe = "configs/recipe.toml"
goals = []
""",
        ),
    ),
)
def test_target_catalog_rejects_invalid_header_or_goals(
    tmp_path: Path,
    field: str,
    catalog_text: str,
) -> None:
    project = _write_owner_project(tmp_path, catalog_text=catalog_text)

    with pytest.raises(ValueError):
        load_owner_target_catalog(project, "example")


def test_recipe_must_be_a_flow_fileset_toml_with_execution_recipe_header(
    tmp_path: Path,
) -> None:
    project = _write_owner_project(
        tmp_path,
        recipe_value="configs/not-a-recipe.txt",
        catalog_text="""schema = 1
contract_kind = "owner-targets"
path_scope = "owner"
owner = "example"

[targets.adder]
description = "Adder target"

[targets.adder.operations.check]
recipe = "configs/not-a-recipe.txt"
goals = ["check"]
""",
    )
    with pytest.raises(ValueError, match="TOML execution recipe"):
        load_owner_target_catalog(project, "example")

    recipe = tmp_path / "ip/example/configs/unlisted.toml"
    recipe.write_text(
        """schema = 1
contract_kind = "execution-recipe"
path_scope = "owner"
owner = "example"
""",
        encoding="utf-8",
    )
    catalog = tmp_path / "ip/example/configs/targets.toml"
    catalog.write_text(
        catalog.read_text(encoding="utf-8").replace(
            'recipe = "configs/not-a-recipe.txt"',
            'recipe = "configs/unlisted.toml"',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="flow fileset"):
        load_owner_target_catalog(project, "example")


def test_recipe_header_must_match_owner_and_execution_recipe(
    tmp_path: Path,
) -> None:
    project = _write_owner_project(
        tmp_path,
        recipe_text="""schema = 1
contract_kind = "flow"
path_scope = "owner"
owner = "example"
""",
    )

    with pytest.raises(ValueError, match="contract_kind"):
        load_owner_target_catalog(project, "example")


def test_target_catalog_must_be_inside_owner_root(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "ip/other/targets.toml"
    outside.parent.mkdir(parents=True)
    outside.write_text(
        """schema = 1
contract_kind = "owner-targets"
path_scope = "owner"
owner = "example"

[targets.adder]
description = "Adder target"

[targets.adder.operations.check]
recipe = "configs/recipe.toml"
goals = ["check"]
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="target_catalog"):
        _write_owner_project(tmp_path, catalog_value="ip/other/targets.toml")


def test_target_catalog_and_flow_recipe_reject_symlinks(
    tmp_path: Path,
) -> None:
    if not hasattr(Path, "symlink_to"):
        pytest.skip("symlinks are unavailable")
    project = _write_owner_project(tmp_path)
    real_catalog = tmp_path / "ip/example/configs/real-targets.toml"
    real_catalog.write_text(
        (tmp_path / "ip/example/configs/targets.toml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    targets = tmp_path / "ip/example/configs/targets.toml"
    targets.unlink()
    try:
        targets.symlink_to(real_catalog)
    except OSError:
        pytest.skip("symlinks are unavailable")
    with pytest.raises(ValueError, match="symlink"):
        Project.from_project_root(tmp_path)

    # Recreate a regular target catalog and make the selected flow member a link.
    targets.unlink()
    targets.write_text(
        """schema = 1
contract_kind = "owner-targets"
path_scope = "owner"
owner = "example"

[targets.adder]
description = "Adder target"

[targets.adder.operations.check]
recipe = "configs/recipe-link.toml"
goals = ["check"]
""",
        encoding="utf-8",
    )
    real_recipe = tmp_path / "ip/example/configs/recipe-real.toml"
    real_recipe.write_text(
        """schema = 1
contract_kind = "execution-recipe"
path_scope = "owner"
owner = "example"
""",
        encoding="utf-8",
    )
    recipe_link = tmp_path / "ip/example/configs/recipe-link.toml"
    if recipe_link.exists() or recipe_link.is_symlink():
        recipe_link.unlink()
    recipe_link.symlink_to(real_recipe)
    component = tmp_path / "ip/example/component.toml"
    component.write_text(
        component.read_text(encoding="utf-8").replace(
            "ip/example/configs/recipe.toml",
            "ip/example/configs/recipe-link.toml",
        ),
        encoding="utf-8",
    )
    project = Project.from_project_root(tmp_path)
    with pytest.raises(ValueError, match="symlink"):
        load_owner_target_catalog(project, "example")


def test_target_catalog_snapshot_must_match_project_owner(
    tmp_path: Path,
) -> None:
    project = _write_owner_project(tmp_path)
    snapshot = project.owner_target_catalog("example")

    with pytest.raises(ValueError, match="snapshot identity drift"):
        load_owner_target_catalog(
            project,
            "example",
            catalog_snapshot=replace(snapshot, owner="other"),
        )
