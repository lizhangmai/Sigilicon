"""Reusable ActionContracts for standard-cell ASIC flow seams."""

from __future__ import annotations

from sigilicon.flow.evidence import FactKind, FactSchema, FactSpec
from sigilicon.flow.model import ActionContract, ArtifactPort, PlatformAssetRequirement
from sigilicon.flow.registry import FlowRegistry


def _schema(action_kind: str, *fields: FactSpec) -> FactSchema:
    return FactSchema(action_kind, fields)


def register_standard_asic_actions(registry: FlowRegistry) -> None:
    """Register tool-independent ASIC Actions without choosing project recipes."""

    registry.register_action(
        ActionContract(
            kind="design.source-assets",
            outputs=(
                ArtifactPort("rtl-sources", "source-set.systemverilog"),
                ArtifactPort("testbench", "source-set.systemverilog"),
                ArtifactPort("simulation-recipe", "recipe.simulation"),
                ArtifactPort("constraints", "constraints.sdc"),
                ArtifactPort("synthesis-recipe", "recipe.synthesis"),
                ArtifactPort(
                    "reference-library-recipe",
                    "recipe.reference-library",
                ),
                ArtifactPort(
                    "implementation-recipe",
                    "recipe.physical-implementation",
                ),
                ArtifactPort("electrical-sources", "source-set.spice"),
                ArtifactPort("decks", "source-set.spice-deck"),
                ArtifactPort(
                    "electrical-recipe",
                    "recipe.electrical-simulation",
                ),
                ArtifactPort("qualification-spec", "spec.qualification"),
            ),
            adapters=("source-assets",),
            resolves_source_assets=True,
        )
    )
    registry.register_action(
        ActionContract(
            kind="design.structural-link-assets",
            outputs=(
                ArtifactPort("rtl-sources", "source-set.systemverilog"),
                ArtifactPort(
                    "structural-link-recipe",
                    "recipe.structural-link",
                ),
            ),
            adapters=("source-assets",),
            resolves_source_assets=True,
        )
    )
    registry.register_action(
        ActionContract(
            kind="asic.rtl-simulation",
            inputs=(
                ArtifactPort("rtl-sources", "source-set.systemverilog"),
                ArtifactPort("testbench", "source-set.systemverilog"),
                ArtifactPort("simulation-recipe", "recipe.simulation"),
            ),
            outputs=(ArtifactPort("evidence", "evidence.simulation"),),
            required_capabilities=("tool.synopsys-vcs",),
            adapters=("synopsys-vcs",),
        )
    )
    registry.register_action(
        ActionContract(
            kind="asic.structural-elaboration",
            inputs=(
                ArtifactPort("rtl-sources", "source-set.systemverilog"),
                ArtifactPort("simulation-recipe", "recipe.simulation"),
            ),
            outputs=(ArtifactPort("evidence", "evidence.simulation"),),
            required_capabilities=("tool.synopsys-vcs",),
            platform_assets=(
                PlatformAssetRequirement(
                    "standard-cell-models",
                    "library.verilog-model-set",
                    members=("rvt", "hvt", "lvt"),
                ),
            ),
            adapters=("synopsys-vcs",),
        )
    )
    registry.register_action(
        ActionContract(
            kind="asic.synthesis",
            inputs=(
                ArtifactPort("rtl-sources", "source-set.systemverilog"),
                ArtifactPort("constraints", "constraints.sdc"),
                ArtifactPort("synthesis-recipe", "recipe.synthesis"),
            ),
            outputs=(
                ArtifactPort("mapped-netlist", "netlist.verilog"),
                ArtifactPort("mapped-constraints", "constraints.sdc"),
                ArtifactPort("checkpoint", "checkpoint.synopsys-ddc"),
                ArtifactPort("reports", "report.collection"),
            ),
            required_capabilities=("tool.synopsys-dc",),
            platform_assets=(
                PlatformAssetRequirement(
                    "standard-cell-timing",
                    "library.synopsys-db-set",
                    members=("rvt", "hvt", "lvt"),
                ),
            ),
            adapters=("synopsys-dc",),
        )
    )
    registry.register_action(
        ActionContract(
            kind="asic.structural-link",
            inputs=(
                ArtifactPort("rtl-sources", "source-set.systemverilog"),
                ArtifactPort(
                    "structural-link-recipe",
                    "recipe.structural-link",
                ),
            ),
            outputs=(
                ArtifactPort(
                    "compiled-macro-library",
                    "library.synopsys-db",
                ),
                ArtifactPort("checkpoint", "checkpoint.synopsys-ddc"),
                ArtifactPort("structural-report", "report.structure"),
                ArtifactPort("evidence", "evidence.tool-execution"),
            ),
            fact_schema=_schema(
                "asic.structural-link",
                FactSpec(
                    "evidence-role",
                    FactKind.TEXT,
                    enum_values=(
                        "diagnostic",
                        "regression",
                        "qualification",
                        "signoff",
                    ),
                ),
                FactSpec(
                    "evidence-level",
                    FactKind.TEXT,
                    enum_values=("l0", "l1", "l2", "l3", "l4"),
                ),
                FactSpec("evidence-scope", FactKind.TEXT),
                FactSpec("product-qualification-conclusion", FactKind.BOOLEAN),
                FactSpec("macro-instance-count", FactKind.INTEGER, unit="count"),
                FactSpec(
                    "unresolved-reference-count",
                    FactKind.INTEGER,
                    unit="count",
                ),
                FactSpec("timing-characterized", FactKind.BOOLEAN),
                FactSpec("power-characterized", FactKind.BOOLEAN),
                FactSpec("area-characterized", FactKind.BOOLEAN),
            ),
            required_capabilities=(
                "tool.synopsys-library-compiler",
                "tool.synopsys-dc",
            ),
            adapters=("synopsys-structural-link",),
        )
    )
    registry.register_action(
        ActionContract(
            kind="asic.gate-simulation",
            inputs=(
                ArtifactPort("mapped-netlist", "netlist.verilog"),
                ArtifactPort("testbench", "source-set.systemverilog"),
                ArtifactPort("simulation-recipe", "recipe.simulation"),
            ),
            outputs=(ArtifactPort("evidence", "evidence.simulation"),),
            required_capabilities=("tool.synopsys-vcs",),
            platform_assets=(
                PlatformAssetRequirement(
                    "standard-cell-models",
                    "library.verilog-model-set",
                    members=("rvt", "hvt", "lvt"),
                ),
            ),
            adapters=("synopsys-vcs",),
        )
    )
    registry.register_action(
        ActionContract(
            kind="asic.reference-library-construction",
            inputs=(
                ArtifactPort(
                    "reference-library-recipe",
                    "recipe.reference-library",
                ),
            ),
            outputs=(
                ArtifactPort(
                    "reference-library",
                    "library.synopsys-ndm",
                ),
                ArtifactPort("library-check-report", "report.library-check"),
                ArtifactPort("execution-evidence", "evidence.tool-execution"),
            ),
            fact_schema=_schema(
                "asic.reference-library-construction",
                FactSpec("library-check-succeeded", FactKind.BOOLEAN),
                FactSpec(
                    "library-check-error-count",
                    FactKind.INTEGER,
                    unit="count",
                ),
                FactSpec(
                    "library-check-warning-count",
                    FactKind.INTEGER,
                    unit="count",
                ),
            ),
            required_capabilities=("tool.synopsys-library-manager",),
            platform_assets=(
                PlatformAssetRequirement(
                    "physical-technology",
                    "platform.physical-view-set",
                    members=("technology-file", "technology-lef"),
                ),
                PlatformAssetRequirement(
                    "standard-cell-physical",
                    "library.lef-set",
                    members=("rvt", "hvt", "lvt"),
                ),
                PlatformAssetRequirement(
                    "standard-cell-timing",
                    "library.synopsys-db-set",
                    members=("rvt", "hvt", "lvt"),
                ),
            ),
            adapters=("synopsys-fc",),
        )
    )
    registry.register_action(
        ActionContract(
            kind="asic.physical-implementation",
            inputs=(
                ArtifactPort("mapped-netlist", "netlist.verilog"),
                ArtifactPort("mapped-constraints", "constraints.sdc"),
                ArtifactPort(
                    "implementation-recipe",
                    "recipe.physical-implementation",
                ),
                ArtifactPort(
                    "reference-library",
                    "library.synopsys-ndm",
                ),
            ),
            outputs=(
                ArtifactPort("routed-netlist", "netlist.verilog"),
                ArtifactPort("routed-constraints", "constraints.sdc"),
                ArtifactPort("layout-stream", "layout.gds"),
                ArtifactPort("checkpoint", "checkpoint.synopsys-dlib"),
                ArtifactPort("design-check-report", "report.design-check"),
                ArtifactPort("structural-report", "report.structure"),
                ArtifactPort("qor-report", "report.qor"),
                ArtifactPort("timing-report", "report.timing"),
                ArtifactPort("area-report", "report.area"),
                ArtifactPort("power-report", "report.power"),
                ArtifactPort("drc-report", "report.drc"),
                ArtifactPort(
                    "physical-completion-report",
                    "report.physical-completion",
                ),
                ArtifactPort("tie-off-check-report", "report.tie-off-check"),
                ArtifactPort("execution-evidence", "evidence.tool-execution"),
            ),
            fact_schema=_schema(
                "asic.physical-implementation",
                FactSpec(
                    "design-check-error-count",
                    FactKind.INTEGER,
                    unit="count",
                ),
                FactSpec(
                    "design-check-warning-count",
                    FactKind.INTEGER,
                    unit="count",
                ),
                FactSpec("open-net-count", FactKind.INTEGER, unit="count"),
                FactSpec(
                    "route-drc-violation-count",
                    FactKind.INTEGER,
                    unit="count",
                ),
                FactSpec("worst-setup-slack-ns", FactKind.REAL, unit="ns"),
                FactSpec("worst-hold-slack-ns", FactKind.REAL, unit="ns"),
                FactSpec(
                    "max-transition-violation-count",
                    FactKind.INTEGER,
                    unit="count",
                ),
                FactSpec(
                    "max-capacitance-violation-count",
                    FactKind.INTEGER,
                    unit="count",
                ),
                FactSpec(
                    "physical-cell-area-um2",
                    FactKind.REAL,
                    unit="um2",
                ),
                FactSpec("leaf-cell-count", FactKind.INTEGER, unit="count"),
                FactSpec("power-activity-mode", FactKind.TEXT),
                FactSpec(
                    "total-dynamic-power-nw",
                    FactKind.REAL,
                    unit="nW",
                ),
                FactSpec(
                    "cell-leakage-power-nw",
                    FactKind.REAL,
                    unit="nW",
                ),
                FactSpec("antenna-check-active", FactKind.BOOLEAN),
                FactSpec(
                    "antenna-check-status",
                    FactKind.TEXT,
                    enum_values=("active", "no-rules", "inactive"),
                ),
                FactSpec("tie-to-rail-check-performed", FactKind.BOOLEAN),
                FactSpec(
                    "tie-to-rail-check-status",
                    FactKind.TEXT,
                    enum_values=("performed", "not-performed"),
                ),
                FactSpec("tie-off-check-performed", FactKind.BOOLEAN),
                FactSpec(
                    "tie-off-check-status",
                    FactKind.TEXT,
                    enum_values=("performed",),
                ),
                FactSpec(
                    "tie-off-violation-count",
                    FactKind.INTEGER,
                    unit="count",
                ),
                FactSpec(
                    "required-pg-port-count",
                    FactKind.INTEGER,
                    unit="count",
                ),
                FactSpec(
                    "placed-required-pg-port-count",
                    FactKind.INTEGER,
                    unit="count",
                ),
                FactSpec(
                    "unplaced-required-pg-port-count",
                    FactKind.INTEGER,
                    unit="count",
                ),
                FactSpec("pg-connectivity-check-performed", FactKind.BOOLEAN),
                FactSpec(
                    "pg-connectivity-check-status",
                    FactKind.TEXT,
                    enum_values=("performed", "not-performed"),
                ),
                FactSpec(
                    "antenna-violation-count",
                    FactKind.INTEGER,
                    required=False,
                    unit="count",
                ),
                FactSpec(
                    "tie-to-rail-violation-count",
                    FactKind.INTEGER,
                    required=False,
                    unit="count",
                ),
                FactSpec(
                    "tie-to-rail-direct-violation-count",
                    FactKind.INTEGER,
                    required=False,
                    unit="count",
                ),
                FactSpec(
                    "pg-connectivity-violation-count",
                    FactKind.INTEGER,
                    required=False,
                    unit="count",
                ),
            ),
            required_capabilities=("tool.synopsys-fc",),
            platform_assets=(
                PlatformAssetRequirement(
                    "physical-technology",
                    "platform.physical-view-set",
                    members=("tluplus", "gds-layer-map", "antenna-rules"),
                ),
            ),
            adapters=("synopsys-fc",),
        )
    )
    registry.register_action(
        ActionContract(
            kind="asic.electrical-functional",
            inputs=(
                ArtifactPort("electrical-sources", "source-set.spice"),
                ArtifactPort("decks", "source-set.spice-deck"),
                ArtifactPort(
                    "electrical-recipe",
                    "recipe.electrical-simulation",
                ),
            ),
            outputs=(
                ArtifactPort("measurements", "measurement.collection"),
                ArtifactPort("waveforms", "waveform.collection", required=False),
            ),
            fact_schema=_schema(
                "asic.electrical-functional",
                FactSpec("measurement-row-count", FactKind.INTEGER, unit="count"),
                FactSpec(
                    "measurement-failure-count",
                    FactKind.INTEGER,
                    unit="count",
                ),
                FactSpec(
                    "measurement-check-failure-count",
                    FactKind.INTEGER,
                    unit="count",
                ),
            ),
            required_capabilities=("tool.synopsys-hspice",),
            platform_assets=(
                PlatformAssetRequirement(
                    "hspice-models",
                    "model.hspice-set",
                    members=("nominal-model", "rvt", "hvt", "lvt"),
                ),
            ),
            adapters=("synopsys-hspice",),
        )
    )
    registry.register_action(
        ActionContract(
            kind="asic.electrical-diagnostic",
            inputs=(
                ArtifactPort("electrical-sources", "source-set.spice"),
                ArtifactPort("decks", "source-set.spice-deck"),
                ArtifactPort(
                    "electrical-recipe",
                    "recipe.electrical-simulation",
                ),
            ),
            outputs=(
                ArtifactPort("evidence", "evidence.electrical-diagnostic"),
            ),
            fact_schema=_schema(
                "asic.electrical-diagnostic",
                FactSpec(
                    "evidence-role",
                    FactKind.TEXT,
                    enum_values=(
                        "diagnostic",
                        "regression",
                        "qualification",
                        "signoff",
                    ),
                ),
                FactSpec("product-qualification-conclusion", FactKind.BOOLEAN),
            ),
            required_capabilities=("tool.synopsys-hspice",),
            platform_assets=(
                PlatformAssetRequirement(
                    "hspice-models",
                    "model.hspice-set",
                    members=(
                        "nominal-model",
                        "mismatch-model",
                        "rvt",
                        "hvt",
                        "lvt",
                    ),
                ),
            ),
            adapters=("synopsys-hspice",),
        )
    )
    registry.register_action(
        ActionContract(
            kind="asic.electrical-model-variant-diagnostic",
            inputs=(
                ArtifactPort("electrical-sources", "source-set.spice"),
                ArtifactPort("decks", "source-set.spice-deck"),
                ArtifactPort(
                    "electrical-recipe",
                    "recipe.electrical-simulation",
                ),
            ),
            outputs=(
                ArtifactPort("evidence", "evidence.electrical-diagnostic"),
            ),
            fact_schema=_schema(
                "asic.electrical-model-variant-diagnostic",
                FactSpec(
                    "evidence-role",
                    FactKind.TEXT,
                    enum_values=(
                        "diagnostic",
                        "regression",
                        "qualification",
                        "signoff",
                    ),
                ),
                FactSpec("product-qualification-conclusion", FactKind.BOOLEAN),
            ),
            required_capabilities=("tool.synopsys-hspice",),
            platform_assets=(
                PlatformAssetRequirement(
                    "hspice-models",
                    "model.hspice-set",
                    members=(
                        "nominal-model",
                        "mismatch-model",
                        "rvt",
                        "hvt",
                        "lvt",
                        "stdcell-12t-rvt",
                    ),
                ),
            ),
            adapters=("synopsys-hspice",),
        )
    )
    registry.register_action(
        ActionContract(
            kind="asic.electrical-campaign",
            inputs=(
                ArtifactPort("electrical-sources", "source-set.spice"),
                ArtifactPort("decks", "source-set.spice-deck"),
                ArtifactPort(
                    "electrical-recipe",
                    "recipe.electrical-simulation",
                ),
            ),
            outputs=(
                ArtifactPort(
                    "campaign-summary",
                    "report.electrical-campaign",
                ),
            ),
            fact_schema=_schema(
                "asic.electrical-campaign",
                FactSpec("campaign-record-count", FactKind.INTEGER, unit="count"),
            ),
            required_capabilities=("tool.synopsys-hspice",),
            platform_assets=(
                PlatformAssetRequirement(
                    "hspice-models",
                    "model.hspice-set",
                    members=(
                        "nominal-model",
                        "mismatch-model",
                        "rvt",
                        "hvt",
                        "lvt",
                    ),
                ),
            ),
            adapters=("synopsys-hspice",),
        )
    )
    registry.register_action(
        ActionContract(
            kind="asic.electrical-qualification",
            inputs=(
                ArtifactPort(
                    "campaign-summary",
                    "report.electrical-campaign",
                ),
                ArtifactPort("qualification-spec", "spec.qualification"),
            ),
            outputs=(
                ArtifactPort("evidence", "evidence.qualification"),
            ),
            fact_schema=_schema(
                "asic.electrical-qualification",
                FactSpec("passed", FactKind.BOOLEAN),
                FactSpec(
                    "qualification-failure-count",
                    FactKind.INTEGER,
                    unit="count",
                ),
            ),
            adapter_extensible=True,
        )
    )


__all__ = ["register_standard_asic_actions"]
