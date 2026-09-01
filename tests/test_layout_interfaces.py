from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from sigilicon.layout.generator import build_layout_plan
from sigilicon.layout.ir import LayoutInstance, LayoutPlan
from sigilicon.layout.pcell import apply_pcell_semantics
from sigilicon.layout.routing import RoutingStack
from sigilicon.layout.technology import LayoutTechnology, MosPcellInterface


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

    assert build_layout_plan(spec).dbu_per_micron == 1007
    assert str(project_root) not in sys.path

    recipe.write_text("OFFSET = 11\n", encoding="utf-8")
    assert build_layout_plan(spec).dbu_per_micron == 1011


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
