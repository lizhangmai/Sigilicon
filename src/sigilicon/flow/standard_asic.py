"""Reusable ActionContracts for standard-cell ASIC flow seams."""

from __future__ import annotations

from sigilicon.flow.model import ActionContract, ArtifactPort, PlatformAssetRequirement
from sigilicon.flow.registry import FlowRegistry


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
            facts=("passed",),
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
            facts=("passed",),
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
            facts=("passed",),
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
            facts=(
                "passed",
                "evidence-role",
                "evidence-level",
                "evidence-scope",
                "product-qualification-conclusion",
                "macro-instance-count",
                "unresolved-reference-count",
                "timing-characterized",
                "power-characterized",
                "area-characterized",
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
            facts=("passed",),
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
            facts=(
                "tool-execution-completed",
                "library-check-succeeded",
                "library-check-error-count",
                "library-check-warning-count",
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
            facts=(
                "tool-execution-completed",
                "design-check-error-count",
                "design-check-warning-count",
                "open-net-count",
                "route-drc-violation-count",
                "worst-setup-slack-ns",
                "worst-hold-slack-ns",
                "max-transition-violation-count",
                "max-capacitance-violation-count",
                "physical-cell-area-um2",
                "leaf-cell-count",
                "power-activity-mode",
                "total-dynamic-power-nw",
                "cell-leakage-power-nw",
                "antenna-check-active",
                "antenna-check-status",
                "tie-to-rail-check-performed",
                "tie-to-rail-check-status",
                "tie-off-check-performed",
                "tie-off-check-status",
                "tie-off-violation-count",
                "required-pg-port-count",
                "placed-required-pg-port-count",
                "unplaced-required-pg-port-count",
                "pg-connectivity-check-performed",
                "pg-connectivity-check-status",
            ),
            optional_facts=(
                "antenna-violation-count",
                "tie-to-rail-violation-count",
                "tie-to-rail-direct-violation-count",
                "pg-connectivity-violation-count",
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
            facts=(
                "tool-execution-completed",
                "measurement-file-count",
                "measurement-row-count",
                "measurement-failure-count",
                "measurement-check-failure-count",
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
            facts=(
                "tool-execution-completed",
                "evidence-role",
                "product-qualification-conclusion",
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
            facts=(
                "tool-execution-completed",
                "evidence-role",
                "product-qualification-conclusion",
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
            facts=(
                "tool-execution-completed",
                "campaign-record-count",
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
            facts=("passed", "qualification-failure-count"),
            adapter_extensible=True,
        )
    )


__all__ = ["register_standard_asic_actions"]
