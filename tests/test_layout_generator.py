from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

from sigilicon.layout.generator import build_layout_plan


def test_project_generator_imports_declared_project_code_without_cwd(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "project_recipe.py").write_text(
        "GENERATOR_VERSION = 7\n",
        encoding="utf-8",
    )
    dependency = project_root / "project_dependency.py"
    dependency.write_text("DEPENDENCY_VERSION = 1\n", encoding="utf-8")
    generator = project_root / "layout_generator.py"
    generator.write_text(
        """from sigilicon.layout.ir import LayoutPlan


def build_layout_plan(spec):
    from project_recipe import GENERATOR_VERSION
    from project_dependency import DEPENDENCY_VERSION

    return LayoutPlan(
        library=spec.library,
        cell=spec.cell,
        view=spec.view,
        stage=spec.stage,
        generator=spec.generator,
        generator_version=GENERATOR_VERSION + DEPENDENCY_VERSION,
        laygo2_version="test",
        dbu_per_micron=1000,
        instances=(),
    )
""",
        encoding="utf-8",
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    spec = SimpleNamespace(
        generator_source=generator,
        generator_dependencies=(dependency,),
        generator_modules=("project_recipe",),
        generator_module_sources=(project_root / "project_recipe.py",),
        project_root=project_root,
        library="test_lib",
        cell="test_cell",
        view="layout",
        stage="placement_probe",
        generator="project_recipe",
    )

    plan = build_layout_plan(spec)

    assert plan.generator_version == 8
    assert str(project_root) not in sys.path

    (project_root / "project_recipe.py").write_text(
        "GENERATOR_VERSION = 11\n",
        encoding="utf-8",
    )
    refreshed = build_layout_plan(spec)

    assert refreshed.generator_version == 12

    dependency.write_text("DEPENDENCY_VERSION = 2\n", encoding="utf-8")

    dependency_refreshed = build_layout_plan(spec)

    assert dependency_refreshed.generator_version == 13
