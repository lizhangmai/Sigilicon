"""Lazy assembly of package-owned execution backends."""


def trusted_backends():
    """Return the explicit built-in backend set used by operator CLIs."""

    from sigilicon.backends.cadence import cadence_backends
    from sigilicon.backends.synopsys import synopsys_backends

    return (*synopsys_backends(), *cadence_backends())


__all__ = ["trusted_backends"]
