"""Hierarchy planning and guarded OA imports."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Literal

from sigilicon.domain.netlist import (
    NetlistSnapshot,
    load_netlist_snapshot,
    materialize_netlist_snapshot,
    order_subckts,
)
from sigilicon.virtuoso.capability import WorkspaceAuthority, require_workspace_capability
from sigilicon.virtuoso.importer import generate_symbol, import_schematic
from sigilicon.virtuoso.workspace import WorkspaceOperation


_CELL_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")


def validate_hierarchy_cell(cell: str) -> str:
    """Accept only the conservative cell-name subset supported by this flow."""

    if not _CELL_IDENTIFIER_RE.fullmatch(cell):
        raise ValueError(f"invalid hierarchy cell identifier: {cell!r}")
    return cell


@dataclass(frozen=True)
class HierarchyPlan:
    """Ordered cells bound to the exact bytes from which they were discovered."""

    snapshot: NetlistSnapshot
    ordered_cells: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.ordered_cells:
            raise ValueError("ordered hierarchy cells must not be empty")
        for cell in self.ordered_cells:
            validate_hierarchy_cell(cell)
        expected = order_subckts(
            self.snapshot.subckts,
            top=self.ordered_cells[-1],
        )
        if self.ordered_cells != expected:
            raise ValueError(
                "hierarchy plan order/cells do not match its immutable netlist snapshot"
            )


def plan_hierarchy(
    netlist: Path | NetlistSnapshot,
    *,
    top: str | None,
) -> HierarchyPlan:
    """Build one immutable plan; callers must not reparse the source path."""

    snapshot = (
        netlist if isinstance(netlist, NetlistSnapshot) else load_netlist_snapshot(netlist)
    )
    discovered = snapshot.subckts
    for cell in discovered:
        validate_hierarchy_cell(cell)
    if top is not None:
        validate_hierarchy_cell(top)
    return HierarchyPlan(
        snapshot=snapshot,
        ordered_cells=order_subckts(discovered, top=top),
    )


class HierarchyImportError(RuntimeError):
    def __init__(
        self,
        library: str,
        cell: str,
        completed: tuple[str, ...],
        *,
        stage: Literal["schematic", "symbol"],
        cause: BaseException,
    ) -> None:
        self.library = library
        self.cell = cell
        self.completed = completed
        self.stage = stage
        self.schematic_completed = (
            (*completed, cell) if stage == "symbol" else completed
        )
        operation = "schematic import" if stage == "schematic" else "symbol generation"
        partial = " after schematic import" if stage == "symbol" else ""
        done = ", ".join(completed) if completed else "none"
        super().__init__(
            f"{operation} failed for {library}/{cell}{partial}: {cause}; "
            f"completed cells: {done}"
        )


def import_hierarchy(
    client: Any,
    *,
    plan: HierarchyPlan,
    library: str,
    reference_libraries: tuple[str, ...] = (),
    dev_map_file: Path | None = None,
    overwrite: bool,
    artifact: Any,
    source_role: str,
    work_role: str,
    cell_evidence_role: str | None = None,
    timeout: int,
    operation: WorkspaceOperation,
    resources: Any,
) -> tuple[str, ...]:
    """Import a preplanned hierarchy through single-cell adapters."""

    ordered_cells = plan.ordered_cells
    if not ordered_cells:
        raise ValueError("ordered hierarchy cells must not be empty")
    if len(set(ordered_cells)) != len(ordered_cells):
        raise ValueError("ordered hierarchy cells must be unique")
    for cell in ordered_cells:
        validate_hierarchy_cell(cell)
    for cell in ordered_cells:
        for view in ("netlist", "schematic", "symbol"):
            require_workspace_capability(
                operation,
                client,
                authority=WorkspaceAuthority.RECURSIVE_OA,
                library=library,
                cell=cell,
                view=view,
            )
    references = tuple(
        dict.fromkeys((library, *reference_libraries, "analogLib", "basic"))
    )
    source_name = "canonical-netlist.scs"
    immutable_netlist = materialize_netlist_snapshot(
        plan.snapshot,
        artifact.path(source_role, source_name),
    )
    add_file = getattr(artifact, "add_file", None)
    if add_file is not None:
        add_file(
            source_role,
            immutable_netlist.path,
            label="immutable netlist snapshot",
        )
    completed: list[str] = []
    for cell in ordered_cells:
        cell_run_dir = artifact.directory(work_role, cell)
        try:
            with operation.mutation_scope(
                library,
                cells=(cell,),
                views=((cell, "netlist"), (cell, "schematic")),
                phase=f"import schematic {library}/{cell}",
            ):
                import_schematic(
                    client,
                    library,
                    cell,
                    immutable_netlist.path,
                    own_netlist=immutable_netlist.open_fd,
                    reference_libraries=references,
                    dev_map_file=dev_map_file,
                    overwrite=overwrite,
                    run_dir=cell_run_dir,
                    timeout=timeout,
                    operation=operation,
                    resources=resources,
                )
        except Exception as exc:
            raise HierarchyImportError(
                library,
                cell,
                tuple(completed),
                stage="schematic",
                cause=exc,
            ) from exc
        except BaseException as exc:
            exc.add_note(
                f"hierarchy import interrupted during schematic {library}/{cell}; "
                f"completed cells: {tuple(completed)!r}"
            )
            raise
        try:
            with operation.mutation_scope(
                library,
                cells=(cell,),
                views=((cell, "symbol"),),
                phase=f"generate symbol {library}/{cell}",
            ):
                generate_symbol(
                    client,
                    library,
                    cell,
                    sort_pins="geometric",
                    overwrite=overwrite,
                    timeout=timeout,
                    operation=operation,
                )
        except Exception as exc:
            raise HierarchyImportError(
                library,
                cell,
                tuple(completed),
                stage="symbol",
                cause=exc,
            ) from exc
        except BaseException as exc:
            exc.add_note(
                f"hierarchy import interrupted during symbol {library}/{cell}; "
                f"completed cells: {tuple(completed)!r}"
            )
            raise
        if cell_evidence_role is not None:
            artifact.write_json(
                cell_evidence_role,
                (f"{cell}.json",),
                {
                    "library": library,
                    "cell": cell,
                    "schematic_confirmed": True,
                    "symbol_confirmed": True,
                },
                label=f"completed OA cell {library}/{cell}",
            )
        completed.append(cell)
    return tuple(completed)
