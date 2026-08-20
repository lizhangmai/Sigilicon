"""Shared lifecycle for every cataloged canonical design.

Design directories may own topology-specific renderers and measurement
contracts.  Loading, fingerprinting, OA synchronization, and OA/source
attestation remain application workflows and must not be reimplemented by a
design-local script.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from sigilicon.domain.design import DesignSpec, load_design_spec
from sigilicon.domain.netlist import select_subckt_snapshot
from sigilicon.domain.provenance import design_fingerprint
from sigilicon.paths import ProjectContext
from sigilicon.virtuoso.oa import (
    validate_cell_port_directions,
    validate_instance_parameters,
)
from sigilicon.virtuoso.workspace import OperationPolicy, workspace_operation
from sigilicon.workflows.design_sync import (
    DesignSyncResult,
    TargetOnlyDesignSyncResult,
    sync_design,
    sync_existing_design_target_only,
)
from sigilicon.workflows.hierarchy_import import plan_hierarchy


@dataclass(frozen=True)
class DesignInspection:
    spec: DesignSpec
    hierarchy: tuple[str, ...]
    source_fingerprint: str

    def as_dict(self) -> dict[str, object]:
        model = self.spec.pdk.simulation.default
        return {
            "passed": True,
            "library": self.spec.library,
            "cell": self.spec.cell,
            "source": str(self.spec.source_netlist),
            "ports": list(self.spec.port_order),
            "hierarchy": list(self.hierarchy),
            "source_fingerprint": self.source_fingerprint,
            "pdk": {
                "name": self.spec.pdk.name,
                "technology_library": self.spec.pdk.oa.technology_library,
                "model_file": str(model.file),
                "model_section": model.single_section,
            },
        }


def inspect_design(
    spec_path: Path,
    *,
    project_root: Path | None = None,
) -> DesignInspection:
    """Load one canonical spec and prove its source hierarchy is coherent."""

    spec = load_design_spec(spec_path, project_root=project_root)
    if spec.sync_mode == "target-only" and spec.netlist_snapshot.subckts != (spec.cell,):
        spec = replace(
            spec,
            netlist_snapshot=select_subckt_snapshot(spec.netlist_snapshot, spec.cell),
        )
    model_file = spec.pdk.simulation.default.file
    if not model_file.is_file():
        raise FileNotFoundError(f"PDK model file does not exist: {model_file}")
    plan = plan_hierarchy(spec.netlist_snapshot, top=spec.cell)
    if not plan.ordered_cells or plan.ordered_cells[-1] != spec.cell:
        raise RuntimeError(f"invalid hierarchy plan for {spec.library}/{spec.cell}")
    return DesignInspection(
        spec=spec,
        hierarchy=plan.ordered_cells,
        source_fingerprint=design_fingerprint(spec),
    )


def synchronize_design(
    inspection: DesignInspection,
    client: Any,
    *,
    overwrite: bool = False,
    timeout: int = 300,
    quarantine_stale_locks: bool = False,
    artifact_root: Path | None = None,
    disposable: bool = False,
) -> DesignSyncResult | TargetOnlyDesignSyncResult:
    """Synchronize the exact inspected hierarchy through the shared OA sigilicon."""

    synchronizer = (
        sync_existing_design_target_only
        if inspection.spec.sync_mode == "target-only"
        else sync_design
    )
    return synchronizer(
        inspection.spec,
        client,
        artifact_root=artifact_root,
        overwrite=overwrite,
        timeout=timeout,
        quarantine_stale_locks=quarantine_stale_locks,
        disposable=disposable,
    )


def attest_oa_design(
    inspection: DesignInspection,
    client: Any,
    *,
    timeout: int = 60,
    acquire_flow_lock: bool = False,
    record_incident: bool = False,
    instance_parameters: Mapping[
        str, tuple[str, Mapping[str, str]]
    ] | None = None,
) -> dict[str, object]:
    """Fail unless OA schematic+symbol match the canonical source fingerprint."""

    spec = inspection.spec
    paths = ProjectContext.from_project_root(spec.project_root)
    with workspace_operation(
        client,
        paths.workspace_root,
        "attest-design-source-parity",
        policy=OperationPolicy.READ_ONLY,
        acquire_flow_lock=acquire_flow_lock,
        record_incident=record_incident,
    ) as operation, operation.view_lease(
        spec.library,
        cells=(spec.cell,),
        views=((spec.cell, "schematic"), (spec.cell, "symbol")),
    ):
        validate_cell_port_directions(
            client,
            spec.library,
            spec.cell,
            spec.directions,
            fingerprint=inspection.source_fingerprint,
            operation=operation,
            timeout=timeout,
        )
        parameter_report = validate_instance_parameters(
            client,
            spec.library,
            spec.cell,
            instance_parameters or {},
            operation=operation,
            timeout=timeout,
        )
    return {
        "passed": True,
        "library": spec.library,
        "cell": spec.cell,
        "source_fingerprint": inspection.source_fingerprint,
        "oa_views": ["schematic", "symbol"],
        "instance_parameters": parameter_report,
    }


def attest_design_set(
    spec_paths: Sequence[Path],
    client: Any,
    *,
    project_root: Path,
    timeout: int = 60,
) -> dict[str, object]:
    """Attest an explicit dependency set before a design-owned simulation.

    Integration runners must name every schematic dependency they consume.
    Keeping the iteration here prevents design-local scripts from inventing
    weaker fingerprint or OA-view checks.
    """

    reports = tuple(
        attest_oa_design(
            inspect_design(path, project_root=project_root),
            client,
            timeout=timeout,
        )
        for path in spec_paths
    )
    return {"passed": True, "designs": reports}
