"""Shared lexical primitives; owner Modules retain semantic validation."""

RUN_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"


def bounded_identity(value: object, label: str, *, maximum_length: int = 4096) -> str:
    """Validate only the common storage-safe shape of a semantic identity."""

    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum_length
        or any(ord(character) < 0x20 for character in value)
    ):
        raise ValueError(f"invalid {label}: {value!r}")
    return value


__all__ = ["RUN_ID_PATTERN", "bounded_identity"]
