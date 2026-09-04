from __future__ import annotations

from pathlib import Path
import importlib
import sys
from types import SimpleNamespace

import pytest

from sigilicon.domain.netlist import NetlistSnapshot
from sigilicon.execution._workspace import ExecutionWorkspace
from sigilicon.layout.generator import build_layout_plan_from_sources
from sigilicon.workflows.layout_generation import (
    LayoutPlanningResult,
    build_managed_layout_ir,
)
from sigilicon.layout.ir import LayoutInstance, LayoutPlan
from sigilicon.layout.pcell import apply_pcell_semantics
from sigilicon.layout.routing import RoutingStack
from sigilicon.domain.layout_technology import LayoutTechnology, MosPcellInterface


def _technology() -> LayoutTechnology:
    return LayoutTechnology(
        owner="test-owner",
        model_polarities={"nch": "nmos"},
        layers={
            "routing1": "M1",
            "routing2": "M2",
            "routing3": "M3",
            "routing4": "M4",
        },
        vias={
            "routing1_routing2": "V12",
            "routing2_routing3": "V23",
            "routing3_routing4": "V34",
        },
        via_landings={
            "routing1_routing2": {"routing1": (10, 10), "routing2": (10, 10)},
            "routing2_routing3": {"routing2": (10, 10), "routing3": (10, 10)},
            "routing3_routing4": {"routing3": (10, 10), "routing4": (10, 10)},
        },
        mos_pcell=MosPcellInterface(
            length_parameter="l",
            width_parameter="Wfg",
            finger_count_parameter="fingers",
            source_terminal="S",
            drain_terminal="D",
            source_alias_prefix="S_",
            drain_alias_prefix="D_",
            cdf_callback_parameter="routePolydir",
            cdf_callback_bypass_parameters=("polyContacts",),
            gate_contact_value="Bottom",
            gate_contact_enhancement_parameter="polyContactsEnh",
            gate_contact_enhancement_value="Bottom",
        ),
    )


def test_project_layout_generator_loads_fresh_declared_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    recipe = project_root / "project_recipe.py"
    recipe.write_text("OFFSET = 7\n", encoding="utf-8")
    generator = project_root / "layout_generator.py"
    generator.write_text(
        """from sigilicon.layout.ir import LayoutPlan

def build_layout_plan(spec):
    from project_recipe import OFFSET
    return LayoutPlan(
        library=spec.library,
        cell=spec.cell,
        view=spec.view,
        stage=spec.stage,
        generator=spec.generator,
        dbu_per_micron=1000 + OFFSET,
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
        generator_dependencies=(),
        generator_modules=("project_recipe",),
        generator_module_sources=(recipe,),
        project_root=project_root,
        library="test_lib",
        cell="test_cell",
        view="layout",
        stage="routed",
        generator="project_recipe",
    )

    assert build_layout_plan_from_sources(
        spec,
        project_root=project_root,
        source_project_root=project_root,
        generator_source=generator,
        dependency_sources=(),
        project_modules=(("project_recipe", recipe),),
    ).dbu_per_micron == 1007
    assert str(project_root) not in sys.path
    assert Path.cwd() == outside

    recipe.write_text("OFFSET = 11\n", encoding="utf-8")
    assert build_layout_plan_from_sources(
        spec,
        project_root=project_root,
        source_project_root=project_root,
        generator_source=generator,
        dependency_sources=(),
        project_modules=(("project_recipe", recipe),),
    ).dbu_per_micron == 1011


def test_managed_layout_ir_uses_only_sealed_owner_code(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    recipe = project_root / "project_recipe.py"
    recipe.write_text("OFFSET = 7\n", encoding="utf-8")
    netlist = project_root / "circuit.scs"
    netlist.write_text("subckt test_cell A Y\nends test_cell\n", encoding="utf-8")
    generator = project_root / "layout_generator.py"
    generator.write_text(
        """from sigilicon.layout.ir import LayoutPlan

def build_layout_plan(spec):
    assert not hasattr(spec, "project")
    assert not hasattr(spec, "project_root")
    assert not hasattr(spec, "path")
    from project_recipe import OFFSET
    return LayoutPlan(
        library=spec.library,
        cell=spec.cell,
        view=spec.view,
        stage=spec.stage,
        generator=spec.generator,
        dbu_per_micron=1000 + OFFSET,
        instances=(),
    )
""",
        encoding="utf-8",
    )
    source_snapshot = NetlistSnapshot(
        source_path=netlist,
        text=netlist.read_text(encoding="utf-8"),
        interfaces={"test_cell": ("A", "Y")},
    )
    spec = SimpleNamespace(
        project_root=project_root,
        generator_source=generator,
        generator_dependencies=(),
        generator_modules=("project_recipe",),
        generator_module_sources=(recipe,),
        library="test_lib",
        cell="test_cell",
        view="layout",
        stage="routed",
        generator="sealed_recipe",
        source_snapshot=source_snapshot,
        source_snapshots=(source_snapshot,),
        ports=("A", "Y"),
        directions={"A": "input", "Y": "output"},
        primitive_masters=(),
        pdk=SimpleNamespace(
            oa=SimpleNamespace(technology_library="test_tech")
        ),
        layout_pdk=SimpleNamespace(dbu_per_micron=1007),
    )
    snapshots = {
        generator: generator.read_text(encoding="utf-8"),
        recipe: recipe.read_text(encoding="utf-8"),
        netlist: netlist.read_text(encoding="utf-8"),
    }
    planning = LayoutPlanningResult(spec, source_records=snapshots)
    assert planning.plan is None
    sealed_generator = tmp_path / "sealed/generator.py"
    sealed_recipe = tmp_path / "sealed/recipe.py"
    sealed_netlist = tmp_path / "sealed/circuit.scs"
    sealed_generator.parent.mkdir()
    sealed_generator.write_text(snapshots[generator], encoding="utf-8")
    sealed_recipe.write_text(snapshots[recipe], encoding="utf-8")
    sealed_netlist.write_text(snapshots[netlist], encoding="utf-8")
    generator.write_text("raise RuntimeError('read original generator')\n", encoding="utf-8")
    recipe.write_text("OFFSET = 99\n", encoding="utf-8")
    netlist.write_text("mutated\n", encoding="utf-8")

    managed = build_managed_layout_ir(
        planning,
        source_paths={
            generator: sealed_generator,
            recipe: sealed_recipe,
            netlist: sealed_netlist,
        },
        workspace=ExecutionWorkspace(
            run_id="layout-ir-test",
            root=tmp_path / "managed",
            input_root=tmp_path / "managed/inputs",
            work_root=tmp_path / "managed/work",
            output_root=tmp_path / "managed/outputs",
            log_root=tmp_path / "managed/logs",
            source={},
        ),
    )

    assert managed.plan is not None
    assert managed.plan.dbu_per_micron == 1007


def test_layout_generator_cannot_import_unsealed_project_module(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "source"
    managed_root = tmp_path / "managed"
    source_root.mkdir()
    managed_root.mkdir()
    (source_root / "unsealed.py").write_text("OFFSET = 99\n", encoding="utf-8")
    generator = managed_root / "layout_generator.py"
    generator.write_text(
        "from pathlib import Path\n"
        "from sigilicon.layout.ir import LayoutPlan\n"
        "def build_layout_plan(spec):\n"
        f"    assert Path.cwd() == Path({str(managed_root)!r})\n"
        "    from unsealed import OFFSET\n"
        "    return LayoutPlan(library=spec.library, cell=spec.cell, "
        "view=spec.view, stage=spec.stage, generator=spec.generator, "
        "dbu_per_micron=OFFSET, instances=())\n",
        encoding="utf-8",
    )
    monkeypatch.delitem(sys.modules, "unsealed", raising=False)
    monkeypatch.syspath_prepend(str(source_root))
    monkeypatch.chdir(source_root)
    importlib.import_module("unsealed")
    spec = SimpleNamespace(
        library="test_lib",
        cell="test_cell",
        view="layout",
        stage="routed",
        generator="sealed_recipe",
    )

    with pytest.raises(ModuleNotFoundError, match="unsealed"):
        build_layout_plan_from_sources(
            spec,
            project_root=managed_root,
            source_project_root=source_root,
            generator_source=generator,
            dependency_sources=(),
            project_modules=(),
        )
    assert Path.cwd() == source_root


def test_layout_generator_discards_unsealed_namespace_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "source"
    managed_root = tmp_path / "managed"
    package = source_root / "unsealed_package"
    package.mkdir(parents=True)
    managed_root.mkdir()
    (package / "payload.py").write_text("OFFSET = 99\n", encoding="utf-8")
    generator = managed_root / "layout_generator.py"
    generator.write_text(
        "from unsealed_package.payload import OFFSET\n"
        "from sigilicon.layout.ir import LayoutPlan\n"
        "def build_layout_plan(spec):\n"
        "    return LayoutPlan(library=spec.library, cell=spec.cell, "
        "view=spec.view, stage=spec.stage, generator=spec.generator, "
        "dbu_per_micron=OFFSET, instances=())\n",
        encoding="utf-8",
    )
    monkeypatch.delitem(sys.modules, "unsealed_package", raising=False)
    monkeypatch.delitem(sys.modules, "unsealed_package.payload", raising=False)
    monkeypatch.syspath_prepend(str(source_root))
    importlib.import_module("unsealed_package")
    spec = SimpleNamespace(
        library="test_lib",
        cell="test_cell",
        view="layout",
        stage="routed",
        generator="sealed_recipe",
    )

    with pytest.raises(ModuleNotFoundError, match="unsealed_package"):
        build_layout_plan_from_sources(
            spec,
            project_root=managed_root,
            source_project_root=source_root,
            generator_source=generator,
            dependency_sources=(),
            project_modules=(),
        )


def test_explicit_source_exclusion_wins_inside_runtime_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = tmp_path / "runtime"
    source_root = runtime_root / "owner"
    managed_root = tmp_path / "managed"
    source_root.mkdir(parents=True)
    managed_root.mkdir()
    (source_root / "unsealed.py").write_text("OFFSET = 99\n", encoding="utf-8")
    generator = managed_root / "layout_generator.py"
    generator.write_text(
        "from unsealed import OFFSET\n"
        "from sigilicon.layout.ir import LayoutPlan\n"
        "def build_layout_plan(spec):\n"
        "    return LayoutPlan(library=spec.library, cell=spec.cell, "
        "view=spec.view, stage=spec.stage, generator=spec.generator, "
        "dbu_per_micron=OFFSET, instances=())\n",
        encoding="utf-8",
    )
    monkeypatch.delitem(sys.modules, "unsealed", raising=False)
    monkeypatch.setattr(sys, "prefix", str(runtime_root))
    monkeypatch.syspath_prepend(str(source_root))
    importlib.import_module("unsealed")
    spec = SimpleNamespace(
        library="test_lib",
        cell="test_cell",
        view="layout",
        stage="routed",
        generator="sealed_recipe",
    )

    with pytest.raises(ModuleNotFoundError, match="unsealed"):
        build_layout_plan_from_sources(
            spec,
            project_root=managed_root,
            source_project_root=source_root,
            generator_source=generator,
            dependency_sources=(),
            project_modules=(),
        )


def test_pcell_semantics_apply_terminal_alias_and_callback_policy() -> None:
    instance = LayoutInstance(
        name="M0",
        library="pdk",
        cell="nch",
        view="layout",
        origin_dbu=(0, 0),
        transform="R0",
        parameters=(
            ("fingers", "string", "4"),
            ("routePolydir", "string", "Bottom"),
        ),
        terminals=(("B", "VSS"), ("D", "Y"), ("G", "A"), ("S", "VSS")),
    )
    plan = LayoutPlan(
        library="test",
        cell="CELL",
        view="layout",
        stage="routed",
        generator="test",
        dbu_per_micron=1000,
        instances=(instance,),
    )

    lowered = apply_pcell_semantics(plan, _technology()).instances[0]

    assert lowered.expected_master_terminals == (
        "B",
        "D",
        "D_1",
        "G",
        "S",
        "S_1",
        "S_2",
    )
    assert lowered.callback_parameters == ("routePolydir",)


def test_routing_stack_resolves_declared_stack_and_landing_policy() -> None:
    stack = RoutingStack(
        _technology(),
        landing_overrides={"routing2_routing3": {"routing3": (20, 30)}},
    )

    assert stack.layers == ("M1", "M2", "M3", "M4")
    assert stack.vias_between("M2", "M4") == ("V23", "V34")
    assert stack.landing_shapes("V23") == (("M2", 10, 10), ("M3", 20, 30))

    with pytest.raises(ValueError, match="has no landing"):
        RoutingStack(
            _technology(),
            landing_overrides={"routing2_routing3": {"routing1": (20, 30)}},
        )
