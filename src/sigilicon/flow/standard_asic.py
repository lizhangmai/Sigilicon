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
                ArtifactPort("qualification-spec", "spec.qualification"),
            ),
            adapters=("source-assets",),
            resolves_source_revision=True,
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
                ArtifactPort("reference-library", "library.synopsys-ndm"),
                ArtifactPort("library-check-report", "report.library-check"),
                ArtifactPort("execution-evidence", "evidence.tool-execution"),
            ),
            facts=("passed",),
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
                ArtifactPort("reference-library", "library.synopsys-ndm"),
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
                ArtifactPort("execution-evidence", "evidence.tool-execution"),
            ),
            facts=("passed",),
            required_capabilities=("tool.synopsys-fc",),
            platform_assets=(
                PlatformAssetRequirement(
                    "physical-technology",
                    "platform.physical-view-set",
                    members=("tluplus", "gds-layer-map"),
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
            ),
            outputs=(
                ArtifactPort("measurements", "measurement.collection"),
                ArtifactPort("waveforms", "waveform.collection"),
            ),
            facts=("passed",),
            required_capabilities=("tool.synopsys-hspice",),
            platform_assets=(
                PlatformAssetRequirement("hspice-models", "model.hspice-set"),
            ),
            adapters=("synopsys-hspice",),
        )
    )
    registry.register_action(
        ActionContract(
            kind="asic.electrical-qualification",
            inputs=(
                ArtifactPort("measurements", "measurement.collection"),
                ArtifactPort("qualification-spec", "spec.qualification"),
            ),
            outputs=(
                ArtifactPort("evidence", "evidence.qualification"),
            ),
            facts=("passed",),
            adapters=("synopsys-hspice",),
        )
    )


__all__ = ["register_standard_asic_actions"]
