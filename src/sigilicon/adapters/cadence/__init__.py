"""Trusted Cadence adapter registry."""

from sigilicon.execution.adapter import Adapter


def cadence_adapters() -> tuple[Adapter, ...]:
    from sigilicon.adapters.cadence.ams_adapter import XceliumAmsAdapter
    from sigilicon.adapters.cadence.layout_adapter import (
        LayoutAdapter,
        LayoutVerificationAdapter,
    )
    from sigilicon.adapters.cadence.oa_adapter import (
        NativeOaAdapter,
        OaAttestAdapter,
        OaCheckAdapter,
        OaRebuildAdapter,
    )
    from sigilicon.adapters.cadence.rtl_adapter import SpectreAdapter, XceliumAdapter

    return (
        SpectreAdapter(),
        XceliumAdapter(),
        XceliumAmsAdapter(),
        NativeOaAdapter(),
        OaCheckAdapter(),
        OaRebuildAdapter(),
        OaAttestAdapter(),
        LayoutAdapter(),
        LayoutVerificationAdapter(),
    )


__all__ = ["cadence_adapters"]
