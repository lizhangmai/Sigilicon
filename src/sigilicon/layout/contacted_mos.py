"""Technology-driven placement and access for contacted MOS PCells."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from sigilicon.layout.mos import MosDevice, nanometers
from sigilicon.layout.spec import LayoutSpec
from sigilicon.layout.technology import LayoutTechnology


@dataclass(frozen=True)
class ContactedMosPlacement:
    """Placed PCells plus the access geometry implied by their technology."""

    design: Any
    devices: Mapping[str, MosDevice]
    origins: Mapping[str, tuple[int, int]]
    gate_landing_half_size: tuple[int, int]
    diffusion_contact_extension: int
    bottom_gate_contact_y_offset: int
    wire_half_width: int

    def diffusion_center(self, name: str, terminal: str) -> tuple[int, int]:
        device = self.devices[name]
        origin_x, origin_y = self.origins[name]
        length = nanometers(device.parameters["l"], f"{name}.l")
        width = nanometers(device.parameters["w"], f"{name}.w")
        if terminal == "S":
            x = origin_x - self.diffusion_contact_extension
        elif terminal == "D":
            x = origin_x + length + self.diffusion_contact_extension
        else:
            raise ValueError(f"MOS access terminal must be S or D, got {terminal!r}")
        return x, origin_y + width // 2

    def gate_contact(self, name: str) -> tuple[int, int]:
        device = self.devices[name]
        origin_x, origin_y = self.origins[name]
        length = nanometers(device.parameters["l"], f"{name}.l")
        return origin_x + length // 2, origin_y + self.bottom_gate_contact_y_offset


def place_contacted_mos(
    spec: LayoutSpec,
    devices: tuple[MosDevice, ...],
    *,
    locations: Mapping[str, tuple[int, int]],
    technology: LayoutTechnology,
    pitch: tuple[int, int] | None = None,
) -> ContactedMosPlacement:
    """Place exact nf=1 MOS PCells under a typed contacted-device recipe."""

    recipe = technology.contacted_mos
    selected_pitch = recipe.pitch_dbu if pitch is None else pitch
    by_name = {device.name: device for device in devices}
    if len(by_name) != len(devices):
        raise ValueError("contacted MOS placement contains duplicate device names")
    for device in devices:
        if (
            device.parameters.get("nf") != "1"
            or device.parameters.get("multi") != "1"
        ):
            raise ValueError(
                f"contacted MOS placement requires nf=1 and multi=1: {device.name}"
            )
        length = nanometers(device.parameters["l"], f"{device.name}.l")
        if length != recipe.supported_gate_length_dbu:
            raise ValueError(
                "contacted MOS placement supports only "
                f"{recipe.supported_gate_length_dbu} nm gates: {device.name}"
            )
        technology.polarity(device.model)

    if (
        len(selected_pitch) != 2
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in selected_pitch
        )
    ):
        raise ValueError("contacted MOS pitch must contain two positive DBU integers")
    if set(locations) != set(by_name):
        raise ValueError("contacted MOS locations must exactly match the devices")
    for name, location in locations.items():
        if (
            len(location) != 2
            or any(isinstance(value, bool) or not isinstance(value, int) for value in location)
        ):
            raise ValueError(f"contacted MOS location must be an integer pair: {name}")

    route_parameter = technology.mos_pcell.cdf_callback_parameter
    finger_parameter = technology.mos_pcell.finger_count_parameter

    import laygo2

    design = laygo2.object.database.Design(name=spec.cell, libname=spec.library)
    for device in devices:
        polarity = technology.polarity(device.model)
        pcell_params = [
            ["l", "string", device.parameters["l"]],
            ["Wfg", "string", device.parameters["w"]],
            [finger_parameter, "string", device.parameters["nf"]],
            [route_parameter, "string", "Bottom"],
            *[list(parameter) for parameter in recipe.gate_contact_parameters],
            ["polyContactsEnh", "string", "Bottom"],
        ]
        if polarity == "pmos":
            pcell_params.extend(
                list(parameter) for parameter in recipe.pmos_contact_parameters
            )
        location = locations[device.name]
        design.append(
            laygo2.object.physical.Instance(
                xy=[
                    location[0] * selected_pitch[0],
                    location[1] * selected_pitch[1],
                ],
                libname=spec.pdk.oa.technology_library,
                cellname=device.model,
                name=device.name,
                params={"pcell_params": pcell_params},
            )
        )
    origins = {
        name: (
            location[0] * selected_pitch[0],
            location[1] * selected_pitch[1],
        )
        for name, location in locations.items()
    }
    return ContactedMosPlacement(
        design=design,
        devices=by_name,
        origins=origins,
        gate_landing_half_size=technology.via_landing_half_size(
            "routing2_routing3",
            "routing2",
        ),
        diffusion_contact_extension=recipe.diffusion_contact_extension_dbu,
        bottom_gate_contact_y_offset=recipe.bottom_gate_contact_y_offset_dbu,
        wire_half_width=recipe.wire_half_width_dbu,
    )


__all__ = ["ContactedMosPlacement", "place_contacted_mos"]
