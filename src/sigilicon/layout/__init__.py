"""Stable layout interchange and project generator integration."""

from sigilicon.layout.generator import build_layout_plan
from sigilicon.layout.materialization import (
    MaterializationAcceptance,
    MaterializationDecision,
    MaterializationError,
    MaterializationPlan,
    MaterializationReason,
    MaterializationTarget,
    compile_materialization_plan,
    validate_materialization_plan,
)
from sigilicon.layout.spec import LayoutSpec, load_layout_spec

__all__ = [
    "LayoutSpec",
    "MaterializationAcceptance",
    "MaterializationDecision",
    "MaterializationError",
    "MaterializationPlan",
    "MaterializationReason",
    "MaterializationTarget",
    "build_layout_plan",
    "compile_materialization_plan",
    "load_layout_spec",
    "validate_materialization_plan",
]
