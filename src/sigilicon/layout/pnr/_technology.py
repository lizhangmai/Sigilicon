"""Normalized technology validation and derived capability negotiation."""

from __future__ import annotations

from sigilicon.layout.pnr.model import (
    Axis,
    CutSpacingRule,
    EnclosureRule,
    ExtensionRule,
    GridlessRoutingResource,
    LayerKind,
    MinimumSpacingRule,
    MinimumWidthRule,
    PhysicalTechnology,
    Rect,
    RoutingDirection,
    RoutingTrackPattern,
    TechnologyCapability,
)


def _duplicates(values: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return tuple(sorted(duplicates))


def _on_grid(value: int, grid: int) -> bool:
    return value % grid == 0


def _rect_on_grid(rectangle: Rect, grid: int) -> bool:
    return all(
        _on_grid(value, grid)
        for value in (
            rectangle.x_min,
            rectangle.y_min,
            rectangle.x_max,
            rectangle.y_max,
        )
    )


def technology_capabilities(
    technology: PhysicalTechnology,
) -> frozenset[TechnologyCapability]:
    capabilities: set[TechnologyCapability] = set()
    if any(
        isinstance(resource, RoutingTrackPattern)
        for resource in technology.routing_resources
    ):
        capabilities.add(TechnologyCapability.TRACK_ROUTING)
    if any(
        isinstance(resource, GridlessRoutingResource)
        for resource in technology.routing_resources
    ):
        capabilities.add(TechnologyCapability.GRIDLESS_ROUTING)
    if technology.via_definitions:
        capabilities.add(TechnologyCapability.VIA_DEFINITIONS)
    if technology.via_stacks:
        capabilities.add(TechnologyCapability.VIA_STACKS)
    rule_capabilities = {
        MinimumWidthRule: TechnologyCapability.MINIMUM_WIDTH_RULES,
        MinimumSpacingRule: TechnologyCapability.MINIMUM_SPACING_RULES,
        EnclosureRule: TechnologyCapability.ENCLOSURE_RULES,
        ExtensionRule: TechnologyCapability.EXTENSION_RULES,
        CutSpacingRule: TechnologyCapability.CUT_SPACING_RULES,
    }
    for rule in technology.rules:
        capability = rule_capabilities.get(type(rule))
        if capability is not None:
            capabilities.add(capability)
    return frozenset(capabilities)


def validate_technology(technology: PhysicalTechnology) -> tuple[str, ...]:
    errors: list[str] = []
    if not technology.name:
        errors.append("technology.name must be non-empty")
    if technology.dbu_per_micron <= 0:
        errors.append("technology.dbu_per_micron must be positive")
    grid = technology.manufacturing_grid_dbu
    if grid <= 0:
        errors.append("technology.manufacturing_grid_dbu must be positive")
        grid = 1

    layer_names = tuple(layer.name for layer in technology.layers)
    duplicates = _duplicates(layer_names)
    if duplicates:
        errors.append(f"duplicate physical layers: {', '.join(duplicates)}")
    layers = {layer.name: layer for layer in technology.layers}
    for layer in technology.layers:
        if not layer.name:
            errors.append("physical layer names must be non-empty")
        if not isinstance(layer.kind, LayerKind):
            errors.append(f"physical layer {layer.name} has an invalid kind")
        if layer.direction is not None and not isinstance(
            layer.direction, RoutingDirection
        ):
            errors.append(f"physical layer {layer.name} has an invalid direction")
        if layer.kind is LayerKind.ROUTING and layer.direction is None:
            errors.append(f"routing layer {layer.name} needs a direction")
        if layer.kind is not LayerKind.ROUTING and layer.direction is not None:
            errors.append(f"non-routing layer {layer.name} has a routing direction")

    resource_names = tuple(resource.name for resource in technology.routing_resources)
    duplicates = _duplicates(resource_names)
    if duplicates:
        errors.append(f"duplicate routing resources: {', '.join(duplicates)}")
    for resource in technology.routing_resources:
        if not resource.name:
            errors.append("routing resource names must be non-empty")
        layer = layers.get(resource.layer)
        if layer is None:
            errors.append(
                f"routing resource {resource.name} uses unknown layer {resource.layer}"
            )
        elif layer.kind is not LayerKind.ROUTING:
            errors.append(
                f"routing resource {resource.name} uses non-routing layer {resource.layer}"
            )
        if isinstance(resource, RoutingTrackPattern):
            if not isinstance(resource.axis, Axis):
                errors.append(f"routing resource {resource.name} has an invalid axis")
            elif layer is not None:
                expected_axis = {
                    RoutingDirection.HORIZONTAL: Axis.Y,
                    RoutingDirection.VERTICAL: Axis.X,
                }.get(layer.direction)
                if expected_axis is not None and resource.axis is not expected_axis:
                    errors.append(
                        f"routing resource {resource.name} track axis conflicts with "
                        f"the layer direction"
                    )
            if resource.pitch_dbu <= 0 or not _on_grid(resource.pitch_dbu, grid):
                errors.append(
                    f"routing resource {resource.name} pitch must be positive and on-grid"
                )
            if resource.count <= 0:
                errors.append(f"routing resource {resource.name} count must be positive")
            if not _on_grid(resource.start_dbu, grid):
                errors.append(f"routing resource {resource.name} start is off-grid")
        elif isinstance(resource, GridlessRoutingResource):
            if resource.region is not None and not _rect_on_grid(resource.region, grid):
                errors.append(f"routing resource {resource.name} region is off-grid")
        else:
            errors.append(
                f"routing resource {resource.name} has unknown type "
                f"{type(resource).__name__}"
            )

    via_names = tuple(via.name for via in technology.via_definitions)
    duplicates = _duplicates(via_names)
    if duplicates:
        errors.append(f"duplicate via definitions: {', '.join(duplicates)}")
    vias = {via.name: via for via in technology.via_definitions}
    for via in technology.via_definitions:
        if not via.name:
            errors.append("via definition names must be non-empty")
        lower = layers.get(via.lower_layer)
        cut = layers.get(via.cut_layer)
        upper = layers.get(via.upper_layer)
        if lower is None or lower.kind is not LayerKind.ROUTING:
            errors.append(f"via {via.name} lower layer must be a routing layer")
        if cut is None or cut.kind is not LayerKind.CUT:
            errors.append(f"via {via.name} cut layer must be a cut layer")
        if upper is None or upper.kind is not LayerKind.ROUTING:
            errors.append(f"via {via.name} upper layer must be a routing layer")
        for label, shapes in (
            ("lower", via.lower_shapes),
            ("cut", via.cut_shapes),
            ("upper", via.upper_shapes),
        ):
            if not shapes:
                errors.append(f"via {via.name} needs a {label} shape")
            if any(not _rect_on_grid(shape, grid) for shape in shapes):
                errors.append(f"via {via.name} has an off-grid {label} shape")

    stack_names = tuple(stack.name for stack in technology.via_stacks)
    duplicates = _duplicates(stack_names)
    if duplicates:
        errors.append(f"duplicate via stacks: {', '.join(duplicates)}")
    for stack in technology.via_stacks:
        if not stack.name:
            errors.append("via stack names must be non-empty")
        if len(stack.vias) < 2:
            errors.append(f"via stack {stack.name} needs at least two vias")
        if len(set(stack.vias)) != len(stack.vias):
            errors.append(f"via stack {stack.name} repeats a via")
        unknown = tuple(name for name in stack.vias if name not in vias)
        if unknown:
            errors.append(
                f"via stack {stack.name} uses unknown vias: {', '.join(unknown)}"
            )
            continue
        for lower_name, upper_name in zip(stack.vias, stack.vias[1:]):
            if vias[lower_name].upper_layer != vias[upper_name].lower_layer:
                errors.append(
                    f"via stack {stack.name} is discontinuous between "
                    f"{lower_name} and {upper_name}"
                )

    rule_names = tuple(rule.name for rule in technology.rules)
    duplicates = _duplicates(rule_names)
    if duplicates:
        errors.append(f"duplicate physical rules: {', '.join(duplicates)}")
    for rule in technology.rules:
        if not rule.name:
            errors.append("physical rule names must be non-empty")
        if isinstance(rule, (MinimumWidthRule, MinimumSpacingRule)):
            layer = layers.get(rule.layer)
            if layer is None or layer.kind is not LayerKind.ROUTING:
                errors.append(f"rule {rule.name} layer must be a routing layer")
            value = (
                rule.width_dbu
                if isinstance(rule, MinimumWidthRule)
                else rule.spacing_dbu
            )
            if value <= 0 or not _on_grid(value, grid):
                errors.append(f"rule {rule.name} value must be positive and on-grid")
        elif isinstance(rule, EnclosureRule):
            if rule.outer_layer not in layers or rule.inner_layer not in layers:
                errors.append(f"rule {rule.name} uses an unknown layer")
            if any(
                value < 0 or not _on_grid(value, grid)
                for value in (rule.enclosure_x_dbu, rule.enclosure_y_dbu)
            ):
                errors.append(
                    f"rule {rule.name} enclosures must be non-negative and on-grid"
                )
        elif isinstance(rule, ExtensionRule):
            if rule.outer_layer not in layers or rule.inner_layer not in layers:
                errors.append(f"rule {rule.name} uses an unknown layer")
            if rule.extension_dbu < 0 or not _on_grid(rule.extension_dbu, grid):
                errors.append(
                    f"rule {rule.name} extension must be non-negative and on-grid"
                )
            if not isinstance(rule.axis, Axis):
                errors.append(f"rule {rule.name} has an invalid extension axis")
        elif isinstance(rule, CutSpacingRule):
            layer = layers.get(rule.cut_layer)
            if layer is None or layer.kind is not LayerKind.CUT:
                errors.append(f"rule {rule.name} layer must be a cut layer")
            if any(
                value <= 0 or not _on_grid(value, grid)
                for value in (rule.spacing_x_dbu, rule.spacing_y_dbu)
            ):
                errors.append(
                    f"rule {rule.name} spacings must be positive and on-grid"
                )
        else:
            errors.append(
                f"physical rule {rule.name} has unknown type {type(rule).__name__}"
            )
    return tuple(errors)
