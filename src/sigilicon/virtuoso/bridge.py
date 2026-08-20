"""Narrow access to the installed virtuoso-bridge adapter dependency."""

from __future__ import annotations

from importlib import import_module
from typing import Any


class BridgeDependencyUnavailable(RuntimeError):
    """The optional virtuoso-bridge dependency is not installed."""


def _bridge_attribute(module: str, name: str) -> Any:
    try:
        dependency_module = import_module(module)
    except ModuleNotFoundError as exc:
        if exc.name == "virtuoso_bridge" or (
            exc.name is not None and exc.name.startswith("virtuoso_bridge.")
        ):
            raise BridgeDependencyUnavailable(
                "virtuoso-bridge is not installed; install Sigilicon with the "
                "'virtuoso' extra before running an EDA command"
            ) from exc
        raise
    return getattr(dependency_module, name)


def create_client_from_env() -> Any:
    return _bridge_attribute("virtuoso_bridge", "VirtuosoClient").from_env()


def decode_skill_output(value: str) -> str:
    return _bridge_attribute("virtuoso_bridge", "decode_skill_output")(value)


def schematic_export_netlist_skill(*args: Any, **kwargs: Any) -> str:
    return _bridge_attribute(
        "virtuoso_bridge.virtuoso.schematic.netlist",
        "schematic_export_netlist_skill",
    )(*args, **kwargs)


def schematic_import_netlist_skill(*args: Any, **kwargs: Any) -> str:
    return _bridge_attribute(
        "virtuoso_bridge.virtuoso.schematic.netlist",
        "schematic_import_netlist_skill",
    )(*args, **kwargs)


def import_netlist_schematic(*args: Any, **kwargs: Any) -> Any:
    return load_import_netlist_schematic()(*args, **kwargs)


def load_import_netlist_schematic() -> Any:
    """Return the real dependency callable for guarded subprocess auditing."""

    return _bridge_attribute(
        "virtuoso_bridge.virtuoso.schematic.netlist",
        "import_netlist_schematic",
    )


def escape_skill_string(value: str) -> str:
    return _bridge_attribute(
        "virtuoso_bridge.virtuoso.ops", "escape_skill_string"
    )(value)


def skill_quote(value: Any) -> str:
    return _bridge_attribute("virtuoso_bridge.virtuoso.ops", "q")(value)


def parse_layout_geometry_output(value: str) -> Any:
    return _bridge_attribute(
        "virtuoso_bridge.virtuoso.layout", "parse_layout_geometry_output"
    )(value)


def layout_read_geometry(*args: Any, **kwargs: Any) -> str:
    return _bridge_attribute(
        "virtuoso_bridge.virtuoso.layout.ops", "layout_read_geometry"
    )(*args, **kwargs)


def generate_symbol_from_schematic(*args: Any, **kwargs: Any) -> Any:
    return _bridge_attribute(
        "virtuoso_bridge.virtuoso.symbol", "generate_symbol_from_schematic"
    )(*args, **kwargs)


def export_schematic_netlist(*args: Any, **kwargs: Any) -> Any:
    return _bridge_attribute(
        "virtuoso_bridge.virtuoso.schematic.netlist", "export_schematic_netlist"
    )(*args, **kwargs)


def bridge_read_schematic(*args: Any, **kwargs: Any) -> Any:
    return _bridge_attribute(
        "virtuoso_bridge.virtuoso.schematic.reader", "read_schematic"
    )(*args, **kwargs)
