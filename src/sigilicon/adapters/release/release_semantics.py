"""One capability and maturity policy for source plans and immutable packages."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Callable, Mapping

from sigilicon.canonical import canonical_digest
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


@dataclass(frozen=True)
class ReceiptArtifact:
    """Content identity addressed by export-local role, independent of package paths."""

    role: str
    size: int
    sha256: str

    @classmethod
    def from_record(cls, row: Mapping[str, object]) -> ReceiptArtifact:
        if not isinstance(row, Mapping) or set(row) != {"role", "size", "sha256"}:
            raise ValueError("receipt artifact requires role, size and sha256")
        if not isinstance(row["role"], str) or not row["role"]:
            raise ValueError("receipt artifact role is invalid")
        if type(row["size"]) is not int or row["size"] < 0:
            raise ValueError("receipt artifact size is invalid")
        if not isinstance(row["sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", row["sha256"]) is None:
            raise ValueError("receipt artifact sha256 is invalid")
        return cls(row["role"], row["size"], row["sha256"])

    @property
    def record(self) -> dict[str, object]:
        return {"role": self.role, "size": self.size, "sha256": self.sha256}


@dataclass(frozen=True)
class SignoffReceipt:
    role: str
    status: str
    source_identity: str
    subject: Mapping[str, str]
    execution: Mapping[str, object]
    tool_name: str
    tool_version: str
    inputs: tuple[ReceiptArtifact, ...]
    outputs: tuple[ReceiptArtifact, ...]

    @classmethod
    def from_record(cls, row: Mapping[str, object]) -> SignoffReceipt:
        fields = {"schema", "contract_kind", "role", "status", "source_identity", "subject", "execution", "tool", "inputs", "outputs"}
        if not isinstance(row, Mapping) or set(row) != fields:
            raise ValueError("invalid signoff receipt envelope")
        if type(row["schema"]) is not int or row["schema"] != 2 or row["contract_kind"] != "release-receipt":
            raise ValueError("invalid signoff receipt schema")
        if not isinstance(row["role"], str) or not row["role"] or not isinstance(row["status"], str):
            raise ValueError("invalid signoff receipt role or status")
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
        return cls(row["role"], row["status"], row["source_identity"], dict(subject), dict(execution), tool["name"], tool["version"], *bindings)


@dataclass(frozen=True)
class ReleaseView:
    role: str
    format: str
    capabilities: frozenset[str]
    size: int
    sha256: str
    library: str | None = None
    cell: str | None = None
    view: str | None = None
    corner: str | None = None

    @classmethod
    def from_source(cls, item: ReleaseCollateralRecord) -> ReleaseView:
        return cls(item.role, item.format, frozenset(item.capabilities),
                   item.size, item.sha256,
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
        identity = ReceiptArtifact.from_record({key: row.get(key) for key in ("role", "size", "sha256")})
        return cls(row["role"], row["format"], frozenset(capabilities), identity.size, identity.sha256,
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
    subject: Mapping[str, str] = field(default_factory=dict)
    receipts: Mapping[str, ReceiptPolicy] = field(default_factory=dict)

    @classmethod
    def from_source(cls, exported: IpExport, level: str, collateral: tuple[ReleaseCollateralRecord, ...]) -> ExportSemantics:
        interface = exported.interface
        rtl = isinstance(interface, RtlIpInterface)
        return cls(
            exported.name, interface.kind, exported.required_roles[level],
            tuple(ReleaseView.from_source(item) for item in collateral if item.export == exported.name),
            interface.source_role if rtl else None,
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
        subject = ({"kind": kind, "module": interface.get("module"),
                    **({"variant": interface["variant"]} if "variant" in interface else {})}
                   if kind == "rtl" else {"kind": kind, **dict(exported["oa"])})
        return cls(name, kind, tuple(required), selected, interface.get("source_role"), oa,
                   subject, receipt_policies(exported.get("receipts", {})))

    def assess(
        self, level: str,
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
        if level == "signoff" and not self.receipts:
            problems.append(f"{self.name}:missing-receipt-policy")
        for view in self.views:
            if "signoff" in view.capabilities and view.role not in self.receipts:
                problems.append(f"{self.name}:{view.role}:missing-receipt-policy")
        for role in self.receipts:
            if level != "signoff" and role not in self.required_roles:
                continue
            if role not in by_role:
                problems.append(f"{self.name}:{role}")
                continue
            try:
                receipt = read_receipt(role)
            except (OSError, ValueError) as exc:
                problems.append(f"{self.name}:{role}:invalid-json:{exc}")
                continue
            problems.extend(self._receipt_problems(role, receipt, by_role))

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
                "role": view.role, "size": view.size, "sha256": view.sha256,
                "format": view.format, "capabilities": sorted(view.capabilities),
                "library": view.library, "cell": view.cell, "view": view.view, "corner": view.corner,
            } for view in sorted(self.views, key=lambda item: item.role) if view.role not in self.receipts],
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
        if receipt.role != role:
            problems.append(f"{prefix}:receipt-role")
        if receipt.source_identity != self.source_identity:
            problems.append(f"{prefix}:source-identity")
        if receipt.subject != self.subject:
            problems.append(f"{prefix}:subject-identity")
        outputs = set(self.receipts[role].outputs)
        inputs = set(self.receipts[role].inputs)
        for field, values, expected in (("inputs", receipt.inputs, inputs), ("outputs", receipt.outputs, outputs)):
            if len(values) != len(expected) or {value.role for value in values} != expected:
                problems.append(f"{prefix}:{field}:receipt-roles")
            for value in values:
                view = by_role.get(value.role)
                if view is None or (value.size, value.sha256) != (view.size, view.sha256):
                    problems.append(f"{prefix}:{field}:artifact-identity:{value.role}")
        return problems
