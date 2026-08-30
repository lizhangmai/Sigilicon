"""Synopsys Fusion Compiler Adapter implementation."""

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
    _FC_ACTIONS,
    _FC_DIRECTORY_OUTPUT_ROLES,
    _FC_IMPLEMENTATION_OUTPUT_ROLES,
    _FC_OUTPUT_ENVIRONMENT,
    _FC_PHYSICAL_TECHNOLOGY_ENVIRONMENT,
    _FC_REFERENCE_OUTPUT_ROLES,
    _FC_STDCELL_PHYSICAL_ENVIRONMENT,
    _FC_STDCELL_TIMING_ENVIRONMENT,
    _VERILOG_IDENTIFIER,
    _manifest_members,
    _pinned_owner_runner,
    _text_mapping,
    atomic_write_json,
    complete_staged_run,
    json,
    os,
    parse_synopsys_fc_report_facts,
    run_process_group_capture,
    shutil,
)

class SynopsysFCAdapter:
    """Run managed reference-library and place-and-route Action interfaces."""

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
        if context.action.kind not in _FC_ACTIONS:
            return ("Synopsys FC Adapter received an unsupported Action",)
        try:
            self._node_configuration(context)
            self._configuration(context)
            self._pinned_runner(context)
            self._execution_resources(context)
            self._qualifiers(context)
        except FlowExecutionError as exc:
            diagnostics.append(str(exc))
        for role, artifact in context.inputs.items():
            if not artifact.path.is_file():
                diagnostics.append(f"FC input {role!r} is not a regular file")
        recipe_role = _FC_ACTIONS[context.action.kind]["recipe"]
        recipe = context.inputs.get(recipe_role)
        if recipe is not None:
            try:
                _manifest_members(
                    recipe.path,
                    recipe.kind,
                    recipe.qualifiers,
                )
            except FlowExecutionError as exc:
                diagnostics.append(str(exc))
        if context.action.kind == "asic.physical-implementation":
            reference = context.inputs.get("reference-library")
            if reference is not None:
                try:
                    self._directory_members(reference)
                except FlowExecutionError as exc:
                    diagnostics.append(str(exc))
        return tuple(diagnostics)

    def _prepare(self, context: ActionContext) -> None:
        (context.work_root / "tool").mkdir()
        (context.work_root / "inputs").mkdir()

    def _execute(self, context: ActionContext) -> AdapterExecution:
        node = self._node_configuration(context)
        configuration = self._configuration(context)
        qualifiers = self._qualifiers(context)
        runner = self._stage_recipe_runner(context, node["runner"])
        capability_environment, executable, resource_environment = (
            self._execution_resources(context)
        )
        environment = os.environ.copy()
        environment[capability_environment] = str(executable)
        for name, path in resource_environment.items():
            environment[name] = str(path)
        environment["SIGILICON_FC_WORK_ROOT"] = str(context.work_root / "tool")
        environment["SIGILICON_DESIGN_VARIANT"] = str(qualifiers["variant"])
        environment["SIGILICON_DESIGN_CORNER"] = str(qualifiers["corner"])
        environment["SIGILICON_DESIGN_TOP"] = node["top"]

        output_locations = self._output_locations(context, configuration["outputs"])
        for role, path in output_locations.items():
            environment[_FC_OUTPUT_ENVIRONMENT[role]] = str(path)

        if context.action.kind == "asic.physical-implementation":
            environment["SIGILICON_FC_MAPPED_NETLIST"] = str(
                self._stage_regular_input(context, "mapped-netlist", "mapped.v")
            )
            environment["SIGILICON_FC_MAPPED_SDC"] = str(
                self._stage_regular_input(
                    context,
                    "mapped-constraints",
                    "mapped.sdc",
                )
            )
            environment["SIGILICON_FC_REFERENCE_NDM"] = str(
                self._stage_directory_input(context, "reference-library")
            )

        try:
            completed = run_process_group_capture(
                [str(runner), node["target"]],
                cwd=context.work_root,
                env=environment,
                timeout=configuration["timeout_seconds"],
            )
        except Exception as exc:
            if "timed out" in str(exc):
                raise FlowExecutionError(
                    "managed Synopsys FC execution timed out after "
                    f"{configuration['timeout_seconds']} seconds"
                ) from exc
            raise FlowExecutionError(
                "managed Synopsys FC process supervision failed: "
                f"{type(exc).__name__}"
            ) from exc
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
            {
                "runner": node["runner"],
                "target": node["target"],
            },
        )

    def _collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        node = self._node_configuration(context)
        configuration = self._configuration(context)
        qualifiers = self._qualifiers(context)
        locations = self._output_locations(context, configuration["outputs"])
        produced: list[ProducedArtifact] = []
        for role in sorted(locations):
            output = locations[role]
            if role in _FC_DIRECTORY_OUTPUT_ROLES:
                manifest = self._write_directory_manifest(
                    context,
                    role,
                    output,
                    qualifiers,
                )
                produced.append(
                    ProducedArtifact(
                        role,
                        context.action.output(role).kind,
                        manifest,
                        qualifiers=qualifiers,
                    )
                )
                continue
            if not output.is_file():
                raise FlowExecutionError(
                    f"Synopsys FC omitted required output {role!r}: "
                    f"{configuration['outputs'][role]}"
                )
            produced.append(
                ProducedArtifact(
                    role,
                    context.action.output(role).kind,
                    output,
                    qualifiers=qualifiers,
                )
            )

        stdout = context.log_root / "stdout.log"
        stderr = context.log_root / "stderr.log"
        evidence = context.output_path("execution-evidence", "execution.json")
        atomic_write_json(
            evidence,
            {
                "schema": 1,
                "contract_kind": "tool-execution-evidence",
                "kind": "evidence.tool-execution",
                "action": context.action.kind,
                "target": node["target"],
                "exit_code": execution.exit_code,
                "qualifiers": dict(qualifiers),
            },
        )
        produced.append(
            ProducedArtifact(
                "execution-evidence",
                context.action.output("execution-evidence").kind,
                evidence,
                qualifiers=qualifiers,
            )
        )
        facts = parse_synopsys_fc_report_facts(
            context.action.kind,
            {
                role: path
                for role, path in locations.items()
                if role.endswith("-report")
            },
        )
        return CollectedActionResult(
            artifacts=tuple(produced),
            facts=facts,
            evidence=(stdout, stderr),
            details={
                "target": node["target"],
                "output_count": len(produced),
            },
        )

    def _node_configuration(self, context: ActionContext) -> dict[str, str]:
        unknown = set(context.action_config) - {"runner", "target", "top"}
        if unknown:
            raise FlowExecutionError(
                f"FC Action contains unknown configuration: {sorted(unknown)}"
            )
        expected_target = _FC_ACTIONS[context.action.kind]["target"]
        target = context.action_config.get("target")
        if target != expected_target:
            raise FlowExecutionError(
                f"FC Action {context.action.kind!r} requires target "
                f"{expected_target!r}"
            )
        runner = context.action_config.get("runner")
        if not isinstance(runner, str) or not runner:
            raise FlowExecutionError("FC Action requires an owner runner")
        top = context.action_config.get("top")
        if not isinstance(top, str) or _VERILOG_IDENTIFIER.fullmatch(top) is None:
            raise FlowExecutionError("FC Action requires a valid top module name")
        return {"runner": runner, "target": target, "top": top}

    def _configuration(self, context: ActionContext) -> dict[str, Any]:
        unknown = set(context.adapter_config) - {"timeout_seconds", "outputs"}
        if unknown:
            raise FlowExecutionError(
                f"FC profile contains unknown configuration: {sorted(unknown)}"
            )
        timeout = context.adapter_config.get("timeout_seconds")
        if not isinstance(timeout, int) or timeout <= 0:
            raise FlowExecutionError("FC profile requires a positive timeout_seconds")
        outputs = _text_mapping(context.adapter_config.get("outputs"), "FC outputs")
        expected = (
            _FC_REFERENCE_OUTPUT_ROLES
            if context.action.kind == "asic.reference-library-construction"
            else _FC_IMPLEMENTATION_OUTPUT_ROLES
        )
        if set(outputs) != expected:
            raise FlowExecutionError(
                f"FC outputs must map {sorted(expected)}"
            )
        for relative in outputs.values():
            self._validate_relative_output(relative)
        if len(outputs.values()) != len(set(outputs.values())):
            raise FlowExecutionError("FC output paths must be unique")
        return {"timeout_seconds": timeout, "outputs": outputs}

    def _pinned_runner(self, context: ActionContext) -> Path:
        node = self._node_configuration(context)
        return _pinned_owner_runner(
            context,
            _FC_ACTIONS[context.action.kind]["recipe"],
            "Fusion Compiler",
        )

    def _stage_recipe_runner(self, context: ActionContext, runner_name: str) -> Path:
        recipe_role = _FC_ACTIONS[context.action.kind]["recipe"]
        recipe = context.input(recipe_role)
        members = _manifest_members(
            recipe.path,
            recipe.kind,
            recipe.qualifiers,
        )
        stage_root = context.work_root / "inputs" / recipe_role
        staged_runner: Path | None = None
        for relative, source in members:
            destination = (stage_root / relative).resolve()
            if not destination.is_relative_to(stage_root.resolve()):
                raise FlowExecutionError(
                    f"FC recipe member escaped staging root: {relative}"
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            if relative == runner_name:
                staged_runner = destination
        if staged_runner is None:
            raise FlowExecutionError("FC runner is not pinned by its recipe")
        if not os.access(staged_runner, os.X_OK):
            raise FlowExecutionError("staged FC runner is not executable")
        return staged_runner

    def _stage_regular_input(
        self,
        context: ActionContext,
        role: str,
        filename: str,
    ) -> Path:
        artifact = context.input(role)
        if not artifact.path.is_file():
            raise FlowExecutionError(f"FC input {role!r} is missing")
        destination = context.work_root / "inputs" / role / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(artifact.path, destination)
        return destination

    def _stage_directory_input(self, context: ActionContext, role: str) -> Path:
        artifact = context.input(role)
        root_name, members = self._directory_members(artifact)
        destination_root = context.work_root / "inputs" / role / root_name
        for relative, source in members:
            destination = (destination_root / relative).resolve()
            if not destination.is_relative_to(destination_root.resolve()):
                raise FlowExecutionError(
                    f"FC directory input escaped staging root: {relative}"
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        return destination_root

    def _directory_members(
        self,
        artifact: Any,
    ) -> tuple[str, tuple[tuple[str, Path], ...]]:
        if not artifact.path.is_file():
            raise FlowExecutionError(f"FC directory artifact {artifact.role!r} is missing")
        try:
            raw = json.loads(artifact.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FlowExecutionError(f"cannot read FC directory manifest: {exc}") from exc
        if (
            not isinstance(raw, dict)
            or raw.get("schema") != 1
            or raw.get("contract_kind") != "artifact-directory-manifest"
            or raw.get("kind") != artifact.kind
            or raw.get("qualifiers") != dict(artifact.qualifiers)
        ):
            raise FlowExecutionError("FC directory manifest identity does not match input")
        root_text = raw.get("root")
        if not isinstance(root_text, str):
            raise FlowExecutionError("FC directory manifest root is invalid")
        self._validate_relative_output(root_text)
        if len(Path(root_text).parts) != 1:
            raise FlowExecutionError("FC directory manifest root must be one name")
        source_root = (artifact.path.parent / root_text).resolve()
        if (
            not source_root.is_relative_to(artifact.path.parent.resolve())
            or not source_root.is_dir()
            or source_root.is_symlink()
        ):
            raise FlowExecutionError("FC directory manifest root is unavailable")
        members_raw = raw.get("members")
        if not isinstance(members_raw, list) or not members_raw:
            raise FlowExecutionError("FC directory manifest has no members")
        members: list[tuple[str, Path]] = []
        for value in members_raw:
            if not isinstance(value, dict):
                raise FlowExecutionError("FC directory member must be an object")
            relative_text = value.get("path")
            if not isinstance(relative_text, str):
                raise FlowExecutionError("FC directory member identity is invalid")
            relative = Path(relative_text)
            if (
                not relative_text
                or relative.is_absolute()
                or "\\" in relative_text
                or any(part in {"", ".", ".."} for part in relative.parts)
            ):
                raise FlowExecutionError(
                    f"FC directory member path is unsafe: {relative_text!r}"
                )
            source = (source_root / relative).resolve()
            if (
                not source.is_relative_to(source_root)
                or not source.is_file()
                or source.is_symlink()
            ):
                raise FlowExecutionError(
                    f"FC directory member is missing or stale: {relative_text}"
                )
            members.append((relative.as_posix(), source))
        declared = [relative for relative, _source in members]
        if len(declared) != len(set(declared)):
            raise FlowExecutionError("FC directory manifest repeats a member")
        actual: list[str] = []
        for path in source_root.rglob("*"):
            if path.is_symlink():
                raise FlowExecutionError("FC directory artifact contains a symlink")
            if path.is_file():
                actual.append(path.relative_to(source_root).as_posix())
        if sorted(actual) != sorted(declared):
            raise FlowExecutionError("FC directory artifact content is stale")
        return root_text, tuple(members)

    def _write_directory_manifest(
        self,
        context: ActionContext,
        role: str,
        directory: Path,
        qualifiers: Mapping[str, Any],
    ) -> Path:
        role_root = (context.output_root / role).resolve()
        resolved = directory.resolve()
        if (
            not resolved.is_relative_to(role_root)
            or not resolved.is_dir()
            or resolved.is_symlink()
        ):
            raise FlowExecutionError(
                f"Synopsys FC omitted required directory output {role!r}"
            )
        root = resolved.relative_to(role_root).as_posix()
        if len(Path(root).parts) != 1:
            raise FlowExecutionError("FC directory output must use one managed name")
        members: list[dict[str, str]] = []
        for path in sorted(resolved.rglob("*")):
            if path.is_symlink():
                raise FlowExecutionError("FC directory output contains a symlink")
            if path.is_file():
                members.append({"path": path.relative_to(resolved).as_posix()})
        if not members:
            raise FlowExecutionError(f"Synopsys FC produced empty output {role!r}")
        manifest = context.output_path(role, f"{role}.json")
        atomic_write_json(
            manifest,
            {
                "schema": 1,
                "contract_kind": "artifact-directory-manifest",
                "kind": context.action.output(role).kind,
                "qualifiers": dict(qualifiers),
                "root": root,
                "members": members,
            },
        )
        return manifest

    def _qualifiers(self, context: ActionContext) -> Mapping[str, Any]:
        artifacts = list(context.inputs.values())
        if not artifacts:
            raise FlowExecutionError("FC Action has no semantic input")
        qualifiers = dict(artifacts[0].qualifiers)
        if "variant" not in qualifiers or "corner" not in qualifiers:
            raise FlowExecutionError("FC input omitted variant or corner qualifier")
        if any(dict(artifact.qualifiers) != qualifiers for artifact in artifacts[1:]):
            raise FlowExecutionError("FC input qualifiers do not match")
        return qualifiers

    def _execution_resources(
        self,
        context: ActionContext,
    ) -> tuple[str, Path, dict[str, Path]]:
        action = _FC_ACTIONS[context.action.kind]
        capability = context.capabilities.get(action["capability"])
        if capability is None or capability.executable is None:
            raise FlowExecutionError(
                f"Synopsys FC capability {action['capability']!r} requires "
                "a resolved executable"
            )
        executable = capability.executable
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise FlowExecutionError("resolved Synopsys FC executable is unavailable")
        resources: dict[str, Path] = {}
        if context.action.kind == "asic.reference-library-construction":
            resources.update(
                self._asset_environment(
                    context,
                    "physical-technology",
                    "platform.physical-view-set",
                    {
                        role: environment
                        for role, environment in _FC_PHYSICAL_TECHNOLOGY_ENVIRONMENT.items()
                        if role in {"technology-file", "technology-lef"}
                    },
                )
            )
            resources.update(
                self._asset_environment(
                    context,
                    "standard-cell-physical",
                    "library.lef-set",
                    _FC_STDCELL_PHYSICAL_ENVIRONMENT,
                )
            )
            resources.update(
                self._asset_environment(
                    context,
                    "standard-cell-timing",
                    "library.synopsys-db-set",
                    _FC_STDCELL_TIMING_ENVIRONMENT,
                )
            )
            return "SIGILICON_SYNOPSYS_LM_SHELL", executable, resources
        resources.update(
            self._asset_environment(
                context,
                "physical-technology",
                "platform.physical-view-set",
                {
                    role: environment
                    for role, environment in _FC_PHYSICAL_TECHNOLOGY_ENVIRONMENT.items()
                    if role in {"tluplus", "gds-layer-map", "antenna-rules"}
                },
            )
        )
        return "SIGILICON_SYNOPSYS_FC_SHELL", executable, resources

    @staticmethod
    def _asset_environment(
        context: ActionContext,
        asset_role: str,
        expected_kind: str,
        member_environment: Mapping[str, str],
    ) -> dict[str, Path]:
        asset = context.platform_assets.get(asset_role)
        if asset is None or asset.kind != expected_kind:
            raise FlowExecutionError(
                f"Synopsys FC requires {asset_role!r} kind {expected_kind!r}"
            )
        result: dict[str, Path] = {}
        for role, environment_name in member_environment.items():
            member = asset.member(role)
            if member is None:
                raise FlowExecutionError(
                    f"Synopsys FC asset {asset_role!r} omitted {role!r}"
                )
            if not member.location.is_file():
                raise FlowExecutionError(
                    f"resolved Synopsys FC asset member {asset_role}.{role} is unavailable"
                )
            result[environment_name] = member.location
        return result

    @staticmethod
    def _output_locations(
        context: ActionContext,
        outputs: Mapping[str, str],
    ) -> dict[str, Path]:
        result: dict[str, Path] = {}
        for role, relative in outputs.items():
            role_root = (context.output_root / role).resolve()
            role_root.mkdir(parents=True, exist_ok=True)
            output = (role_root / relative).resolve()
            if not output.is_relative_to(role_root):
                raise FlowExecutionError(f"FC output escaped managed root: {relative!r}")
            output.parent.mkdir(parents=True, exist_ok=True)
            result[role] = output
        return result

    @staticmethod
    def _validate_relative_output(value: str) -> None:
        path = Path(value)
        if (
            path.is_absolute()
            or not path.parts
            or "\\" in value
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise FlowExecutionError(f"unsafe FC output path: {value!r}")



