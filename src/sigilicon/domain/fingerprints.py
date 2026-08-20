"""Deterministic exact and semantic fingerprints for source-owned OA inputs.

The semantic normalizer is intentionally lexical.  It removes comments and
normalizes token spacing, but it does not interpret SKILL, ADE, or Maestro
constructs.  Cadence remains the only authority for those semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import tomllib
from typing import Any, Mapping


_WHITESPACE = re.compile(r"\s+")
_TOKEN_PUNCTUATION = set("()[]{}:,;=+*/-<>!?&|^~`'\\")


@dataclass(frozen=True)
class SourceFingerprintSet:
    """Exact and semantic identities for one complete OA source contract."""

    exact: str
    semantic: str
    exact_inputs: Mapping[str, str | None]
    semantic_inputs: Mapping[str, str | None]

    def as_dict(self) -> dict[str, Any]:
        return {
            "exact": self.exact,
            "semantic": self.semantic,
            "exact_inputs": dict(self.exact_inputs),
            "semantic_inputs": dict(self.semantic_inputs),
        }


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _length_delimited(parts: Mapping[str, bytes]) -> bytes:
    result = bytearray()
    for label in sorted(parts):
        name = label.encode("utf-8")
        value = parts[label]
        result.extend(len(name).to_bytes(8, "big"))
        result.extend(name)
        result.extend(len(value).to_bytes(8, "big"))
        result.extend(value)
    return bytes(result)


def exact_bytes_fingerprint(payload: bytes) -> str:
    """Return the byte-exact SHA-256 of one source payload."""

    return _sha256(payload)


def _strip_comments(text: str, *, suffix: str) -> str:
    """Remove lexical comments while preserving quoted strings."""

    line_comments = {".il": (";",), ".scs": ("//",), ".sv": ("//",)}
    block_comments = suffix in {".scs", ".sv"}
    line_markers = line_comments.get(suffix, ("#",) if suffix == ".toml" else ())
    output: list[str] = []
    index = 0
    in_string = False
    escaped = False
    in_block = False
    line_start = True
    while index < len(text):
        character = text[index]
        next_character = text[index + 1] if index + 1 < len(text) else ""
        if in_block:
            if character == "*" and next_character == "/":
                in_block = False
                output.extend((" ", " "))
                index += 2
            else:
                if character == "\n":
                    output.append("\n")
                    line_start = True
                index += 1
            continue
        if in_string:
            output.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            line_start = character == "\n"
            index += 1
            continue
        if character == '"':
            in_string = True
            output.append(character)
            line_start = False
            index += 1
            continue
        if block_comments and character == "/" and next_character == "*":
            in_block = True
            output.extend((" ", " "))
            index += 2
            continue
        marker = next(
            (marker for marker in line_markers if text.startswith(marker, index)),
            None,
        )
        if marker is not None:
            while index < len(text) and text[index] != "\n":
                index += 1
            continue
        # Spectre's asterisk comment is recognized only at the beginning of a
        # physical line, after indentation.  It is not a generic multiply.
        if suffix == ".scs" and line_start and character.isspace():
            output.append(character)
            index += 1
            continue
        if suffix == ".scs" and line_start and character == "*":
            while index < len(text) and text[index] != "\n":
                index += 1
            continue
        output.append(character)
        line_start = character == "\n"
        index += 1
    return "".join(output)


def _lexical_tokens(text: str) -> tuple[str, ...]:
    """Normalize spacing without assigning meaning to the token stream."""

    tokens: list[str] = []
    index = 0
    while index < len(text):
        character = text[index]
        if character.isspace():
            index += 1
            continue
        if character == '"':
            start = index
            index += 1
            escaped = False
            while index < len(text):
                current = text[index]
                index += 1
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    break
            tokens.append(text[start:index])
            continue
        if character.isalnum() or character in "_.$?%":
            start = index
            index += 1
            while index < len(text) and (
                text[index].isalnum() or text[index] in "_.$?%"
            ):
                index += 1
            tokens.append(text[start:index])
            continue
        # Keep common two-character operators together.  This is still a
        # lexical operation; no ADE/SKILL meaning is inferred.
        if index + 1 < len(text) and text[index : index + 2] in {
            "<=",
            ">=",
            "==",
            "!=",
            "&&",
            "||",
            "::",
            "=>",
        }:
            tokens.append(text[index : index + 2])
            index += 2
            continue
        if character in _TOKEN_PUNCTUATION or not character.isspace():
            tokens.append(character)
        index += 1
    return tuple(tokens)


def normalize_source_bytes(path: Path, payload: bytes | None = None) -> bytes:
    """Return a deterministic semantic representation of a source file."""

    raw = path.read_bytes() if payload is None else payload
    suffix = path.suffix.lower()
    if suffix == ".toml":
        value = tomllib.loads(raw.decode("utf-8"))
        return (
            json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            + "\n"
        ).encode("utf-8")
    text = raw.decode("utf-8")
    text = _strip_comments(text, suffix=suffix)
    return (" ".join(_lexical_tokens(text)) + "\n").encode("utf-8")


def semantic_file_fingerprint(path: Path) -> str:
    """Fingerprint one file after deterministic lexical normalization."""

    return _sha256(normalize_source_bytes(path))


def source_fingerprint_set(files: Mapping[str, Path | None]) -> SourceFingerprintSet:
    """Fingerprint a labeled set of exact source files.

    Missing optional files are represented explicitly, so adding or removing a
    contract file is itself a semantic change.
    """

    exact_parts: dict[str, bytes] = {}
    semantic_parts: dict[str, bytes] = {}
    exact_inputs: dict[str, str | None] = {}
    semantic_inputs: dict[str, str | None] = {}
    for label, path in sorted(files.items()):
        if path is None or not path.is_file():
            exact_inputs[label] = None
            semantic_inputs[label] = None
            exact_parts[label] = b"<absent>"
            semantic_parts[label] = b"<absent>"
            continue
        payload = path.read_bytes()
        exact_inputs[label] = _sha256(payload)
        normalized = normalize_source_bytes(path, payload)
        semantic_inputs[label] = _sha256(normalized)
        exact_parts[label] = payload
        semantic_parts[label] = normalized
    return SourceFingerprintSet(
        exact=_sha256(_length_delimited(exact_parts)),
        semantic=_sha256(_length_delimited(semantic_parts)),
        exact_inputs=exact_inputs,
        semantic_inputs=semantic_inputs,
    )


def oa_source_fingerprints(spec: Any, canonical_source: Path) -> SourceFingerprintSet:
    """Fingerprint the complete source-owned native OA input set."""

    native_rdb = getattr(spec.native_setup, "rdb_contract", None)
    rdb_path = None if native_rdb is None else native_rdb.path
    files = {
        "source/testbench.scs": canonical_source,
        "contract/cell.toml": spec.path.parent / "cell.toml",
        "contract/simulation.toml": spec.path,
        "contract/native_rdb.toml": rdb_path,
        "setup/setup.il": spec.native_setup.source,
    }
    if native_rdb is not None:
        files.update(
            {
                f"contract/support/{index:02d}-{path.name}": path
                for index, path in enumerate(native_rdb.support_sources)
            }
        )
    return source_fingerprint_set(files)
