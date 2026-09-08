"""Trusted Synopsys adapter registry."""

from sigilicon.execution.adapter import Adapter


def synopsys_adapters() -> tuple[Adapter, ...]:
    from sigilicon.adapters.synopsys.dc_adapter import DcAdapter
    from sigilicon.adapters.synopsys.fc_adapter import FcAdapter
    from sigilicon.adapters.synopsys.hspice_adapter import HspiceAdapter
    from sigilicon.adapters.synopsys.lc_adapter import LibraryCompilerAdapter
    from sigilicon.adapters.synopsys.structural_adapter import StructuralLinkAdapter
    from sigilicon.adapters.synopsys.vcs_adapter import VcsAdapter

    return (
        VcsAdapter(),
        DcAdapter(),
        LibraryCompilerAdapter(),
        FcAdapter(),
        HspiceAdapter(),
        StructuralLinkAdapter(),
    )


__all__ = ["synopsys_adapters"]
