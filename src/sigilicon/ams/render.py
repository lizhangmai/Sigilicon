"""Render simulator inputs from an :class:`AmsSpec`."""

from __future__ import annotations

from sigilicon.ams.provenance import ams_fingerprint
from sigilicon.ams.spec import AmsSpec


def _concat(names: tuple[str, ...]) -> str:
    return "{" + ", ".join(names) + "}"


def _literal(bits: tuple[int, ...]) -> str:
    return f"{len(bits)}'b{''.join(map(str, bits))}"


def render_testbench(
    spec: AmsSpec,
    *,
    dut_cell: str,
    include_supplies: bool,
    dump_vcd: bool,
) -> str:
    """Generate the common vector checker for standalone Xrun or ADE."""

    design = spec.design
    declarations = [f"    logic {name};" for name in design.inputs + design.outputs]
    if include_supplies:
        declarations.extend(
            (
                f"    supply1 {design.primary_supply};",
                f"    supply0 {design.ground_supply};",
            )
        )
    connections = (
        design.inputs
        + design.outputs
        + (design.supplies if include_supplies else ())
    )
    port_map = ",\n".join(f"        .{name}({name})" for name in connections)
    vector_calls = "\n".join(
        f"        check_vector({index}, {_literal(vector.inputs)}, {_literal(vector.expected)});"
        for index, vector in enumerate(spec.vectors)
    )
    dump_lines = ""
    if dump_vcd:
        dump_lines = (
            f'        $dumpfile("{spec.testbench}.vcd");\n'
            f"        $dumpvars(0, {spec.testbench});\n\n"
        )
    inputs_expr = _concat(design.inputs)
    outputs_expr = _concat(design.outputs)
    fingerprint = ams_fingerprint(spec)
    return f"""`timescale 1ns/1ps

module {spec.testbench};
    localparam time SETTLE = {spec.simulation.checker.settle};

{chr(10).join(declarations)}
    int unsigned failed = 0;

    {dut_cell} XDUT (
{port_map}
    );

    task automatic check_vector(
        input int unsigned vector,
        input logic [{len(design.inputs) - 1}:0] stimulus,
        input logic [{len(design.outputs) - 1}:0] expected
    );
        {inputs_expr} = stimulus;
        #(SETTLE);
        if ({outputs_expr} === expected) begin
            $display(
                "TRUTH vector=%0d inputs=%0h expected=%0h observed=%0h pass=1",
                vector, stimulus, expected, {outputs_expr}
            );
        end else begin
            failed++;
            $display(
                "TRUTH vector=%0d inputs=%0h expected=%0h observed=%0h pass=0",
                vector, stimulus, expected, {outputs_expr}
            );
        end
    endtask

    initial begin
{dump_lines}        $display("FLOW_FINGERPRINT {fingerprint}");
{vector_calls}
        $display("SUMMARY vectors={len(spec.vectors)} failed=%0d", failed);
        if (failed != 0) begin
            $fatal(1, "AMS functional verification failed");
        end
        #1ns;
        $finish;
    end
endmodule
"""


def render_reference_module(spec: AmsSpec) -> str:
    """Generate the digital reference shell for the analog wrapper."""

    design = spec.design
    ports = design.inputs + design.outputs
    declarations = [*(f"    input {name};" for name in design.inputs)]
    declarations.extend(f"    output {name};" for name in design.outputs)
    return (
        f"module {spec.wrapper_cell} ({', '.join(ports)});\n"
        + "\n".join(declarations)
        + "\nendmodule\n"
    )


def render_analog_netlist(
    spec: AmsSpec,
    *,
    reference_file: str,
    model_file: str | None = None,
    source_netlist: str | None = None,
) -> str:
    """Generate the Spectre wrapper, supplies, analysis, and AMSD mapping."""

    timing = spec.simulation.timing
    interface = spec.simulation.interface
    design = spec.design
    model_path = str(model_file or design.pdk.model_file).replace('"', '\\"')
    source_path = str(source_netlist or design.source_netlist).replace('"', '\\"')
    return f"""simulator lang=spectre

include "{model_path}" section={design.pdk.model_section}
simulator lang=spectre
include "{source_path}"
global 0

{render_load_wrapper_netlist(spec, include_language=False)}

tran tran stop={timing.stop} maxstep={timing.maxstep}

amsd {{
    portmap subckt={spec.wrapper_cell} reffile="{reference_file}" refformat=verilog porttype=name
    config cell={spec.wrapper_cell} use=spice
    ie vsup={interface.vdd:g} discipline=logic tr={interface.rise_time} tf={interface.rise_time} vthi={interface.vthi:g} vtlo={interface.vtlo:g} connrules="{interface.connect_rules}"
}}
"""


def render_load_wrapper_netlist(
    spec: AmsSpec,
    *,
    include_language: bool = True,
) -> str:
    """Render the backend-neutral DUT supply and output-load wrapper."""

    design = spec.design
    interface = spec.simulation.interface
    wrapper_ports = design.inputs + design.outputs
    dut_connections = tuple(
        "0" if port == design.ground_supply else port for port in design.port_order
    )
    loads = "\n".join(
        f"CLOAD{index} ({output} 0) capacitor c={interface.load_cap}"
        for index, output in enumerate(design.outputs)
    )
    language = "simulator lang=spectre\n\n" if include_language else ""
    return f"""{language}// Common standalone/ADE wrapper owns supplies and loads.
subckt {spec.wrapper_cell} {' '.join(wrapper_ports)}
VVDD ({design.primary_supply} 0) vsource dc={interface.vdd:g} type=dc
XDUT ({' '.join(dut_connections)}) {design.cell}
{loads}
ends {spec.wrapper_cell}
"""
