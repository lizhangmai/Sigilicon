"""Materialize and compare source-defined testbench schematic parameters."""

from __future__ import annotations

from collections.abc import Mapping
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from sigilicon.adapters.cadence.oa_library import OALibraryRebuildPlan, TestbenchRebuildStep
from sigilicon.domain.netlist import (
    NetlistSnapshot,
    parse_subcircuit_definitions,
    parse_subcircuit_instances,
    parse_spectre_pwl_sources,
)
from sigilicon.virtuoso.schematic import read_schematic, set_instance_parameters
from sigilicon.virtuoso.workspace import OperationPolicy, workspace_operation


_OA_PRIMITIVE_MASTER_ALIASES = {
    "capacitor": "cap",
    "resistor": "res",
}
_OA_PRIMITIVE_TERMINAL_ORDERS = {
    "cap": ("PLUS", "MINUS"),
    "res": ("PLUS", "MINUS"),
    "vsource": ("PLUS", "MINUS"),
    "vcvs": ("PLUS", "MINUS", "NC+", "NC-"),
    "vccs": ("PLUS", "MINUS", "NC+", "NC-"),
}
_OA_SYNTHETIC_GROUND_MASTERS = frozenset({"gnd"})
_OA_GROUND_NET_NAMES = frozenset({"0", "gnd", "gnd!"})
_OA_NUMERIC_VALUE = re.compile(
    r"^(?P<mantissa>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
    r"(?P<suffix>[afpnumkKMGTP])?$"
)
_OA_NUMERIC_SUFFIXES = {
    "a": Decimal("1e-18"),
    "f": Decimal("1e-15"),
    "p": Decimal("1e-12"),
    "n": Decimal("1e-9"),
    "u": Decimal("1e-6"),
    "m": Decimal("1e-3"),
    "k": Decimal("1e3"),
    "K": Decimal("1e3"),
    "M": Decimal("1e6"),
    "G": Decimal("1e9"),
    "T": Decimal("1e12"),
    "P": Decimal("1e15"),
}
_OA_SOURCE_PARAMETER_ALIASES = {
    "type": "srcType",
    "dc": "vdc",
    "delay": "td",
    "rise": "tr",
    "fall": "tf",
    "width": "pw",
    "period": "per",
}
_OA_CONTROLLED_SOURCE_PARAMETER_ALIASES = {
    "vcvs": {"gain": "egain"},
    "vccs": {"gm": "ggain"},
}


def _oa_master_name(master: str) -> str:
    lowered = master.lower()
    return _OA_PRIMITIVE_MASTER_ALIASES.get(
        lowered,
        lowered if lowered in _OA_PRIMITIVE_TERMINAL_ORDERS else master,
    )


def _oa_net_name(net: str) -> str:
    return "0" if net.lower() in _OA_GROUND_NET_NAMES else net


def _normalize_oa_parameter_value(value: object) -> str:
    """Normalize numeric Spectre/CDF spellings without evaluating expressions."""

    text = str(value).strip().strip('"')
    match = _OA_NUMERIC_VALUE.fullmatch(text)
    if match is None:
        return " ".join(text.split())
    try:
        number = Decimal(match.group("mantissa"))
        if match.group("suffix"):
            number *= _OA_NUMERIC_SUFFIXES[match.group("suffix")]
    except (InvalidOperation, KeyError):
        return " ".join(text.split())
    if not number:
        return "0"
    scientific = format(number.normalize(), "e")
    mantissa, exponent = scientific.split("e")
    mantissa = mantissa.rstrip("0").rstrip(".")
    return f"{mantissa}e{int(exponent)}"


def _source_parameter_assignments(instance: Any) -> dict[str, str]:
    assignments: dict[str, str] = {}
    for token in instance.parameters:
        name, separator, value = token.partition("=")
        if separator and name and value and name.lower() != "wave":
            assignments[name.lower()] = value
    return assignments


def _platform_parameter_aliases(
    plan: OALibraryRebuildPlan,
    master: str,
) -> Mapping[str, str]:
    """Return source-to-CDF names declared by the selected platform contract."""

    for document in plan.platform_documents.values():
        custom_layout = document.get("custom_layout")
        if not isinstance(custom_layout, Mapping):
            continue
        mom = custom_layout.get("mom_pcell")
        if not isinstance(mom, Mapping):
            continue
        if str(mom.get("cell_name", "")).lower() != master.lower():
            continue
        fields = (
            ("w", "finger_width_parameter"),
            ("s", "finger_spacing_parameter"),
            ("lr", "finger_length_parameter"),
            ("nr", "finger_count_parameter"),
            ("stm", "start_metal_parameter"),
            ("spm", "stop_metal_parameter"),
            ("multi", "multiplicity_parameter"),
        )
        return {
            source_name: str(mom[target_name])
            for source_name, target_name in fields
            if isinstance(mom.get(target_name), str) and mom[target_name]
        }
    return {}


def _expected_instance_parameters(
    instance: Any,
    pwl_sources: Mapping[str, Any],
    *,
    parameter_aliases: Mapping[str, str],
) -> Mapping[str, str]:
    """Translate source parameters to the CDF names exposed by the reader."""

    master = instance.master.lower()
    assignments = _source_parameter_assignments(instance)
    if master in _OA_CONTROLLED_SOURCE_PARAMETER_ALIASES:
        aliases = _OA_CONTROLLED_SOURCE_PARAMETER_ALIASES[master]
        return {aliases.get(name, name): value for name, value in assignments.items()}
    if master == "vsource":
        source_type = assignments.get("type", "dc").lower()
        expected = {
            _OA_SOURCE_PARAMETER_ALIASES.get(name, name): value
            for name, value in assignments.items()
        }
        expected["srcType"] = source_type
        if source_type == "pwl":
            source = pwl_sources.get(instance.name)
            if source is None:
                raise ValueError(
                    f"PWL source is missing from canonical netlist: {instance.name}"
                )
            expected.update({
                "srcType": "pwl",
                "pwlEntryMethod": "Voltage/Time points",
                "tvpairs": str(len(source.points)),
            })
            for index, (time, value) in enumerate(source.points, start=1):
                expected[f"t{index}"] = time
                expected[f"v{index}"] = value
        return expected
    return {
        parameter_aliases.get(name, name): value
        for name, value in assignments.items()
    }


def _expected_instance_terminals(
    instance: Any,
    definitions: Mapping[str, Any],
    primitive_orders: Mapping[str, Any],
) -> Mapping[str, str] | None:
    definition = definitions.get(instance.master)
    ports = (
        definition.ports
        if definition is not None
        else primitive_orders.get(
            instance.master, _OA_PRIMITIVE_TERMINAL_ORDERS.get(instance.master.lower())
        )
    )
    if ports is None:
        return None
    if len(ports) != len(instance.nodes):
        raise ValueError(
            f"instance {instance.name} has {len(instance.nodes)} source terminals, "
            f"but master {instance.master} declares {len(ports)}"
        )
    return {
        port: _oa_net_name(node)
        for port, node in zip(ports, instance.nodes, strict=True)
    }


def restore_source_parameters(
    snapshot: NetlistSnapshot,
    client: Any,
    *,
    library: str,
    cell: str,
    operation: Any,
) -> None:
    """Restore source values that spiceIn rounds or leaves at CDF defaults."""

    definition = parse_subcircuit_definitions((snapshot,))[cell]
    pwl_sources = {
        source.instance: source
        for source in parse_spectre_pwl_sources(snapshot, cell)
    }
    for instance in parse_subcircuit_instances(definition):
        if instance.master.lower() not in {"vsource", "vcvs", "vccs"}:
            continue
        parameters = _expected_instance_parameters(instance, pwl_sources, parameter_aliases={})
        if parameters:
            set_instance_parameters(
                client, library, cell, instance.name, parameters,
                operation=operation, invoke_callbacks=False,
            )


def _testbench_schematic_parity(
    plan: OALibraryRebuildPlan,
    step: TestbenchRebuildStep,
    client: Any,
    *,
    operation: Any,
    timeout: int,
) -> dict[str, object]:
    """Compare the live testbench schematic topology with its source netlist."""

    snapshots = {step.source_snapshot.source_path: step.source_snapshot}
    for path, snapshot in plan.netlist_snapshots.items():
        snapshots.setdefault(path, snapshot)
    definitions = parse_subcircuit_definitions(tuple(snapshots.values()))
    try:
        definition = definitions[step.cell]
    except KeyError as exc:
        raise ValueError(
            f"testbench source does not define its declared cell: {step.cell}"
        ) from exc
    expected_instances = parse_subcircuit_instances(definition)
    primitive_orders = {
        name: ports
        for document in plan.platform_documents.values()
        for name, ports in document.get("primitive_subcircuits", {}).items()
    }
    pwl_sources = {
        source.instance: source
        for source in parse_spectre_pwl_sources(step.source_snapshot, step.cell)
    }
    actual = read_schematic(
        client,
        plan.library,
        step.cell,
        include_positions=False,
        timeout=timeout,
        operation=operation,
    )
    if not isinstance(actual, Mapping):
        raise ValueError("OA schematic reader returned a non-mapping result")
    actual_rows = actual.get("instances")
    if not isinstance(actual_rows, (tuple, list)):
        raise ValueError("OA schematic reader returned no instance inventory")

    expected_by_name = {
        instance.name: {
            "master": _oa_master_name(instance.master),
            "nodes": tuple(sorted(_oa_net_name(node) for node in instance.nodes)),
            "terminals": _expected_instance_terminals(instance, definitions, primitive_orders),
            "parameters": _expected_instance_parameters(
                instance,
                pwl_sources,
                parameter_aliases=_platform_parameter_aliases(plan, instance.master),
            ),
        }
        for instance in expected_instances
    }
    actual_by_name: dict[str, dict[str, object]] = {}
    for row in actual_rows:
        if not isinstance(row, Mapping):
            raise ValueError("OA schematic reader returned an invalid instance row")
        name = row.get("name")
        master = row.get("cell")
        terms = row.get("terms")
        parameters = row.get("params", {})
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(master, str)
            or not master
            or not isinstance(terms, Mapping)
            or not terms
            or not isinstance(parameters, Mapping)
            or any(
                not isinstance(pin, str) or not pin
                or not isinstance(net, str) or not net
                for pin, net in terms.items()
            )
        ):
            raise ValueError("OA schematic reader returned an incomplete instance row")
        observed_nodes = tuple(
            sorted(_oa_net_name(str(net)) for net in terms.values())
        )
        if (
            master.lower() in _OA_SYNTHETIC_GROUND_MASTERS
            and all(net == "0" for net in observed_nodes)
        ):
            continue
        if name in actual_by_name:
            raise ValueError(f"OA schematic reader returned duplicate instance: {name}")
        actual_by_name[name] = {
            "master": _oa_master_name(master),
            "nodes": observed_nodes,
            "terminals": {
                str(pin): _oa_net_name(str(net)) for pin, net in terms.items()
            },
            "parameters": {
                str(parameter): _normalize_oa_parameter_value(value)
                for parameter, value in parameters.items()
            },
        }

    missing_instances = sorted(set(expected_by_name) - set(actual_by_name))
    extra_instances = sorted(set(actual_by_name) - set(expected_by_name))
    master_mismatches = [
        {
            "instance": name,
            "expected": expected_by_name[name]["master"],
            "observed": actual_by_name[name]["master"],
        }
        for name in sorted(set(expected_by_name) & set(actual_by_name))
        if expected_by_name[name]["master"] != actual_by_name[name]["master"]
    ]
    connection_mismatches = [
        {
            "instance": name,
            "expected": list(expected_by_name[name]["nodes"]),
            "observed": list(actual_by_name[name]["nodes"]),
        }
        for name in sorted(set(expected_by_name) & set(actual_by_name))
        if expected_by_name[name]["nodes"] != actual_by_name[name]["nodes"]
    ]
    terminal_mismatches = [
        {
            "instance": name,
            "expected": expected_by_name[name]["terminals"],
            "observed": actual_by_name[name]["terminals"],
        }
        for name in sorted(set(expected_by_name) & set(actual_by_name))
        if expected_by_name[name]["terminals"] is not None
        and expected_by_name[name]["terminals"]
        != actual_by_name[name]["terminals"]
    ]
    parameter_mismatches = []
    for name in sorted(set(expected_by_name) & set(actual_by_name)):
        expected_parameters = {
            str(parameter): _normalize_oa_parameter_value(value)
            for parameter, value in expected_by_name[name]["parameters"].items()
        }
        observed_parameters = actual_by_name[name]["parameters"]
        observed_by_name = {
            str(parameter).lower(): value
            for parameter, value in observed_parameters.items()
        }
        observed = {
            parameter: observed_by_name.get(parameter.lower())
            for parameter in expected_parameters
        }
        if observed != expected_parameters:
            parameter_mismatches.append(
                {
                    "instance": name,
                    "expected": expected_parameters,
                    "observed": observed,
                }
            )

    expected_pins = set(definition.ports)
    actual_pins = actual.get("pins")
    if not isinstance(actual_pins, Mapping):
        raise ValueError("OA schematic reader returned no pin inventory")
    observed_pins = set(actual_pins)
    return {
        "passed": not any(
            (
                missing_instances,
                extra_instances,
                master_mismatches,
                connection_mismatches,
                terminal_mismatches,
                parameter_mismatches,
                expected_pins - observed_pins,
                observed_pins - expected_pins,
            )
        ),
        "expected_instance_count": len(expected_by_name),
        "observed_instance_count": len(actual_by_name),
        "missing_instances": missing_instances,
        "extra_instances": extra_instances,
        "master_mismatches": master_mismatches,
        "connection_mismatches": connection_mismatches,
        "terminal_mismatches": terminal_mismatches,
        "parameter_mismatches": parameter_mismatches,
        "missing_pins": sorted(expected_pins - observed_pins),
        "extra_pins": sorted(observed_pins - expected_pins),
    }


def check_testbench_schematic_parity(
    plan: OALibraryRebuildPlan,
    step: TestbenchRebuildStep,
    client: Any,
    *,
    timeout: int,
    acquire_flow_lock: bool,
    record_incident: bool,
    operation_id: str | None,
    bind_operation: Any | None,
    operation: Any | None,
) -> dict[str, object]:
    """Run testbench source parity under a read-only view lease."""

    def attest(current: Any) -> dict[str, object]:
        with current.view_lease(
            plan.library,
            cells=(step.cell,),
            views=((step.cell, "schematic"),),
        ):
            return _testbench_schematic_parity(
                plan,
                step,
                client,
                operation=current,
                timeout=timeout,
            )

    if operation is not None:
        return attest(operation)
    with workspace_operation(
        client,
        plan.source.workspace_root,
        "check-oa-testbench-schematic",
        policy=OperationPolicy.READ_ONLY,
        acquire_flow_lock=acquire_flow_lock,
        record_incident=record_incident,
        operation_id=operation_id,
    ) as current:
        if callable(bind_operation):
            bind_operation(current)
        return attest(current)
