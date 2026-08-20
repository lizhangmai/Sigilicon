"""Parse and persist the stable AMS truth-table protocol."""

from __future__ import annotations

import csv
import io
import re
from typing import TYPE_CHECKING

from sigilicon.ams.provenance import ams_fingerprint

if TYPE_CHECKING:
    from sigilicon.ams.spec import AmsSpec


TRUTH_RE = re.compile(
    r"TRUTH vector=(?P<vector>\d+) inputs=(?P<inputs>[0-9a-fA-FxXzZ]+) "
    r"expected=(?P<expected>[0-9a-fA-FxXzZ]+) "
    r"observed=(?P<observed>[0-9a-fA-FxXzZ]+) pass=(?P<pass>[01])"
)
SUMMARY_RE = re.compile(r"SUMMARY vectors=(?P<vectors>\d+) failed=(?P<failed>\d+)")
FINGERPRINT_RE = re.compile(r"FLOW_FINGERPRINT (?P<fingerprint>[0-9a-f]{64})")


def parse_results(text: str) -> tuple[list[dict[str, str]], dict[str, int] | None]:
    rows = [match.groupdict() for match in TRUTH_RE.finditer(text)]
    matches = list(SUMMARY_RE.finditer(text))
    summary = None
    if matches:
        summary = {key: int(value) for key, value in matches[-1].groupdict().items()}
    return rows, summary


def render_truth_table(rows: list[dict[str, str]]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream,
        fieldnames=("vector", "inputs", "expected", "observed", "pass"),
    )
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def validate_truth_contract(
    spec: AmsSpec,
    text: str,
    rows: list[dict[str, str]],
    summary: dict[str, int] | None,
) -> None:
    """Prove that the executed checker and every truth row match this spec."""

    fingerprints = FINGERPRINT_RE.findall(text)
    expected_fingerprint = ams_fingerprint(spec)
    if not fingerprints or fingerprints[-1] != expected_fingerprint:
        raise RuntimeError(
            "simulation used stale or unrecognized generated AMS state; run setup-ams-ade"
        )
    if summary is None or not rows:
        raise RuntimeError("simulation finished without a parseable truth-table summary")
    if summary["vectors"] != len(spec.vectors) or len(rows) != len(spec.vectors):
        raise RuntimeError(
            f"truth row count mismatch: spec={len(spec.vectors)} rows={len(rows)} "
            f"summary={summary}"
        )
    for index, (row, vector) in enumerate(zip(rows, spec.vectors, strict=True)):
        expected_inputs = int("".join(map(str, vector.inputs)), 2)
        expected_outputs = int("".join(map(str, vector.expected)), 2)
        try:
            actual_index = int(row["vector"])
            actual_inputs = int(row["inputs"], 16)
            actual_expected = int(row["expected"], 16)
        except ValueError as exc:
            raise RuntimeError(f"truth row {index} contains non-numeric X/Z data: {row}") from exc
        if (
            actual_index != index
            or actual_inputs != expected_inputs
            or actual_expected != expected_outputs
        ):
            raise RuntimeError(f"truth row {index} does not match current spec: {row}")
        if row["pass"] != "1":
            raise RuntimeError(f"truth row {index} failed: {row}")
    if summary["failed"]:
        raise RuntimeError(f"functional verification failed: {summary}")
