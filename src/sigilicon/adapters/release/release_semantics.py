"""One capability and maturity policy for source plans and immutable packages."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Callable, Mapping

from sigilicon.canonical import canonical_digest, canonical_json
from sigilicon.release_views import view_condition
from sigilicon.domain.ip_release import IpExport, RtlIpInterface, ReceiptPolicy, receipt_policies
from sigilicon.adapters.release.release_plan_record import ReleaseAvailability, ReleaseCollateralRecord


IMPLEMENTATION_FORMATS = {
    "raw_macro_lef": frozenset({"lef"}),
    "raw_macro_liberty_or_db": frozenset({"liberty", "db"}),
    "raw_macro_gds_or_oasis": frozenset({"gds", "oasis"}),
    "raw_macro_cdl_or_lvs_netlist": frozenset({"cdl", "spice", "spectre"}),
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

# Purposes required by the native physical signoff domain. Owners bind these
# purposes to exact, independently named views, including every required corner.
NATIVE_SIGNOFF_BINDINGS = {
    "schematic_layout_parity_receipt": (("circuit_netlist", "raw_macro_gds_or_oasis", "raw_macro_cdl_or_lvs_netlist"), ()),
    "drc_receipt": (("raw_macro_gds_or_oasis",), ()),
    "lvs_receipt": (("circuit_netlist", "raw_macro_gds_or_oasis", "raw_macro_cdl_or_lvs_netlist"), ()),
    "characterization_receipt": (("pex_netlist",), ("raw_macro_liberty_or_db",)),
}


@dataclass(frozen=True)
class ReceiptArtifact:
    """Content identity addressed by export-local name, independent of package paths."""

    name: str
    size: int
    sha256: str

    @classmethod
    def from_record(cls, row: Mapping[str, object]) -> ReceiptArtifact:
        if not isinstance(row, Mapping) or set(row) != {"name", "size", "sha256"}:
            raise ValueError("receipt artifact requires name, size and sha256")
        if not isinstance(row["name"], str) or not row["name"]:
            raise ValueError("receipt artifact name is invalid")
        if type(row["size"]) is not int or row["size"] < 0:
            raise ValueError("receipt artifact size is invalid")
        if not isinstance(row["sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", row["sha256"]) is None:
            raise ValueError("receipt artifact sha256 is invalid")
        return cls(row["name"], row["size"], row["sha256"])

    @property
    def record(self) -> dict[str, object]:
        return {"name": self.name, "size": self.size, "sha256": self.sha256}


@dataclass(frozen=True)
class SignoffReceipt:
    name: str
    status: str
    source_identity: str
    subject: Mapping[str, str]
    execution: Mapping[str, object]
    tool_name: str
    tool_version: str
    inputs: tuple[ReceiptArtifact, ...]
    outputs: tuple[ReceiptArtifact, ...]
    variant: str | None
    condition: Mapping[str, str | int | float | bool]
    coverage: tuple[str, ...]

    @classmethod
    def from_record(cls, row: Mapping[str, object]) -> SignoffReceipt:
        fields = {"schema", "contract_kind", "name", "status", "source_identity", "subject", "execution", "tool", "inputs", "outputs", "variant", "condition", "coverage"}
        if not isinstance(row, Mapping) or set(row) != fields:
            raise ValueError("invalid signoff receipt envelope")
        if type(row["schema"]) is not int or row["schema"] != 3 or row["contract_kind"] != "release-receipt":
            raise ValueError("invalid signoff receipt schema")
        if not isinstance(row["name"], str) or not row["name"] or not isinstance(row["status"], str):
            raise ValueError("invalid signoff receipt name or status")
        if not isinstance(row["source_identity"], str) or re.fullmatch(r"sha256-[0-9a-f]{64}", row["source_identity"]) is None:
            raise ValueError("invalid receipt source-identity")
        subject = row["subject"]
        if not isinstance(subject, Mapping) or not subject or any(
            not isinstance(value, str) or not value for value in subject.values()
        ):
            raise ValueError("invalid receipt subject-identity")
        execution = row["execution"]
        if not isinstance(execution, Mapping) or set(execution) != {
            "run_id", "operation_id", "plan_identity", "executed", "report_parsed", "exit_code"
        } or any(not isinstance(execution[key], str) or not execution[key]
                 for key in ("run_id", "operation_id", "plan_identity")):
            raise ValueError("invalid receipt execution provenance")
        if (execution["executed"] is not True or execution["report_parsed"] is not True
                or type(execution["exit_code"]) is not int or execution["exit_code"] != 0):
            raise ValueError("receipt execution is incomplete or failed")
        tool = row["tool"]
        if not isinstance(tool, Mapping) or set(tool) != {"name", "version"} or any(
            not isinstance(value, str) or not value for value in tool.values()
        ):
            raise ValueError("invalid receipt tool-version")
        bindings = []
        for field in ("inputs", "outputs"):
            if not isinstance(row[field], list):
                raise ValueError(f"receipt {field} must be an array")
            bindings.append(tuple(ReceiptArtifact.from_record(value) for value in row[field]))
        variant = row["variant"]
        if variant is not None and (not isinstance(variant, str) or not variant):
            raise ValueError("invalid receipt variant")
        coverage = row["coverage"]
        if not isinstance(coverage, list) or any(not isinstance(item, str) or not item for item in coverage) or len(set(coverage)) != len(coverage):
            raise ValueError("invalid receipt coverage")
        return cls(row["name"], row["status"], row["source_identity"], dict(subject), dict(execution), tool["name"], tool["version"], *bindings,
                   variant, view_condition(row["condition"]), tuple(coverage))


@dataclass(frozen=True)
class ReleaseView:
    name: str
    role: str
    format: str
    capabilities: frozenset[str]
    size: int
    sha256: str
    library: str | None = None
    cell: str | None = None
    view: str | None = None
    variant: str | None = None
    condition: Mapping[str, str | int | float | bool] = field(default_factory=dict)

    @classmethod
    def from_source(cls, item: ReleaseCollateralRecord) -> ReleaseView:
        return cls(item.name, item.role, item.format, frozenset(item.capabilities),
                   item.size, item.sha256,
                   item.library, item.cell, item.view, item.variant, item.condition)

    @classmethod
    def from_record(cls, row: Mapping[str, object]) -> ReleaseView:
        capabilities = row.get("capabilities", [])
        if not isinstance(capabilities, list) or any(not isinstance(value, str) or not value for value in capabilities):
            raise ValueError("release view capabilities must be strings")
        for field in ("name", "role", "format"):
            if not isinstance(row.get(field), str) or not row[field]:
                raise ValueError(f"release view {field} is required")
        for field in ("library", "cell", "view", "variant"):
            if row.get(field) is not None and not isinstance(row[field], str):
                raise ValueError(f"release view {field} must be text")
        identity = ReceiptArtifact.from_record({key: row.get(key) for key in ("name", "size", "sha256")})
        return cls(row["name"], row["role"], row["format"], frozenset(capabilities), identity.size, identity.sha256,
                   *(row.get(field) for field in ("library", "cell", "view", "variant")),
                   view_condition(row.get("condition", {})))

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
    required_views: tuple[str, ...]
    views: tuple[ReleaseView, ...]
    source_view: str | None = None
    oa_identity: tuple[str, str, str, str] | None = None
    subject: Mapping[str, str] = field(default_factory=dict)
    receipts: Mapping[str, ReceiptPolicy] = field(default_factory=dict)

    @classmethod
    def from_source(cls, exported: IpExport, level: str, collateral: tuple[ReleaseCollateralRecord, ...]) -> ExportSemantics:
        interface = exported.interface
        rtl = isinstance(interface, RtlIpInterface)
        return cls(
            exported.name, interface.kind, exported.required_views[level],
            tuple(ReleaseView.from_source(item) for item in collateral if item.export == exported.name),
            interface.source_view if rtl else None,
            None if rtl else (interface.library, interface.cell, interface.schematic_view, interface.layout_view),
            ({"kind": interface.kind, "module": interface.module,
              **({"variant": interface.variant} if interface.variant else {})} if rtl else
             {"kind": interface.kind, **{key: getattr(interface, key)
              for key in ("library", "cell", "schematic_view", "layout_view")}}),
            exported.receipts,
        )

    @classmethod
    def from_record(cls, exported: Mapping[str, object], views: list[object]) -> ExportSemantics:
        name = exported["name"]
        interface = exported["interface"]
        maturity = exported["maturity"]
        if not isinstance(interface, Mapping) or not isinstance(maturity, Mapping):
            raise ValueError("release export interface and maturity are required")
        required = maturity.get("required_views")
        if not isinstance(required, list) or any(not isinstance(role, str) or not role for role in required):
            raise ValueError("release required_views must be strings")
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
        subject = ({"kind": kind, "module": interface.get("module"),
                    **({"variant": interface["variant"]} if "variant" in interface else {})}
                   if kind == "rtl" else {"kind": kind, **dict(exported["oa"])})
        return cls(name, kind, tuple(required), selected, interface.get("source_view"), oa,
                   subject, receipt_policies(exported.get("receipts", {})))

    def assess(
        self, level: str,
        read_receipt: Callable[[str], Mapping[str, object]],
    ) -> tuple[ReleaseAvailability, tuple[str, ...]]:
        by_name = {view.name: view for view in self.views}
        by_role = {role: tuple(view for view in self.views if view.role == role)
                   for role in {view.role for view in self.views}}
        problems = [f"{self.name}:{name}" for name in self.required_views if name not in by_name]
        if len(by_name) != len(self.views):
            problems.append(f"{self.name}:duplicate-view")
        implementation = level in {"implementation", "signoff"}
        physical_views = {role: tuple(view for view in by_role.get(role, ()) if view.name in self.required_views)
                          for role in IMPLEMENTATION_FORMATS}
        if self.kind != "rtl" and implementation:
            for role in IMPLEMENTATION_FORMATS:
                views = physical_views[role]
                prefix = f"{self.name}:{role}"
                if not views:
                    problems.append(prefix)
                elif any(not self._physical_view(view) or not view.supports("physical_implementation") for view in views):
                    problems.append(f"{prefix}:format-identity-or-capability")
        if self.kind != "rtl" and level == "signoff":
            pex = by_role.get("pex_netlist", ())
            if not pex or any(not self._physical_view(view) or not view.supports("circuit_simulation") for view in pex):
                problems.append(f"{self.name}:pex_netlist:format-identity-or-capability")
            problems.extend(self._native_signoff_coverage(by_name, by_role))
        if level == "signoff" and not self.receipts:
            problems.append(f"{self.name}:missing-receipt-policy")
        for view in self.views:
            if "signoff" in view.capabilities and view.name not in self.receipts:
                problems.append(f"{self.name}:{view.name}:missing-receipt-policy")
        for role in self.receipts:
            if level != "signoff" and role not in self.required_views:
                continue
            if role not in by_name:
                problems.append(f"{self.name}:{role}")
                continue
            try:
                receipt = read_receipt(role)
            except (OSError, ValueError) as exc:
                problems.append(f"{self.name}:{role}:invalid-json:{exc}")
                continue
            problems.extend(self._receipt_problems(role, receipt, by_name))

        def supports(role: str | None, capability: str, formats: frozenset[str] | None = None) -> bool:
            return any(view.supports(capability, formats) for view in by_role.get(role, ()))

        if self.kind == "rtl":
            source = by_name.get(self.source_view)
            simulation = source is not None and source.supports("simulation", HDL_FORMATS)
            synthesis = implementation and source is not None and source.supports("synthesis", HDL_FORMATS)
            physical = implementation and source is not None and source.supports("physical_implementation", HDL_FORMATS)
        else:
            liberty = by_role.get("raw_macro_liberty_or_db", ())
            linkable = any(self._physical_view(view) and view.supports("synthesis") for view in liberty)
            physical = implementation and all(
                physical_views[role] and all(view.supports("physical_implementation") for view in physical_views[role])
                for role in IMPLEMENTATION_FORMATS)
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

    def _native_signoff_coverage(
        self, by_name: Mapping[str, ReleaseView], by_role: Mapping[str, tuple[ReleaseView, ...]],
    ) -> list[str]:
        problems = []
        for role, directions in NATIVE_SIGNOFF_BINDINGS.items():
            receipts = [view for view in by_role.get(role, ()) if view.name in self.receipts]
            if not receipts:
                problems.append(f"{self.name}:{role}:missing-evidence")
            for field, purposes in zip(("inputs", "outputs"), directions):
                covered = set()
                for receipt in receipts:
                    names = getattr(self.receipts[receipt.name], field)
                    selected = [by_name[name] for name in names if name in by_name]
                    if not set(purposes).issubset({view.role for view in selected}):
                        problems.append(f"{self.name}:{receipt.name}:{field}:evidence-purpose")
                    covered.update(names)
                    if role == "characterization_receipt" and field == "outputs":
                        for view in selected:
                            if view.role in purposes and "characterized" not in view.capabilities:
                                problems.append(f"{self.name}:{receipt.name}:{view.name}:characterization-capability")
                required = {view.name for purpose in purposes for view in by_role.get(purpose, ())
                            if view.name in self.required_views}
                if not required.issubset(covered):
                    problems.append(f"{self.name}:{role}:{field}:incomplete-evidence-coverage")
        return problems

    def _physical_view(self, view: ReleaseView) -> bool:
        return (self.oa_identity is not None
                and (view.library, view.cell) == self.oa_identity[:2]
                and bool(view.view)
                and view.format in ROLE_FORMATS[view.role]
                and (view.role not in {"raw_macro_liberty_or_db", "pex_netlist"} or bool(view.condition)))

    @property
    def source_identity(self) -> str:
        """Identity of the exported design/collateral, excluding its attestations.

        Receipts can be committed after this content is verified without introducing
        a self-reference to the commit that stores the receipts themselves.
        """
        return canonical_digest({
            "export": self.name, "subject": dict(self.subject),
            "receipts": {role: policy.record for role, policy in self.receipts.items()},
            "views": [{
                "name": view.name, "role": view.role, "size": view.size, "sha256": view.sha256,
                "format": view.format, "capabilities": sorted(view.capabilities),
                "library": view.library, "cell": view.cell, "view": view.view,
                "variant": view.variant, "condition": dict(view.condition),
            } for view in sorted(self.views, key=lambda item: item.name) if view.name not in self.receipts],
        })

    def _receipt_problems(
        self, role: str, record: Mapping[str, object], by_role: Mapping[str, ReleaseView],
    ) -> list[str]:
        prefix = f"{self.name}:{role}"
        try:
            receipt = SignoffReceipt.from_record(record)
        except ValueError as exc:
            return [f"{prefix}:{exc}"]
        problems = []
        if receipt.status != "passed":
            problems.append(f"{prefix}:status")
        if receipt.name != role:
            problems.append(f"{prefix}:receipt-role")
        if receipt.source_identity != self.source_identity:
            problems.append(f"{prefix}:source-identity")
        if receipt.subject != self.subject:
            problems.append(f"{prefix}:subject-identity")
        view = by_role[role]
        if receipt.variant != view.variant or canonical_json(dict(receipt.condition)) != canonical_json(dict(view.condition)):
            problems.append(f"{prefix}:condition-or-variant")
        if set(receipt.coverage) != set(self.receipts[role].coverage):
            problems.append(f"{prefix}:coverage")
        outputs = set(self.receipts[role].outputs)
        inputs = set(self.receipts[role].inputs)
        for field, values, expected in (("inputs", receipt.inputs, inputs), ("outputs", receipt.outputs, outputs)):
            if len(values) != len(expected) or {value.name for value in values} != expected:
                problems.append(f"{prefix}:{field}:receipt-roles")
            for value in values:
                view = by_role.get(value.name)
                if view is None or (value.size, value.sha256) != (view.size, view.sha256):
                    problems.append(f"{prefix}:{field}:artifact-identity:{value.name}")
                if view is not None:
                    variant_conflict = (receipt.variant is not None and view.variant is not None
                                        and receipt.variant != view.variant)
                    condition_conflict = any(
                        canonical_json(receipt.condition[key]) != canonical_json(view.condition[key])
                        for key in receipt.condition.keys() & view.condition.keys()
                    )
                    if variant_conflict or condition_conflict:
                        problems.append(f"{prefix}:{field}:applicability-conflict:{view.name}")
        return problems
