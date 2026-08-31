"""Compile stable physical ownership for routing attribution.

This module interprets placed master obstructions and pin access once. Routing
search consumes the resulting regions; pressure and placement repair consume
the same immutable owner facts instead of reinterpreting the public job.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from sigilicon.layout.physical_geometry import (
    transformed_obstructions,
    transformed_pin_accesses,
    transformed_routing_blockage_shapes,
)
from sigilicon.layout.physical_design import (
    InstancePlacement,
    LayerShape,
    PhysicalDesignJob,
    PhysicalOwnerIdentity,
    PhysicalOwnerKind,
    PinReference,
    Rect,
    RoutingBlockagePlacement,
)
from sigilicon.experimental.reference_pnr.model import PhysicalOwnerMobility

if TYPE_CHECKING:
    from sigilicon.experimental.reference_pnr._routing_resources import (
        RoutingResourceGraph,
        RoutingResourceIdentity,
    )


@dataclass(frozen=True)
class PhysicalOwner:
    identity: PhysicalOwnerIdentity
    mobility: PhysicalOwnerMobility
    repair_owner: PhysicalOwnerIdentity | None

    @property
    def repair_instance(self) -> str | None:
        if (
            self.repair_owner is not None
            and self.repair_owner.kind is PhysicalOwnerKind.INSTANCE
        ):
            return self.repair_owner.locator[0]
        return None

    @property
    def repair_blockage(self) -> str | None:
        if (
            self.repair_owner is not None
            and self.repair_owner.kind is PhysicalOwnerKind.BLOCKAGE
        ):
            return self.repair_owner.locator[0]
        return None


@dataclass(frozen=True)
class OwnedRoutingRegion:
    """Exact placed geometry with stable owner and source provenance."""

    identity: str
    layer: str
    shape: Rect
    owners: tuple[PhysicalOwnerIdentity, ...]
    source: str


@dataclass(frozen=True)
class RoutingPhysicalOwnership:
    """Immutable owner/region index for one compiled Routing Problem."""

    owners: tuple[PhysicalOwner, ...]
    regions: tuple[OwnedRoutingRegion, ...]
    _owners_by_identity: Mapping[PhysicalOwnerIdentity, PhysicalOwner]
    _regions_by_owner: Mapping[
        PhysicalOwnerIdentity,
        tuple[OwnedRoutingRegion, ...],
    ]
    _accesses_by_reference: Mapping[
        PinReference,
        tuple[OwnedRoutingRegion, ...],
    ]
    _owner_by_reference: Mapping[PinReference, PhysicalOwnerIdentity]
    _obstructions: tuple[OwnedRoutingRegion, ...]

    def owner(self, identity: PhysicalOwnerIdentity) -> PhysicalOwner:
        return self._owners_by_identity[identity]

    def owner_for_reference(self, reference: PinReference) -> PhysicalOwner:
        return self.owner(self._owner_by_reference[reference])

    def terminal_accesses(
        self,
        reference: PinReference,
    ) -> tuple[LayerShape, ...]:
        return tuple(
            LayerShape(region.layer, region.shape)
            for region in self._accesses_by_reference[reference]
        )

    def blocking_regions(
        self,
        connected: frozenset[PinReference],
    ) -> tuple[OwnedRoutingRegion, ...]:
        access_blockers = tuple(
            region
            for reference, regions in self._accesses_by_reference.items()
            if reference not in connected
            for region in regions
        )
        return tuple(
            sorted(
                self._obstructions + access_blockers,
                key=lambda item: item.identity,
            )
        )

    def regions_for_owner(
        self,
        identity: PhysicalOwnerIdentity,
    ) -> tuple[OwnedRoutingRegion, ...]:
        return self._regions_by_owner.get(identity, ())

    def owners_for_region(
        self,
        layer: str,
        region: Rect,
    ) -> tuple[PhysicalOwner, ...]:
        identities = {
            owner
            for item in self.regions
            if item.layer == layer and item.shape.intersection(region) is not None
            for owner in item.owners
        }
        return tuple(
            self.owner(identity)
            for identity in sorted(
                identities,
                key=lambda item: item.stable_name,
            )
        )

    def owners_for_resource(
        self,
        graph: RoutingResourceGraph,
        resource: RoutingResourceIdentity,
    ) -> tuple[PhysicalOwner, ...]:
        bounds = graph.bounds(resource)
        if bounds is None:
            return ()
        return self.owners_for_region(resource.layer, bounds)


def _region_identity(
    owner: PhysicalOwnerIdentity,
    source: str,
    index: int,
    shape: LayerShape,
) -> str:
    rect = shape.shape
    return (
        f"physical-region:{owner.stable_name}:{source}:{index}:{shape.layer}:"
        f"{rect.x_min}:{rect.y_min}:{rect.x_max}:{rect.y_max}"
    )


def compile_routing_physical_ownership(
    job: PhysicalDesignJob,
    instance_placements: tuple[InstancePlacement, ...],
    routing_blockage_placements: tuple[RoutingBlockagePlacement, ...] | None = None,
) -> RoutingPhysicalOwnership:
    """Compile placed obstruction and pin-access ownership once."""

    placements = {item.instance: item.placement for item in instance_placements}
    if routing_blockage_placements is None:
        routing_blockage_placements = tuple(
            RoutingBlockagePlacement(blockage.name, blockage.placement)
            for blockage in job.design.routing_blockages
        )
    blockage_placements = {
        item.blockage: item.placement for item in routing_blockage_placements
    }
    masters = {master.name: master for master in job.design.masters}
    instances = {instance.name: instance for instance in job.design.instances}
    owners: dict[PhysicalOwnerIdentity, PhysicalOwner] = {}
    regions: list[OwnedRoutingRegion] = []
    obstructions: list[OwnedRoutingRegion] = []
    accesses_by_reference: dict[
        PinReference,
        tuple[OwnedRoutingRegion, ...],
    ] = {}
    owner_by_reference: dict[PinReference, PhysicalOwnerIdentity] = {}

    for instance_name in sorted(instances):
        instance = instances[instance_name]
        mobility = (
            PhysicalOwnerMobility.MOVABLE
            if instance.fixed_placement is None
            else PhysicalOwnerMobility.FIXED
        )
        instance_identity = PhysicalOwnerIdentity(
            PhysicalOwnerKind.INSTANCE,
            (instance_name,),
        )
        owners[instance_identity] = PhysicalOwner(
            instance_identity,
            mobility,
            instance_identity,
        )
        master = masters[instance.master]
        for index, obstruction in enumerate(
            transformed_obstructions(master, placements[instance_name])
        ):
            region = OwnedRoutingRegion(
                _region_identity(
                    instance_identity,
                    "obstruction",
                    index,
                    obstruction,
                ),
                obstruction.layer,
                obstruction.shape,
                (instance_identity,),
                "obstruction",
            )
            regions.append(region)
            obstructions.append(region)

        for pin in sorted(master.pins, key=lambda item: item.name):
            reference = PinReference(pin.name, instance_name)
            pin_identity = PhysicalOwnerIdentity(
                PhysicalOwnerKind.PIN,
                (instance_name, pin.name),
            )
            owners[pin_identity] = PhysicalOwner(
                pin_identity,
                mobility,
                instance_identity,
            )
            pin_regions = tuple(
                OwnedRoutingRegion(
                    _region_identity(
                        pin_identity,
                        "pin-access",
                        index,
                        access,
                    ),
                    access.layer,
                    access.shape,
                    (pin_identity,),
                    "pin-access",
                )
                for index, access in enumerate(
                    transformed_pin_accesses(
                        master,
                        pin.name,
                        placements[instance_name],
                    )
                )
            )
            regions.extend(pin_regions)
            accesses_by_reference[reference] = pin_regions
            owner_by_reference[reference] = pin_identity

    for port in sorted(job.design.ports, key=lambda item: item.name):
        reference = PinReference(port.name)
        port_identity = PhysicalOwnerIdentity(
            PhysicalOwnerKind.PORT,
            (port.name,),
        )
        owners[port_identity] = PhysicalOwner(
            port_identity,
            PhysicalOwnerMobility.FIXED,
            None,
        )
        port_regions = tuple(
            OwnedRoutingRegion(
                _region_identity(
                    port_identity,
                    "port-access",
                    index,
                    LayerShape(access.layer, access.shape),
                ),
                access.layer,
                access.shape,
                (port_identity,),
                "port-access",
            )
            for index, access in enumerate(port.accesses)
        )
        regions.extend(port_regions)
        accesses_by_reference[reference] = port_regions
        owner_by_reference[reference] = port_identity

    for blockage in sorted(
        job.design.routing_blockages,
        key=lambda item: item.name,
    ):
        blockage_identity = PhysicalOwnerIdentity(
            PhysicalOwnerKind.BLOCKAGE,
            (blockage.name,),
        )
        mobility = (
            PhysicalOwnerMobility.MOVABLE
            if blockage.repair_region is not None
            else PhysicalOwnerMobility.FIXED
        )
        owners[blockage_identity] = PhysicalOwner(
            blockage_identity,
            mobility,
            blockage_identity,
        )
        for index, shape in enumerate(
            transformed_routing_blockage_shapes(
                blockage,
                blockage_placements[blockage.name],
            )
        ):
            region = OwnedRoutingRegion(
                _region_identity(
                    blockage_identity,
                    "routing-blockage",
                    index,
                    shape,
                ),
                shape.layer,
                shape.shape,
                (blockage_identity,),
                "routing-blockage",
            )
            regions.append(region)
            obstructions.append(region)

    ordered_owners = tuple(
        owners[identity]
        for identity in sorted(
            owners,
            key=lambda item: item.stable_name,
        )
    )
    ordered_regions = tuple(sorted(regions, key=lambda item: item.identity))
    regions_by_owner = {
        identity: tuple(
            region
            for region in ordered_regions
            if identity in region.owners
        )
        for identity in owners
    }
    return RoutingPhysicalOwnership(
        ordered_owners,
        ordered_regions,
        MappingProxyType(dict(owners)),
        MappingProxyType(regions_by_owner),
        MappingProxyType(
            {
                reference: accesses_by_reference[reference]
                for reference in sorted(
                    accesses_by_reference,
                    key=lambda item: (item.instance or "", item.pin),
                )
            }
        ),
        MappingProxyType(dict(owner_by_reference)),
        tuple(sorted(obstructions, key=lambda item: item.identity)),
    )
