"""Synopsys HSPICE Adapter implementation."""

from __future__ import annotations

from ._common import (
    ActionContext,
    AdapterExecution,
    AdapterResult,
    Any,
    CollectedActionResult,
    FlowExecutionError,
    Mapping,
    Path,
    ProducedArtifact,
    _ENVIRONMENT_NAME,
    _ENVIRONMENT_PREFIX,
    _HSPICE_DIAGNOSTIC_ACTIONS,
    _HSPICE_MEASUREMENT_NAME,
    _HSPICE_MODEL_ENVIRONMENT,
    _HSPICE_TARGET,
    _RESERVED_PROCESS_ENVIRONMENT,
    _fact_set,
    _manifest_members,
    _pinned_owner_runner,
    _stage_source_set,
    atomic_write_json,
    complete_staged_run,
    csv,
    json,
    math,
    os,
    run_process_group_capture,
    sys,
)

class SynopsysHSpiceAdapter:
    """Run an owner-selected HSPICE regression or characterization campaign."""

    def run(self, context: ActionContext) -> AdapterResult:
        return complete_staged_run(
            context,
            validate_inputs=self._validate_inputs,
            prepare=self._prepare,
            execute=self._execute,
            collect_result=self._collect_result,
        )

    def _validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        diagnostics: list[str] = []
        if context.action.kind not in {
            "asic.electrical-functional",
            *_HSPICE_DIAGNOSTIC_ACTIONS,
            "asic.electrical-campaign",
        }:
            return (
                "Synopsys HSPICE Adapter requires an HSPICE electrical Action",
            )
        try:
            self._configuration(context)
            self._pinned_runner(context)
            self._execution_resources(context)
            self._qualifiers(context)
        except FlowExecutionError as exc:
            diagnostics.append(str(exc))
        for role in ("electrical-sources", "decks", "electrical-recipe"):
            artifact = context.inputs.get(role)
            if artifact is None:
                diagnostics.append(f"HSPICE input {role!r} is missing")
                continue
            try:
                _manifest_members(
                    artifact.path,
                    artifact.kind,
                    artifact.qualifiers,
                )
            except FlowExecutionError as exc:
                diagnostics.append(str(exc))
        return tuple(diagnostics)

    def _prepare(self, context: ActionContext) -> None:
        (context.output_root / "tool").mkdir()

    def _execute(self, context: ActionContext) -> AdapterExecution:
        configuration = self._configuration(context)
        runner = self._pinned_runner(context)
        executable, models = self._execution_resources(context)
        qualifiers = self._qualifiers(context)
        _stage_source_set(context, "electrical-sources", "electrical-sources.f")
        _stage_source_set(context, "decks", "decks.f")

        environment = os.environ.copy()
        environment["SIGILICON_SYNOPSYS_HSPICE"] = str(executable)
        environment["SIGILICON_HSPICE_OUTPUT_ROOT"] = str(
            context.output_root / "tool"
        )
        environment["SIGILICON_HSPICE_SOURCE_ROOT"] = str(
            context.work_root / "inputs" / "electrical-sources"
        )
        environment["SIGILICON_HSPICE_DECK_ROOT"] = str(
            context.work_root / "inputs" / "decks"
        )
        environment["SIGILICON_HSPICE_MODEL_SECTION"] = configuration[
            "model_section"
        ]
        environment["SIGILICON_DESIGN_VARIANT"] = str(qualifiers["variant"])
        environment["SIGILICON_DESIGN_CORNER"] = str(qualifiers["corner"])
        environment["SIGILICON_PYTHON"] = sys.executable
        environment.update(configuration.get("runner_environment", {}))
        for role, model in models.items():
            environment[_HSPICE_MODEL_ENVIRONMENT[role]] = str(model)

        completed = run_process_group_capture(
            [str(runner), configuration["target"]],
            cwd=context.work_root,
            env=environment,
            timeout=configuration["timeout_seconds"],
        )
        (context.log_root / "stdout.log").write_text(
            completed.stdout,
            encoding="utf-8",
        )
        (context.log_root / "stderr.log").write_text(
            completed.stderr or "",
            encoding="utf-8",
        )
        return AdapterExecution(
            "succeeded" if completed.returncode == 0 else "failed",
            completed.returncode,
        )

    def _collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        configuration = self._configuration(context)
        qualifiers = self._qualifiers(context)
        if context.action.kind in _HSPICE_DIAGNOSTIC_ACTIONS:
            return self._collect_diagnostic(
                context,
                configuration,
                qualifiers,
            )
        if context.action.kind == "asic.electrical-campaign":
            return self._collect_campaign(
                context,
                execution,
                configuration,
                qualifiers,
            )
        raw_measurement = self._managed_tool_output(
            context.output_root / "tool",
            configuration["measurement_file"],
        )
        if not raw_measurement.is_file():
            raise FlowExecutionError(
                "Synopsys HSPICE omitted required measurement file: "
                f"{configuration['measurement_file']}"
            )
        rows, failure_count = self._parse_measurements(
            raw_measurement,
            configuration["required_measurements"],
        )
        check_failure_count = sum(
            1
            for row in rows
            for measurement in configuration["positive_measurements"]
            if not isinstance(row[measurement], float) or row[measurement] <= 0.0
        )
        measurements = context.output_path("measurements", "measurements.json")
        atomic_write_json(
            measurements,
            {
                "schema": 1,
                "contract_kind": "measurement-collection",
                "kind": "measurement.collection",
                "target": configuration["target"],
                "qualifiers": dict(qualifiers),
                "measurement_file": configuration["measurement_file"],
                "required_measurements": list(configuration["required_measurements"]),
                "rows": rows,
            },
        )
        stdout = context.log_root / "stdout.log"
        stderr = context.log_root / "stderr.log"
        return CollectedActionResult(
            artifacts=(
                ProducedArtifact(
                    "measurements",
                    context.action.output("measurements").kind,
                    measurements,
                    qualifiers=qualifiers,
                ),
            ),
            facts=_fact_set(
                context,
                {
                    "measurement-row-count": len(rows),
                    "measurement-failure-count": failure_count,
                    "measurement-check-failure-count": check_failure_count,
                },
            ),
            evidence=(stdout, stderr, raw_measurement),
        )

    def _collect_diagnostic(
        self,
        context: ActionContext,
        configuration: Mapping[str, Any],
        qualifiers: Mapping[str, Any],
    ) -> CollectedActionResult:
        evidence = context.output_path("evidence", "diagnostic.json")
        atomic_write_json(
            evidence,
            {
                "schema": 1,
                "contract_kind": "electrical-diagnostic-evidence",
                "kind": "evidence.electrical-diagnostic",
                "target": configuration["target"],
                "qualifiers": dict(qualifiers),
                "tool_execution_completed": True,
                "evidence_role": "diagnostic",
                "product_qualification_conclusion": False,
            },
        )
        return CollectedActionResult(
            artifacts=(
                ProducedArtifact(
                    "evidence",
                    "evidence.electrical-diagnostic",
                    evidence,
                    qualifiers=qualifiers,
                ),
            ),
            facts=_fact_set(
                context,
                {
                    "evidence-role": "diagnostic",
                    "product-qualification-conclusion": False,
                },
            ),
            evidence=(
                context.log_root / "stdout.log",
                context.log_root / "stderr.log",
            ),
        )

    def _collect_campaign(
        self,
        context: ActionContext,
        execution: AdapterExecution,
        configuration: Mapping[str, Any],
        qualifiers: Mapping[str, Any],
    ) -> CollectedActionResult:
        raw_summary = self._managed_tool_output(
            context.output_root / "tool",
            configuration["summary_file"],
        )
        try:
            summary = json.loads(raw_summary.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FlowExecutionError(
                "Synopsys HSPICE omitted or malformed the campaign summary"
            ) from exc
        if not isinstance(summary, dict) or summary.get("schema") != 1:
            raise FlowExecutionError("HSPICE campaign summary schema must be 1")
        if summary.get("contract_kind") != configuration["summary_contract_kind"]:
            raise FlowExecutionError(
                "HSPICE campaign summary contract_kind does not match the Action"
            )
        records = summary.get(configuration["summary_records_field"])
        if not isinstance(records, list) or not records:
            raise FlowExecutionError("HSPICE campaign summary has no records")
        summary["kind"] = "report.electrical-campaign"
        summary["qualifiers"] = dict(qualifiers)
        output = context.output_path("campaign-summary", "campaign-summary.json")
        atomic_write_json(output, summary)
        return CollectedActionResult(
            artifacts=(
                ProducedArtifact(
                    "campaign-summary",
                    "report.electrical-campaign",
                    output,
                    qualifiers=qualifiers,
                ),
            ),
            facts=_fact_set(
                context,
                {"campaign-record-count": len(records)},
            ),
            evidence=(
                context.log_root / "stdout.log",
                context.log_root / "stderr.log",
                raw_summary,
            ),
        )

    def _configuration(self, context: ActionContext) -> dict[str, Any]:
        if context.action.kind in _HSPICE_DIAGNOSTIC_ACTIONS:
            unknown_action = set(context.action_config) - {
                "runner",
                "target",
                "model_section",
            }
            if unknown_action:
                raise FlowExecutionError(
                    "HSPICE diagnostic Action contains unknown configuration: "
                    f"{sorted(unknown_action)}"
                )
            target = context.action_config.get("target")
            if not isinstance(target, str) or _HSPICE_TARGET.fullmatch(target) is None:
                raise FlowExecutionError(
                    "HSPICE diagnostic Action requires a safe target"
                )
            if (
                context.action.kind == "asic.electrical-model-variant-diagnostic"
                and target != "core-variant-offset"
            ):
                raise FlowExecutionError(
                    "HSPICE model-variant diagnostic requires "
                    "target 'core-variant-offset'"
                )
            model_section = context.action_config.get("model_section")
            if not isinstance(model_section, str) or not model_section:
                raise FlowExecutionError(
                    "HSPICE diagnostic Action requires a model_section"
                )
            unknown_adapter = set(context.adapter_config) - {
                "timeout_seconds",
                "runner_environment_prefix",
                "runner_environment",
            }
            if unknown_adapter:
                raise FlowExecutionError(
                    "HSPICE diagnostic Adapter contains unknown configuration: "
                    f"{sorted(unknown_adapter)}"
                )
            prefix = context.adapter_config.get("runner_environment_prefix")
            if (
                not isinstance(prefix, str)
                or _ENVIRONMENT_PREFIX.fullmatch(prefix) is None
            ):
                raise FlowExecutionError(
                    "HSPICE diagnostic Action requires a safe "
                    "runner_environment_prefix"
                )
            return {
                "target": target,
                "model_section": model_section,
                "runner_environment": self._runner_environment(
                    context.adapter_config.get("runner_environment", {}),
                    prefix=prefix,
                ),
                "timeout_seconds": self._timeout(
                    context,
                    allowed={
                        "timeout_seconds",
                        "runner_environment_prefix",
                        "runner_environment",
                    },
                ),
            }
        if context.action.kind == "asic.electrical-campaign":
            unknown_action = set(context.action_config) - {
                "runner",
                "target",
                "model_section",
                "summary_file",
                "summary_contract_kind",
                "summary_records_field",
                "runner_environment_prefix",
                "runner_environment",
            }
            if unknown_action:
                raise FlowExecutionError(
                    "HSPICE campaign Action contains unknown configuration: "
                    f"{sorted(unknown_action)}"
                )
            target = context.action_config.get("target")
            if (
                not isinstance(target, str)
                or _HSPICE_TARGET.fullmatch(target) is None
            ):
                raise FlowExecutionError("HSPICE campaign Action requires a safe target")
            model_section = context.action_config.get("model_section")
            if not isinstance(model_section, str) or not model_section:
                raise FlowExecutionError(
                    "HSPICE campaign Action requires a model_section"
                )
            summary_file = context.action_config.get("summary_file")
            if not isinstance(summary_file, str):
                raise FlowExecutionError(
                    "HSPICE campaign Action requires a summary_file"
                )
            self._managed_tool_output(Path("."), summary_file)
            summary_contract_kind = context.action_config.get(
                "summary_contract_kind"
            )
            if (
                not isinstance(summary_contract_kind, str)
                or not summary_contract_kind
            ):
                raise FlowExecutionError(
                    "HSPICE campaign requires a summary_contract_kind"
                )
            summary_records_field = context.action_config.get(
                "summary_records_field"
            )
            if (
                not isinstance(summary_records_field, str)
                or _HSPICE_MEASUREMENT_NAME.fullmatch(summary_records_field) is None
            ):
                raise FlowExecutionError(
                    "HSPICE campaign requires a safe summary_records_field"
                )
            runner_environment_prefix = context.action_config.get(
                "runner_environment_prefix"
            )
            if (
                not isinstance(runner_environment_prefix, str)
                or _ENVIRONMENT_PREFIX.fullmatch(runner_environment_prefix) is None
            ):
                raise FlowExecutionError(
                    "HSPICE campaign requires a safe runner_environment_prefix"
                )
            runner_environment = self._runner_environment(
                context.action_config.get("runner_environment", {}),
                prefix=runner_environment_prefix,
            )
            timeout = self._timeout(context)
            return {
                "target": target,
                "model_section": model_section,
                "summary_file": summary_file,
                "summary_contract_kind": summary_contract_kind,
                "summary_records_field": summary_records_field,
                "runner_environment_prefix": runner_environment_prefix,
                "runner_environment": runner_environment,
                "timeout_seconds": timeout,
            }
        unknown_action = set(context.action_config) - {
            "runner",
            "target",
            "model_section",
            "measurement_file",
            "required_measurements",
            "positive_measurements",
        }
        if unknown_action:
            raise FlowExecutionError(
                "HSPICE Action contains unknown configuration: "
                f"{sorted(unknown_action)}"
            )
        target = context.action_config.get("target")
        if not isinstance(target, str) or _HSPICE_TARGET.fullmatch(target) is None:
            raise FlowExecutionError("HSPICE Action requires a safe target")
        model_section = context.action_config.get("model_section")
        if not isinstance(model_section, str) or not model_section:
            raise FlowExecutionError("HSPICE Action requires a model_section")
        measurement_file = context.action_config.get("measurement_file")
        if not isinstance(measurement_file, str):
            raise FlowExecutionError("HSPICE Action requires a measurement_file")
        self._managed_tool_output(Path("."), measurement_file)
        required = self._measurement_names(
            context.action_config.get("required_measurements"),
            "required_measurements",
        )
        positive = self._measurement_names(
            context.action_config.get("positive_measurements", ()),
            "positive_measurements",
            allow_empty=True,
        )
        if not set(positive) <= set(required):
            raise FlowExecutionError(
                "HSPICE positive_measurements must be required measurements"
            )
        timeout = self._timeout(context)
        return {
            "target": target,
            "model_section": model_section,
            "measurement_file": measurement_file,
            "required_measurements": required,
            "positive_measurements": positive,
            "timeout_seconds": timeout,
        }

    @staticmethod
    def _runner_environment(value: object, *, prefix: str) -> dict[str, str]:
        if not isinstance(value, Mapping):
            raise FlowExecutionError(
                "HSPICE runner_environment must be a mapping"
            )
        result: dict[str, str] = {}
        for name, raw_value in value.items():
            if not isinstance(name, str) or _ENVIRONMENT_NAME.fullmatch(name) is None:
                raise FlowExecutionError(
                    f"HSPICE runner_environment contains invalid name {name!r}"
                )
            if name.startswith("SIGILICON_"):
                raise FlowExecutionError(
                    "HSPICE runner_environment cannot override SIGILICON_* variables"
                )
            if name in _RESERVED_PROCESS_ENVIRONMENT:
                raise FlowExecutionError(
                    f"HSPICE runner_environment cannot override {name}"
                )
            if not name.startswith(prefix):
                raise FlowExecutionError(
                    "HSPICE runner_environment name must use owner prefix "
                    f"{prefix!r}: {name!r}"
                )
            if isinstance(raw_value, bool) or not isinstance(
                raw_value, (str, int, float)
            ):
                raise FlowExecutionError(
                    f"HSPICE runner_environment value for {name!r} must be scalar"
                )
            if isinstance(raw_value, float) and not math.isfinite(raw_value):
                raise FlowExecutionError(
                    f"HSPICE runner_environment value for {name!r} must be finite"
                )
            result[name] = str(raw_value)
        return result

    @staticmethod
    def _timeout(
        context: ActionContext,
        *,
        allowed: set[str] | None = None,
    ) -> int:
        allowed_keys = {"timeout_seconds"} if allowed is None else allowed
        unknown_adapter = set(context.adapter_config) - allowed_keys
        if unknown_adapter:
            raise FlowExecutionError(
                "HSPICE Adapter contains unknown configuration: "
                f"{sorted(unknown_adapter)}"
            )
        timeout = context.adapter_config.get("timeout_seconds")
        if not isinstance(timeout, int) or timeout <= 0:
            raise FlowExecutionError(
                "HSPICE Adapter requires a positive timeout_seconds"
            )
        return timeout

    @staticmethod
    def _measurement_names(
        value: object,
        label: str,
        *,
        allow_empty: bool = False,
    ) -> tuple[str, ...]:
        if (
            not isinstance(value, tuple)
            or (not value and not allow_empty)
            or any(
                not isinstance(item, str)
                or _HSPICE_MEASUREMENT_NAME.fullmatch(item) is None
                for item in value
            )
        ):
            requirement = "a list" if allow_empty else "a non-empty list"
            raise FlowExecutionError(f"HSPICE {label} must be {requirement}")
        if len(value) != len(set(value)):
            raise FlowExecutionError(f"HSPICE {label} repeats a measurement")
        return value

    def _pinned_runner(self, context: ActionContext) -> Path:
        return _pinned_owner_runner(
            context,
            "electrical-recipe",
            "electrical simulation",
        )

    def _qualifiers(self, context: ActionContext) -> Mapping[str, Any]:
        semantic = tuple(
            context.input(role)
            for role in ("electrical-sources", "decks", "electrical-recipe")
        )
        qualifiers = dict(semantic[0].qualifiers)
        if "variant" not in qualifiers or "corner" not in qualifiers:
            raise FlowExecutionError(
                "HSPICE inputs require variant and corner qualifiers"
            )
        if any(dict(artifact.qualifiers) != qualifiers for artifact in semantic[1:]):
            raise FlowExecutionError("HSPICE input qualifiers do not match")
        return qualifiers

    def _execution_resources(
        self,
        context: ActionContext,
    ) -> tuple[Path, dict[str, Path]]:
        capability = context.capabilities.get("tool.synopsys-hspice")
        if capability is None or capability.executable is None:
            raise FlowExecutionError(
                "Synopsys HSPICE capability requires a resolved executable"
            )
        executable = capability.executable
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise FlowExecutionError(
                "resolved Synopsys HSPICE executable is unavailable"
            )
        asset = context.platform_assets.get("hspice-models")
        if asset is None or asset.kind != "model.hspice-set":
            raise FlowExecutionError(
                "Synopsys HSPICE requires a resolved HSPICE model set"
            )
        models: dict[str, Path] = {}
        if context.action.kind == "asic.electrical-diagnostic":
            required_models = tuple(
                role
                for role in _HSPICE_MODEL_ENVIRONMENT
                if role != "stdcell-12t-rvt"
            )
        elif context.action.kind == "asic.electrical-model-variant-diagnostic":
            required_models = tuple(_HSPICE_MODEL_ENVIRONMENT)
        elif context.action.kind == "asic.electrical-campaign":
            required_models = tuple(
                role
                for role in _HSPICE_MODEL_ENVIRONMENT
                if role != "stdcell-12t-rvt"
            )
        else:
            required_models = tuple(
                role
                for role in _HSPICE_MODEL_ENVIRONMENT
                if role not in {"mismatch-model", "stdcell-12t-rvt"}
            )
        for role in required_models:
            member = asset.member(role)
            if member is None:
                raise FlowExecutionError(f"HSPICE model set omitted {role!r}")
            if not member.location.is_file():
                raise FlowExecutionError(
                    f"resolved HSPICE model {role!r} is unavailable"
                )
            models[role] = member.location
        return executable, models

    @staticmethod
    def _managed_tool_output(tool_root: Path, relative: str) -> Path:
        path = Path(relative)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise FlowExecutionError(
                f"unsafe HSPICE output path: {relative!r}"
            )
        output = (tool_root / path).resolve()
        if not output.is_relative_to(tool_root.resolve()):
            raise FlowExecutionError(
                f"HSPICE output escaped managed root: {relative!r}"
            )
        return output

    @staticmethod
    def _parse_measurements(
        path: Path,
        required: tuple[str, ...],
    ) -> tuple[list[dict[str, float | str]], int]:
        lines = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith(("$", "."))
        ]
        if len(lines) < 2:
            raise FlowExecutionError("HSPICE measurement CSV has no data rows")
        reader = csv.DictReader(lines, skipinitialspace=True)
        if reader.fieldnames is None or len(reader.fieldnames) != len(
            set(reader.fieldnames)
        ):
            raise FlowExecutionError("HSPICE measurement CSV has an invalid header")
        missing = set(required) - set(reader.fieldnames)
        if missing:
            raise FlowExecutionError(
                f"HSPICE measurement CSV omitted columns {sorted(missing)}"
            )
        rows: list[dict[str, float | str]] = []
        failures = 0
        for raw_row in reader:
            row: dict[str, float | str] = {}
            for name, raw_value in raw_row.items():
                if name is None or raw_value is None:
                    raise FlowExecutionError(
                        "HSPICE measurement CSV has an irregular row"
                    )
                value = raw_value.strip()
                if value.lower() == "failed":
                    row[name] = "failed"
                    failures += 1
                    continue
                try:
                    numeric = float(value)
                except ValueError as exc:
                    raise FlowExecutionError(
                        f"HSPICE measurement {name!r} is not numeric"
                    ) from exc
                if not math.isfinite(numeric):
                    raise FlowExecutionError(
                        f"HSPICE measurement {name!r} is not finite"
                    )
                row[name] = numeric
            rows.append(row)
        if not rows:
            raise FlowExecutionError("HSPICE measurement CSV has no data rows")
        return rows, failures

