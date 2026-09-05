"""One capability and maturity policy for source plans and immutable packages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

from sigilicon.domain.ip_release import IpCollateral, IpExport, RtlIpInterface
from sigilicon.adapters.release.release_plan_record import ReleaseAvailability


IMPLEMENTATION_FORMATS = {
    "raw_macro_lef": frozenset({"lef"}),
    "raw_macro_liberty_or_db": frozenset({"liberty", "db"}),
    "raw_macro_gds_or_oasis": frozenset({"gds", "oasis"}),
    "raw_macro_cdl_or_lvs_netlist": frozenset({"cdl", "spice", "spectre"}),
}
RECEIPT_BINDINGS = {
    "schematic_layout_parity_receipt": frozenset({"circuit_netlist", "raw_macro_gds_or_oasis", "raw_macro_cdl_or_lvs_netlist"}),
    "drc_receipt": frozenset({"raw_macro_gds_or_oasis"}),
    "lvs_receipt": frozenset({"circuit_netlist", "raw_macro_gds_or_oasis", "raw_macro_cdl_or_lvs_netlist"}),
    "characterization_receipt": frozenset({"raw_macro_liberty_or_db", "pex_netlist"}),
}
HDL_FORMATS = frozenset({"verilog", "systemverilog"})
CIRCUIT_FORMATS = frozenset({"spectre-source", "spectre", "spice", "cdl", "dspf"})
ROLE_FORMATS = {
    **IMPLEMENTATION_FORMATS,
    "transaction_model": HDL_FORMATS,
    "integration_adapter": HDL_FORMATS,
    "physical_blackbox": HDL_FORMATS,
    "circuit_netlist": CIRCUIT_FORMATS,
    "pex_netlist": frozenset({"dspf", "spice", "spectre"}),
}


@dataclass(frozen=True)
class ReleaseView:
    role: str
    format: str
    capabilities: frozenset[str]
    library: str | None = None
    cell: str | None = None
    view: str | None = None
    corner: str | None = None

    @classmethod
    def from_source(cls, item: IpCollateral) -> ReleaseView:
        return cls(item.role, item.format, frozenset(item.capabilities),
                   item.library, item.cell, item.view, item.corner)

    @classmethod
    def from_record(cls, row: Mapping[str, object]) -> ReleaseView:
        capabilities = row.get("capabilities", [])
        if not isinstance(capabilities, list) or any(not isinstance(value, str) or not value for value in capabilities):
            raise ValueError("release view capabilities must be strings")
        for field in ("role", "format"):
            if not isinstance(row.get(field), str) or not row[field]:
                raise ValueError(f"release view {field} is required")
        for field in ("library", "cell", "view", "corner"):
            if row.get(field) is not None and not isinstance(row[field], str):
                raise ValueError(f"release view {field} must be text")
        return cls(row["role"], row["format"], frozenset(capabilities),
                   *(row.get(field) for field in ("library", "cell", "view", "corner")))

    def supports(self, capability: str, formats: frozenset[str] | None = None) -> bool:
        allowed = formats if formats is not None else ROLE_FORMATS.get(self.role)
        capabilities = {capability}
        if capability == "simulation":
            capabilities.add("circuit_simulation")
        return bool(capabilities & self.capabilities) and (allowed is None or self.format in allowed)


@dataclass(frozen=True)
class ExportSemantics:
    name: str
    kind: str
    required_roles: tuple[str, ...]
    views: tuple[ReleaseView, ...]
    source_role: str | None = None
    oa_identity: tuple[str, str, str, str] | None = None

    @classmethod
    def from_source(cls, exported: IpExport, level: str) -> ExportSemantics:
        interface = exported.interface
        rtl = isinstance(interface, RtlIpInterface)
        return cls(
            exported.name, interface.kind, exported.required_roles[level],
            tuple(ReleaseView.from_source(item) for item in exported.collateral),
            interface.source_role if rtl else None,
            None if rtl else (interface.library, interface.cell, interface.schematic_view, interface.layout_view),
        )

    @classmethod
    def from_record(cls, exported: Mapping[str, object], views: list[object]) -> ExportSemantics:
        name = exported["name"]
        interface = exported["interface"]
        maturity = exported["maturity"]
        if not isinstance(interface, Mapping) or not isinstance(maturity, Mapping):
            raise ValueError("release export interface and maturity are required")
        required = maturity.get("required_roles")
        if not isinstance(required, list) or any(not isinstance(role, str) or not role for role in required):
            raise ValueError("release required_roles must be strings")
        kind = interface.get("kind")
        if kind not in {"rtl", "oa-native", "oa-mixed-signal"}:
            raise ValueError("release interface kind is invalid")
        oa = None
        if kind != "rtl":
            raw = exported.get("oa")
            if not isinstance(raw, Mapping):
                raise ValueError("release OA identity is required")
            oa = tuple(raw.get(key) for key in ("library", "cell", "schematic_view", "layout_view"))
            if any(not isinstance(value, str) or not value for value in oa):
                raise ValueError("release OA identity is incomplete")
        elif "oa" in exported:
            raise ValueError("RTL release cannot declare OA identity")
        selected = tuple(ReleaseView.from_record(row) for row in views
                         if isinstance(row, Mapping) and row.get("export") == name)
        return cls(name, kind, tuple(required), selected, interface.get("source_role"), oa)

    def assess(
        self, level: str, source_commit: str,
        read_receipt: Callable[[str], Mapping[str, object]],
    ) -> tuple[ReleaseAvailability, tuple[str, ...]]:
        by_role = {view.role: view for view in self.views}
        problems = [f"{self.name}:{role}" for role in self.required_roles if role not in by_role]
        if len(by_role) != len(self.views):
            problems.append(f"{self.name}:duplicate-role")
        implementation = level in {"implementation", "signoff"}
        if self.kind != "rtl" and implementation:
            for role in IMPLEMENTATION_FORMATS:
                view = by_role.get(role)
                prefix = f"{self.name}:{role}"
                if view is None:
                    problems.append(prefix)
                elif not self._physical_view(view) or not view.supports("physical_implementation"):
                    problems.append(f"{prefix}:format-identity-or-capability")
        if self.kind != "rtl" and level == "signoff":
            pex = by_role.get("pex_netlist")
            if pex is None or not self._physical_view(pex) or not pex.supports("circuit_simulation"):
                problems.append(f"{self.name}:pex_netlist:format-identity-or-capability")
            for role, bindings in RECEIPT_BINDINGS.items():
                if role not in by_role:
                    problems.append(f"{self.name}:{role}")
                    continue
                try:
                    receipt = read_receipt(role)
                except (OSError, ValueError) as exc:
                    problems.append(f"{self.name}:{role}:invalid-json:{exc}")
                    continue
                problems.extend(self._receipt_problems(role, receipt, source_commit, bindings, by_role))

        def supports(role: str | None, capability: str, formats: frozenset[str] | None = None) -> bool:
            view = by_role.get(role)
            return view is not None and view.supports(capability, formats)

        if self.kind == "rtl":
            simulation = supports(self.source_role, "simulation", HDL_FORMATS)
            synthesis = implementation and supports(self.source_role, "synthesis", HDL_FORMATS)
            physical = implementation and supports(self.source_role, "physical_implementation", HDL_FORMATS)
        else:
            liberty = by_role.get("raw_macro_liberty_or_db")
            linkable = liberty is not None and self._physical_view(liberty) and liberty.supports("synthesis")
            physical = implementation and all(
                supports(role, "physical_implementation") for role in IMPLEMENTATION_FORMATS)
            if self.kind == "oa-native":
                simulation = supports("circuit_netlist", "simulation")
                synthesis = linkable
            else:
                simulation = supports("transaction_model", "simulation")
                synthesis = implementation and linkable and all(
                    supports(role, "synthesis") for role in ("integration_adapter", "physical_blackbox"))
                physical = physical and all(supports(role, "physical_implementation")
                                            for role in ("integration_adapter", "physical_blackbox"))
        valid = not problems
        return ReleaseAvailability(valid and simulation, valid and synthesis, valid and physical), tuple(sorted(set(problems)))

    def _physical_view(self, view: ReleaseView) -> bool:
        return (self.oa_identity is not None
                and (view.library, view.cell) == self.oa_identity[:2]
                and bool(view.view)
                and view.format in ROLE_FORMATS[view.role]
                and (view.role not in {"raw_macro_liberty_or_db", "pex_netlist"} or bool(view.corner)))

    def _receipt_problems(
        self, role: str, receipt: Mapping[str, object], source_commit: str,
        bindings: frozenset[str], by_role: Mapping[str, ReleaseView],
    ) -> list[str]:
        prefix = f"{self.name}:{role}"
        problems = []
        if receipt.get("status") != "passed":
            problems.append(f"{prefix}:status")
        if receipt.get("source_commit") != source_commit:
            problems.append(f"{prefix}:source-commit")
        expected_oa = dict(zip(("library", "cell", "schematic_view", "layout_view"), self.oa_identity, strict=True))
        if receipt.get("oa") != expected_oa:
            problems.append(f"{prefix}:oa-identity")
        tool = receipt.get("tool")
        if not isinstance(tool, Mapping) or any(not isinstance(tool.get(field), str) or not tool[field] for field in ("name", "version")):
            problems.append(f"{prefix}:tool-version")
        receipt_roles = set()
        for field in ("inputs", "outputs"):
            values = receipt.get(field)
            if not isinstance(values, list):
                problems.append(f"{prefix}:{field}")
                continue
            for row in values:
                if not isinstance(row, Mapping) or not isinstance(row.get("role"), str) or not row["role"]:
                    problems.append(f"{prefix}:{field}-entry")
                else:
                    receipt_roles.add(row["role"])
        for bound in bindings:
            if bound not in by_role:
                problems.append(f"{prefix}:missing-bound-role:{bound}")
            elif bound not in receipt_roles:
                problems.append(f"{prefix}:missing-receipt-role:{bound}")
        return problems
