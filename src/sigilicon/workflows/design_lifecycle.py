"""Shared lifecycle for every cataloged canonical design.

Design directories may own topology-specific renderers and measurement
contracts. Loading, OA synchronization, and OA/source attestation remain
application workflows and must not be reimplemented by a design-local script.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from sigilicon.domain.design import DesignSpec, load_design_spec
from sigilicon.domain.platform import PlatformSnapshot
from sigilicon.domain.netlist import NetlistSnapshot, select_subckt_snapshot
from sigilicon.project import Project
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

    def as_dict(self) -> dict[str, object]:
        model = self.spec.pdk.simulation.default
        return {
            "passed": True,
            "library": self.spec.library,
            "cell": self.spec.cell,
            "source": str(self.spec.source_netlist),
            "ports": list(self.spec.port_order),
            "hierarchy": list(self.hierarchy),
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
    project: Project,
    platform: PlatformSnapshot | None = None,
    netlist_snapshot: NetlistSnapshot | None = None,
) -> DesignInspection:
    """Load one canonical spec and prove its source hierarchy is coherent."""

    spec = load_design_spec(
        spec_path,
        project=project,
        platform=platform,
        netlist_snapshot=netlist_snapshot,
    )
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
    )


def synchronize_design(
    inspection: DesignInspection,
    client: Any,
    *,
    source_paths: Mapping[Path, Path],
    overwrite: bool = False,
    timeout: int = 300,
    quarantine_stale_locks: bool = False,
    operation_id: str | None = None,
    bind_operation: Any | None = None,
    resources: Any,
) -> DesignSyncResult | TargetOnlyDesignSyncResult:
    """Synchronize the exact inspected hierarchy through the shared OA sigilicon."""

    if inspection.spec.sync_mode == "target-only":
        try:
            design_source = source_paths[inspection.spec.path.resolve()]
        except KeyError as exc:
            raise ValueError("target-only design source is outside the sealed closure") from exc
        return sync_existing_design_target_only(
            inspection.spec,
            client,
            design_source=design_source,
            overwrite=overwrite,
            timeout=timeout,
            quarantine_stale_locks=quarantine_stale_locks,
            operation_id=operation_id,
            bind_operation=bind_operation,
            resources=resources,
        )
    return sync_design(
        inspection.spec,
        client,
        overwrite=overwrite,
        timeout=timeout,
        quarantine_stale_locks=quarantine_stale_locks,
        operation_id=operation_id,
        bind_operation=bind_operation,
        resources=resources,
    )


def attest_oa_design(
    inspection: DesignInspection,
    client: Any,
    *,
    timeout: int = 60,
    acquire_flow_lock: bool = False,
    record_incident: bool = False,
    operation_id: str | None = None,
    bind_operation: Any | None = None,
    operation: Any | None = None,
    instance_parameters: Mapping[
        str, tuple[str, Mapping[str, str]]
    ] | None = None,
) -> dict[str, object]:
    """Fail unless OA schematic+symbol match the canonical source."""

    spec = inspection.spec

    def attest(current: Any) -> Mapping[str, object]:
        with current.view_lease(
            spec.library,
            cells=(spec.cell,),
            views=((spec.cell, "schematic"), (spec.cell, "symbol")),
        ):
            validate_cell_port_directions(
                client,
                spec.library,
                spec.cell,
                spec.directions,
                operation=current,
                timeout=timeout,
            )
            return validate_instance_parameters(
                client,
                spec.library,
                spec.cell,
                instance_parameters or {},
                operation=current,
                timeout=timeout,
            )

    if operation is not None:
        parameter_report = attest(operation)
    else:
        project = spec.project
        with workspace_operation(
            client,
            project.workspace_root,
            "attest-design-source-parity",
            policy=OperationPolicy.READ_ONLY,
            acquire_flow_lock=acquire_flow_lock,
            record_incident=record_incident,
            operation_id=operation_id,
        ) as current:
            if callable(bind_operation):
                bind_operation(current)
            parameter_report = attest(current)
    return {
        "passed": True,
        "library": spec.library,
        "cell": spec.cell,
        "oa_views": ["schematic", "symbol"],
        "instance_parameters": parameter_report,
    }


def attest_design_set(
    spec_paths: Sequence[Path],
    client: Any,
    *,
    project: Project,
    timeout: int = 60,
) -> dict[str, object]:
    """Attest an explicit dependency set before a design-owned simulation.

    Integration runners must name every schematic dependency they consume.
    Keeping the iteration here prevents design-local scripts from inventing
    weaker OA-view checks.
    """

    reports = tuple(
        attest_oa_design(
            inspect_design(path, project=project),
            client,
            timeout=timeout,
        )
        for path in spec_paths
    )
    return {"passed": True, "designs": reports}
