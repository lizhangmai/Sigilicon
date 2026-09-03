"""Lower PCell-backed drawings into technology-neutral layout IR."""

from __future__ import annotations

from dataclasses import replace

from sigilicon.layout.ir import LayoutInstance, LayoutPlan, lower_laygo2_design
from sigilicon.domain.layout_technology import LayoutTechnology


def _expected_terminals(
    instance: LayoutInstance,
    technology: LayoutTechnology,
) -> tuple[str, ...]:
    names = {name for name, _net in instance.terminals}
    policy = technology.mos_pcell
    fingers_value = next(
        (
            value
            for name, _kind, value in instance.parameters
            if name == policy.finger_count_parameter
        ),
        None,
    )
    if fingers_value is None:
        return tuple(sorted(names))
    try:
        fingers = int(fingers_value)
    except ValueError as exc:
        raise ValueError(
            f"instance {instance.name} has non-integral "
            f"{policy.finger_count_parameter}={fingers_value!r}"
        ) from exc
    if fingers < 1:
        raise ValueError(f"instance {instance.name} must have at least one finger")
    if fingers > 1:
        if not {policy.source_terminal, policy.drain_terminal}.issubset(names):
            raise ValueError(
                f"multi-finger instance {instance.name} must map "
                f"{policy.source_terminal} and {policy.drain_terminal} terminals"
            )
        diffusion_count = fingers + 1
        source_count = (diffusion_count + 1) // 2
        drain_count = diffusion_count // 2
        names.update(
            f"{policy.source_alias_prefix}{index}" for index in range(1, source_count)
        )
        names.update(
            f"{policy.drain_alias_prefix}{index}" for index in range(1, drain_count)
        )
    return tuple(sorted(names))


def _callback_parameters(
    instance: LayoutInstance,
    technology: LayoutTechnology,
) -> tuple[str, ...]:
    parameter_names = {name for name, _kind, _value in instance.parameters}
    policy = technology.mos_pcell
    if parameter_names.intersection(policy.cdf_callback_bypass_parameters):
        return ()
    if policy.cdf_callback_parameter in parameter_names:
        return (policy.cdf_callback_parameter,)
    return ()


def apply_pcell_semantics(
    plan: LayoutPlan,
    technology: LayoutTechnology,
) -> LayoutPlan:
    """Attach terminal aliases and callback policy from a technology contract."""

    return replace(
        plan,
        instances=tuple(
            replace(
                instance,
                expected_master_terminals=_expected_terminals(instance, technology),
                callback_parameters=_callback_parameters(instance, technology),
            )
            for instance in plan.instances
        ),
    )


def lower_pcell_design(
    design,
    *,
    technology: LayoutTechnology,
    **arguments,
) -> LayoutPlan:
    """Lower a drawing and apply the selected PCell technology semantics."""

    return apply_pcell_semantics(
        lower_laygo2_design(design, **arguments),
        technology,
    )


__all__ = ["apply_pcell_semantics", "lower_pcell_design"]
