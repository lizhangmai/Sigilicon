from __future__ import annotations

from dataclasses import replace

from sigilicon.domain.provenance import canonical_cdl_electrical_fingerprint
from sigilicon.layout.ir import LayoutInstance, LayoutPin, LayoutPlan, LayoutRect
from sigilicon.layout.provenance import (
    layout_hierarchy_fingerprints,
    layout_verification_fingerprint,
)
from sigilicon.virtuoso.layout_generation import render_layout_plan_skill


def _leaf(cell: str, *, width: int = 10, pin_direction: str = "input") -> LayoutPlan:
    return LayoutPlan(
        library="work",
        cell=cell,
        view="layout",
        stage="routed",
        generator="generator",
        generator_version=1,
        laygo2_version="test",
        source_fingerprint=cell * 2,
        dbu_per_micron=1000,
        instances=(),
        rectangles=(
            LayoutRect(
                name="route-name",
                layer="M1",
                purpose="drawing",
                bbox_dbu=((0, 0), (width, 4)),
                net="A",
            ),
        ),
        pins=(
            LayoutPin(
                name="A",
                direction=pin_direction,
                layer="M1",
                purpose="pin",
                bbox_dbu=((0, 0), (2, 2)),
            ),
        ),
    )


def _parent(cell: str, child: str) -> LayoutPlan:
    return LayoutPlan(
        library="work",
        cell=cell,
        view="layout",
        stage="routed",
        generator="generator",
        generator_version=1,
        laygo2_version="test",
        source_fingerprint=cell * 2,
        dbu_per_micron=1000,
        instances=(
            LayoutInstance(
                name="X_RENAME_DOES_NOT_MATTER",
                library="work",
                cell=child,
                view="layout",
                origin_dbu=(20, 30),
                transform="R0",
                parameters=(),
                terminals=(("A", "TOP_A"),),
            ),
        ),
    )


def test_drc_scope_ignores_identity_and_connectivity_but_not_geometry() -> None:
    old = _leaf("OLD", pin_direction="input")
    renamed = replace(
        old,
        cell="NEW",
        generator="different-path-and-generator-name",
        source_fingerprint="new exact source identity",
        rectangles=(replace(old.rectangles[0], name="renamed", net="B"),),
        pins=(replace(old.pins[0], name="B", direction="output"),),
    )

    assert old.fingerprint != renamed.fingerprint
    assert layout_verification_fingerprint(old, scope="drc") == (
        layout_verification_fingerprint(renamed, scope="drc")
    )
    assert layout_verification_fingerprint(old, scope="lvs") != (
        layout_verification_fingerprint(renamed, scope="lvs")
    )
    assert layout_verification_fingerprint(old, scope="drc") != (
        layout_verification_fingerprint(_leaf("NEW", width=11), scope="drc")
    )


def test_oa_layout_fingerprint_ignores_generator_provenance() -> None:
    old = _leaf("CELL")
    provenance_only = replace(
        old,
        generator="refactored_generator",
        generator_version=99,
        laygo2_version="new-tool-version",
        source_fingerprint="new-source-provenance",
    )

    assert old.fingerprint == provenance_only.fingerprint
    assert old.payload() != provenance_only.payload()
    assert old.content_payload() == provenance_only.content_payload()
    assert "new-source-provenance" in provenance_only.canonical_json()


def test_oa_layout_fingerprint_rejects_physical_or_connectivity_changes() -> None:
    old = _leaf("CELL")
    geometry_changed = replace(
        old,
        rectangles=(replace(old.rectangles[0], bbox_dbu=((0, 0), (11, 4))),),
    )
    connectivity_changed = replace(
        old,
        rectangles=(replace(old.rectangles[0], net="B"),),
    )

    assert old.fingerprint != geometry_changed.fingerprint
    assert old.fingerprint != connectivity_changed.fingerprint


def test_oa_layout_properties_separate_content_from_source_provenance() -> None:
    plan = _leaf("CELL")
    skill = render_layout_plan_skill(plan)

    assert '"flowLayoutFingerprint"' in skill
    assert '"flowLayoutSourceFingerprint"' in skill
    assert plan.fingerprint in skill
    assert plan.source_fingerprint in skill


def test_hierarchy_scope_replaces_generated_master_names_with_child_structure() -> None:
    old_plans = (_leaf("OLD_LEAF"), _parent("OLD_TOP", "OLD_LEAF"))
    new_plans = (_leaf("NEW_LEAF"), _parent("NEW_TOP", "NEW_LEAF"))

    old = layout_hierarchy_fingerprints(old_plans, scope="drc")
    new = layout_hierarchy_fingerprints(new_plans, scope="drc")

    assert old[("work", "OLD_LEAF", "layout")] == new[
        ("work", "NEW_LEAF", "layout")
    ]
    assert old[("work", "OLD_TOP", "layout")] == new[
        ("work", "NEW_TOP", "layout")
    ]

    changed = layout_hierarchy_fingerprints(
        (_leaf("NEW_LEAF", width=11), _parent("NEW_TOP", "NEW_LEAF")),
        scope="drc",
    )
    assert old[("work", "OLD_TOP", "layout")] != changed[
        ("work", "NEW_TOP", "layout")
    ]


def test_canonical_cdl_scope_ignores_cell_and_instance_names_only() -> None:
    old = """.SUBCKT OLD_LEAF A Y
M_OLD Y A VSS VSS nch l=30n w=100n
.ENDS OLD_LEAF

.SUBCKT OLD_TOP A Y
X_OLD A Y OLD_LEAF
.ENDS OLD_TOP
"""
    renamed = """.SUBCKT NEW_LEAF A Y
M_NEW Y A VSS VSS nch l=30n w=100n
.ENDS NEW_LEAF

.SUBCKT NEW_TOP A Y
X_NEW A Y NEW_LEAF
.ENDS NEW_TOP
"""
    changed = renamed.replace("w=100n", "w=101n")

    old_fingerprint = canonical_cdl_electrical_fingerprint(
        old, top="OLD_TOP", primitive_masters=("nch",)
    )
    assert old_fingerprint == canonical_cdl_electrical_fingerprint(
        renamed, top="NEW_TOP", primitive_masters=("nch",)
    )
    assert old_fingerprint != canonical_cdl_electrical_fingerprint(
        changed, top="NEW_TOP", primitive_masters=("nch",)
    )
