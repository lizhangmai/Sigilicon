"""Tool-independent evaluation of owner-defined electrical qualification evidence."""

from __future__ import annotations

import json
import math
from pathlib import Path
import tomllib
from typing import Any, Mapping

from sigilicon.artifacts import atomic_write_json
from sigilicon.flow.model import (
    ActionContext,
    AdapterExecution,
    CollectedActionResult,
    FlowExecutionError,
    ProducedArtifact,
)


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise FlowExecutionError(f"{label} must be an object")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FlowExecutionError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise FlowExecutionError(f"{label} must be finite")
    return result


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FlowExecutionError(f"{label} must be an integer")
    return value


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise FlowExecutionError(f"{label} must be boolean")
    return value


class ElectricalQualificationAdapter:
    """Recompute an electrical offset qualification from raw campaign facts."""

    version = "1"

    def validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        diagnostics: list[str] = []
        if context.action.kind != "asic.electrical-qualification":
            diagnostics.append(
                "Electrical Qualification Adapter requires "
                "asic.electrical-qualification"
            )
        if context.action_config or context.adapter_config:
            diagnostics.append(
                "electrical qualification does not accept configuration"
            )
        try:
            summary = context.input("campaign-summary")
            spec = context.input("qualification-spec")
            if dict(summary.qualifiers) != dict(spec.qualifiers):
                diagnostics.append(
                    "electrical qualification input qualifiers do not match"
                )
            self._require_clean_spec_source(context)
        except FlowExecutionError as exc:
            diagnostics.append(str(exc))
        return tuple(diagnostics)

    def prepare(self, context: ActionContext) -> None:
        pass

    def execute(self, context: ActionContext) -> AdapterExecution:
        report = self._evaluate(context)
        atomic_write_json(context.output_path("evidence", "qualification.json"), report)
        return AdapterExecution.succeeded(
            details={
                "passed": report["passed"],
                "failure_count": len(report["failed_checks"]),
            }
        )

    def collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        passed = execution.details.get("passed")
        failure_count = execution.details.get("failure_count")
        if not isinstance(passed, bool) or not isinstance(failure_count, int):
            raise FlowExecutionError(
                "electrical qualification execution omitted its result"
            )
        evidence = context.output_path("evidence", "qualification.json")
        return CollectedActionResult(
            artifacts=(
                ProducedArtifact(
                    "evidence",
                    "evidence.qualification",
                    evidence,
                    qualifiers=context.input("campaign-summary").qualifiers,
                ),
            ),
            facts={
                "passed": passed,
                "qualification-failure-count": failure_count,
            },
            evidence=(evidence,),
        )

    @staticmethod
    def _require_clean_spec_source(context: ActionContext) -> None:
        producer = context.input("qualification-spec").producer
        request_path = context.run_root / "nodes" / producer / "action_request.json"
        try:
            request = json.loads(request_path.read_text(encoding="utf-8"))
            source = _mapping(request.get("source_assets"), "qualification source")
            git = _mapping(source.get("git"), "qualification Git source")
            dirty = _boolean(git.get("dirty"), "qualification Git dirty state")
        except (OSError, json.JSONDecodeError) as exc:
            raise FlowExecutionError(
                "cannot verify qualification spec source identity"
            ) from exc
        if dirty:
            raise FlowExecutionError(
                "electrical qualification requires a clean committed owner source"
            )

    def _evaluate(self, context: ActionContext) -> dict[str, Any]:
        try:
            summary = json.loads(
                context.input("campaign-summary").path.read_text(encoding="utf-8")
            )
            with context.input("qualification-spec").path.open("rb") as stream:
                spec_document = tomllib.load(stream)
        except (OSError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
            raise FlowExecutionError("cannot read electrical qualification inputs") from exc
        summary = _mapping(summary, "electrical campaign summary")
        if summary.get("schema") != 1:
            raise FlowExecutionError("electrical campaign summary schema must be 1")
        if summary.get("contract_kind") != "electrical-offset-campaign":
            raise FlowExecutionError(
                "electrical campaign summary contract_kind is not supported"
            )
        if spec_document.get("schema") != 1:
            raise FlowExecutionError("qualification spec schema must be 1")
        if spec_document.get("contract_kind") != "ip-qualification":
            raise FlowExecutionError(
                "qualification spec contract_kind must be 'ip-qualification'"
            )
        spec = _mapping(
            spec_document.get("electrical_offset_campaign"),
            "electrical_offset_campaign qualification spec",
        )
        expected_variant = spec.get("variant")
        expected_corner = spec.get("corner")
        qualifiers = context.input("campaign-summary").qualifiers
        if qualifiers.get("variant") != expected_variant:
            raise FlowExecutionError("qualification variant does not match evidence")
        if qualifiers.get("corner") != expected_corner:
            raise FlowExecutionError("qualification corner does not match evidence")

        expected_vcm_raw = spec.get("common_mode_v")
        if not isinstance(expected_vcm_raw, list) or not expected_vcm_raw:
            raise FlowExecutionError(
                "qualification common_mode_v must be a non-empty list"
            )
        expected_vcm = tuple(
            _number(value, "qualification common-mode")
            for value in expected_vcm_raw
        )
        expected_samples = _integer(
            spec.get("mismatch_samples"),
            "mismatch_samples",
        )
        raw_reference = _number(
            spec.get("raw_reference_sigma_mv"),
            "raw reference sigma",
        )
        calibrated_reference = _number(
            spec.get("calibrated_reference_sigma_mv"),
            "calibrated reference sigma",
        )
        ratio_min = _number(
            spec.get("reference_ratio_min"),
            "reference ratio minimum",
        )
        ratio_max = _number(
            spec.get("reference_ratio_max"),
            "reference ratio maximum",
        )
        if expected_samples <= 1:
            raise FlowExecutionError(
                "qualification mismatch_samples must be greater than one"
            )
        if raw_reference <= 0 or calibrated_reference <= 0:
            raise FlowExecutionError("qualification reference sigmas must be positive")
        if not 0 < ratio_min <= ratio_max:
            raise FlowExecutionError("qualification reference ratio interval is invalid")
        require_same = _boolean(
            spec.get("require_same_conditions"),
            "require_same_conditions",
        )
        require_coverage = _boolean(
            spec.get("require_full_code_coverage"),
            "require_full_code_coverage",
        )
        require_improvement = _boolean(
            spec.get("require_sigma_improvement"),
            "require_sigma_improvement",
        )
        points = summary.get("points")
        if not isinstance(points, list) or not points:
            raise FlowExecutionError("electrical campaign summary has no points")

        failed: list[dict[str, Any]] = []
        evaluated: list[dict[str, Any]] = []
        actual_vcm: list[float] = []
        for index, raw_point in enumerate(points):
            point = _mapping(raw_point, f"campaign point {index}")
            vcm = _number(point.get("vcm_v"), f"campaign point {index} vcm_v")
            actual_vcm.append(vcm)
            method = _mapping(point.get("method"), f"campaign point {index} method")
            raw = _mapping(point.get("raw"), f"campaign point {index} raw")
            calibrated = _mapping(
                point.get("calibrated"),
                f"campaign point {index} calibrated",
            )
            failures = _mapping(
                point.get("failures"),
                f"campaign point {index} failures",
            )
            sample_count = _integer(
                method.get("mismatch_samples"),
                f"campaign point {index} mismatch_samples",
            )
            raw_count = _integer(raw.get("count"), f"campaign point {index} raw count")
            calibrated_count = _integer(
                calibrated.get("count"),
                f"campaign point {index} calibrated count",
            )
            raw_sigma = _number(
                raw.get("sample_sigma_mv"),
                f"campaign point {index} raw sigma",
            )
            calibrated_sigma = _number(
                calibrated.get("sample_sigma_mv"),
                f"campaign point {index} calibrated sigma",
            )
            if raw_sigma <= 0 or calibrated_sigma <= 0:
                raise FlowExecutionError(
                    f"campaign point {index} sigmas must be positive"
                )
            same = _boolean(
                method.get("same_vcm_and_mc_index"),
                f"campaign point {index} same conditions",
            )
            uncovered = failures.get("uncovered_indices")
            checks = failures.get("checks")
            if not isinstance(uncovered, list) or not isinstance(checks, list):
                raise FlowExecutionError(
                    f"campaign point {index} failure collections must be lists"
                )
            checks_for_point = {
                "sample-count": (
                    sample_count == expected_samples
                    and raw_count == expected_samples
                    and calibrated_count == expected_samples
                ),
                "same-conditions": same or not require_same,
                "full-code-coverage": not uncovered or not require_coverage,
                "sigma-improvement": (
                    raw_sigma > calibrated_sigma or not require_improvement
                ),
                "raw-reference-order": (
                    ratio_min <= raw_sigma / raw_reference <= ratio_max
                ),
                "calibrated-reference-order": (
                    ratio_min <= calibrated_sigma / calibrated_reference <= ratio_max
                ),
                "point-summary": point.get("status") == "pass" and not checks,
            }
            for check, passed in checks_for_point.items():
                if not passed:
                    failed.append({"point_vcm_v": vcm, "check": check})
            evaluated.append(
                {
                    "vcm_v": vcm,
                    "mismatch_samples": sample_count,
                    "raw_sigma_mv": raw_sigma,
                    "calibrated_sigma_mv": calibrated_sigma,
                    "sigma_improvement_ratio": raw_sigma / calibrated_sigma,
                    "uncovered_samples": len(uncovered),
                    "checks": checks_for_point,
                }
            )
        if (
            len(actual_vcm) != len(set(actual_vcm))
            or sorted(actual_vcm) != sorted(expected_vcm)
        ):
            failed.append({"check": "common-mode-points"})
        passed = not failed
        return {
            "schema": 1,
            "contract_kind": "qualification-evidence",
            "conclusion": "qualification",
            "passed": passed,
            "qualifiers": dict(qualifiers),
            "points": evaluated,
            "failed_checks": failed,
        }


__all__ = ["ElectricalQualificationAdapter"]
