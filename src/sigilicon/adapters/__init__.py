"""Lazy assembly of package-owned execution adapters."""


def trusted_adapters():
    """Return the explicit built-in adapter set used by operator CLIs."""

    from sigilicon.adapters.mentor.calibre_adapter import CalibreAdapter
    from sigilicon.adapters.cadence import cadence_adapters
    from sigilicon.adapters.release import release_adapters
    from sigilicon.adapters.synopsys import synopsys_adapters

    return (CalibreAdapter(), *synopsys_adapters(), *cadence_adapters(), *release_adapters())


__all__ = ["trusted_adapters"]
