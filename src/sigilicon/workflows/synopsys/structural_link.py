"""Synopsys structural-link Adapter implementation."""

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
    PurePosixPath,
    RELEASE_MATURITY_LEVELS,
    _VERILOG_IDENTIFIER,
    _manifest_members,
    _stage_source_set,
    atomic_write_json,
    audit_ip_release_manifest,
    complete_staged_run,
    json,
    os,
    re,
    run_process_group_capture,
    run_readonly_capture,
    sha256,
    shutil,
    tomllib,
)

class SynopsysStructuralLinkAdapter:
    """Link composite-IP RTL against one immutable structural macro release."""

    _ACTION = "asic.structural-link"
    _CAPABILITIES = (
        "tool.synopsys-library-compiler",
        "tool.synopsys-dc",
    )

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
        if context.action.kind != self._ACTION:
            diagnostics.append("structural-link Adapter requires asic.structural-link")
        for role in ("rtl-sources", "structural-link-recipe"):
            artifact = context.inputs.get(role)
            if artifact is None:
                diagnostics.append(f"structural-link input {role!r} is missing")
            elif not artifact.path.is_file():
                diagnostics.append(
                    f"structural-link input {role!r} is not a regular file"
                )
        try:
            recipe, recipe_members = self._recipe(context)
            integration = self._integration(context, recipe, recipe_members)
            self._source_members(context, integration)
            self._recipe_member(recipe_members, recipe["liberty_compile_script"])
            self._recipe_member(recipe_members, recipe["link_script"])
            self._variant_top(context, recipe, recipe_members)
            self._executables(context)
            self._timeout(context)
        except FlowExecutionError as exc:
            diagnostics.append(str(exc))
        return tuple(diagnostics)

    @staticmethod
    def _prepare(context: ActionContext) -> None:
        (context.output_root / "tool").mkdir()

    def _execute(self, context: ActionContext) -> AdapterExecution:
        recipe, recipe_members = self._recipe(context)
        integration = self._integration(context, recipe, recipe_members)
        self._source_members(context, integration)
        source_file = _stage_source_set(
            context,
            "rtl-sources",
            "structural-link-sources.f",
        )
        library_compiler, design_compiler = self._executables(context)
        compile_script = self._recipe_member(
            recipe_members,
            recipe["liberty_compile_script"],
        )
        link_script = self._recipe_member(
            recipe_members,
            recipe["link_script"],
        )
        top = self._variant_top(context, recipe, recipe_members)
        released, macro_liberty = self._released_macro(context, recipe, integration)
        staged_release_root = context.work_root / "inputs" / "release"
        staged_release_root.mkdir(parents=True)
        staged_macro_liberty = staged_release_root / "structural-macro.lib"
        shutil.copyfile(macro_liberty, staged_macro_liberty)
        macro_liberty_sha256 = sha256(staged_macro_liberty.read_bytes()).hexdigest()
        pinned_source_sha256 = self._pinned_release_source_sha256(
            context,
            recipe,
            released,
            macro_liberty,
        )
        if macro_liberty_sha256 != pinned_source_sha256:
            raise FlowExecutionError(
                "structural-link Liberty differs from its pinned source commit"
            )
        tool_root = context.output_root / "tool"
        macro_db = tool_root / "structural-macro.db"
        report = tool_root / "structural-link.rpt"
        checkpoint = tool_root / f"{top}.ddc"
        parameters = ",".join(
            f"{name}={value}"
            for name, value in recipe["parameter_overrides"].items()
        )
        environment = os.environ.copy()
        environment.pop("LD_PRELOAD", None)
        environment.update(
            {
                "SIGILICON_STRUCTURAL_LIBERTY": str(staged_macro_liberty),
                "SIGILICON_STRUCTURAL_DB": str(macro_db),
                "SIGILICON_STRUCTURAL_LIBRARY": recipe["library_name"],
                "SIGILICON_STRUCTURAL_MACRO_CELL": recipe["macro_cell"],
                "SIGILICON_STRUCTURAL_TOP": top,
                "SIGILICON_STRUCTURAL_SOURCES": str(source_file),
                "SIGILICON_STRUCTURAL_PARAMETERS": parameters,
                "SIGILICON_STRUCTURAL_REPORT": str(report),
                "SIGILICON_STRUCTURAL_CHECKPOINT": str(checkpoint),
            }
        )

        lc = run_process_group_capture(
            [str(library_compiler), "-f", str(compile_script)],
            cwd=context.work_root,
            env=environment,
            timeout=self._timeout(context),
        )
        (context.log_root / "library-compiler.stdout.log").write_text(
            lc.stdout,
            encoding="utf-8",
        )
        (context.log_root / "library-compiler.stderr.log").write_text(
            lc.stderr or "",
            encoding="utf-8",
        )
        lc_marker = (
            "SIGILICON_STRUCTURAL_DB_PASS "
            f"library={recipe['library_name']}"
        )
        lc_errors = tuple(
            line
            for line in (lc.stdout + "\n" + (lc.stderr or "")).splitlines()
            if line.startswith(("Error:", "Fatal:"))
        )
        if (
            lc.returncode != 0
            or lc_marker not in lc.stdout
            or lc_errors
            or not macro_db.is_file()
        ):
            return AdapterExecution(
                "failed",
                lc.returncode,
                {
                    "stage": "library-compilation",
                    "tool-error-count": len(lc_errors),
                },
            )

        dc = run_process_group_capture(
            [str(design_compiler), "-f", str(link_script)],
            cwd=context.work_root,
            env=environment,
            timeout=self._timeout(context),
        )
        (context.log_root / "design-compiler.stdout.log").write_text(
            dc.stdout,
            encoding="utf-8",
        )
        (context.log_root / "design-compiler.stderr.log").write_text(
            dc.stderr or "",
            encoding="utf-8",
        )
        expected = recipe["expected"]
        marker = (
            f"SIGILICON_STRUCTURAL_LINK_PASS top={top} "
            f"macro_instances={expected['macro_instances']} "
            f"unresolved={expected['unresolved_references']}"
        )
        tool_errors = tuple(
            line
            for line in (dc.stdout + "\n" + (dc.stderr or "")).splitlines()
            if line.startswith(("Error:", "Fatal:"))
        )
        if (
            dc.returncode != 0
            or marker not in dc.stdout
            or tool_errors
            or not report.is_file()
            or not checkpoint.is_file()
        ):
            return AdapterExecution(
                "failed",
                dc.returncode,
                {"stage": "structural-link", "tool-error-count": len(tool_errors)},
            )
        return AdapterExecution.succeeded(
            details={
                "stage": "complete",
                "variant": self._variant(context),
                "top": top,
                "release": {
                    "release-id": released["release_id"],
                    "source-commit": released["source_commit"],
                    "manifest": released["manifest"],
                    "manifest-sha256": released["manifest_sha256"],
                    "macro-liberty": released["roles"][recipe["liberty_role"]],
                    "macro-liberty-sha256": macro_liberty_sha256,
                    "pinned-source-sha256": pinned_source_sha256,
                },
            }
        )

    def _collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        recipe, recipe_members = self._recipe(context)
        top = self._variant_top(context, recipe, recipe_members)
        if execution.details.get("top") != top:
            raise FlowExecutionError("structural-link execution top identity drifted")
        release = execution.details.get("release")
        if not isinstance(release, Mapping):
            raise FlowExecutionError("structural-link execution omitted release identity")
        tool_root = context.output_root / "tool"
        macro_db = tool_root / "structural-macro.db"
        report = tool_root / "structural-link.rpt"
        checkpoint = tool_root / f"{top}.ddc"
        for label, path in (
            ("compiled macro library", macro_db),
            ("structural report", report),
            ("checkpoint", checkpoint),
        ):
            if not path.is_file():
                raise FlowExecutionError(f"structural-link omitted {label}")

        expected = recipe["expected"]
        claims = recipe["claims"]
        qualifiers = dict(context.input("rtl-sources").qualifiers)
        evidence_path = context.output_path("evidence", "structural-link.json")
        evidence = {
            "schema": 1,
            "contract_kind": "structural-link-evidence",
            "owner": context.require_project_scope().owner,
            "variant": self._variant(context),
            "top": top,
            "scope": "structural-link-only",
            "parameter_overrides": dict(recipe["parameter_overrides"]),
            "macro_cell": recipe["macro_cell"],
            "macro_instances": expected["macro_instances"],
            "unresolved_references": expected["unresolved_references"],
            "release_id": release["release-id"],
            "release_source_commit": release["source-commit"],
            "manifest": release["manifest"],
            "manifest_sha256": release["manifest-sha256"],
            "macro_liberty": release["macro-liberty"],
            "macro_liberty_sha256": release["macro-liberty-sha256"],
            "pinned_source_sha256": release["pinned-source-sha256"],
            "compiled_macro_db_sha256": sha256(macro_db.read_bytes()).hexdigest(),
            "timing_characterized": claims["timing_characterized"],
            "power_characterized": claims["power_characterized"],
            "area_characterized": claims["area_characterized"],
        }
        atomic_write_json(evidence_path, evidence)
        facts = {
            "passed": True,
            "evidence-role": "regression",
            "evidence-level": "l4",
            "evidence-scope": "native-macro-structural-link",
            "product-qualification-conclusion": False,
            "macro-instance-count": expected["macro_instances"],
            "unresolved-reference-count": expected["unresolved_references"],
            "timing-characterized": claims["timing_characterized"],
            "power-characterized": claims["power_characterized"],
            "area-characterized": claims["area_characterized"],
        }
        return CollectedActionResult(
            artifacts=(
                ProducedArtifact(
                    "compiled-macro-library",
                    context.action.output("compiled-macro-library").kind,
                    macro_db,
                    qualifiers=qualifiers,
                ),
                ProducedArtifact(
                    "checkpoint",
                    context.action.output("checkpoint").kind,
                    checkpoint,
                    qualifiers=qualifiers,
                ),
                ProducedArtifact(
                    "structural-report",
                    context.action.output("structural-report").kind,
                    report,
                    qualifiers=qualifiers,
                ),
                ProducedArtifact(
                    "evidence",
                    context.action.output("evidence").kind,
                    evidence_path,
                    qualifiers=qualifiers,
                ),
            ),
            facts=facts,
            evidence=(
                context.log_root / "library-compiler.stdout.log",
                context.log_root / "library-compiler.stderr.log",
                context.log_root / "design-compiler.stdout.log",
                context.log_root / "design-compiler.stderr.log",
                evidence_path,
            ),
            details={"release-id": release["release-id"]},
        )

    def _recipe(
        self,
        context: ActionContext,
    ) -> tuple[dict[str, Any], dict[str, Path]]:
        artifact = context.input("structural-link-recipe")
        members = dict(
            _manifest_members(
                artifact.path,
                artifact.kind,
                artifact.qualifiers,
            )
        )
        recipe_path = context.action_config.get("recipe")
        if not isinstance(recipe_path, str) or recipe_path not in members:
            raise FlowExecutionError(
                "structural-link recipe must be pinned by its typed input"
            )
        try:
            with members[recipe_path].open("rb") as stream:
                raw = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise FlowExecutionError("cannot read structural-link recipe") from exc
        scope = context.require_project_scope()
        if (
            raw.get("schema") != 1
            or raw.get("contract_kind") != "ip-structural-link-recipe"
            or raw.get("path_scope") != "owner"
            or raw.get("owner") != scope.owner
        ):
            raise FlowExecutionError("structural-link recipe header is invalid")
        required_text = (
            "component_contract",
            "dependency_lock",
            "provider_owner",
            "fileset",
            "liberty_role",
            "liberty_compile_script",
            "link_script",
            "library_name",
            "macro_cell",
        )
        for field in required_text:
            if not isinstance(raw.get(field), str) or not raw[field]:
                raise FlowExecutionError(
                    f"structural-link recipe {field!r} must be text"
                )
        for field in ("library_name", "macro_cell"):
            if _VERILOG_IDENTIFIER.fullmatch(raw[field]) is None:
                raise FlowExecutionError(
                    f"structural-link recipe {field!r} must be an identifier"
                )
        parameters = raw.get("parameter_overrides")
        if (
            not isinstance(parameters, dict)
            or not parameters
            or any(
                not isinstance(name, str)
                or _VERILOG_IDENTIFIER.fullmatch(name) is None
                or not isinstance(value, int)
                or isinstance(value, bool)
                or value <= 0
                for name, value in parameters.items()
            )
        ):
            raise FlowExecutionError(
                "structural-link parameter_overrides must be positive integers"
            )
        variants = raw.get("variants")
        if not isinstance(variants, dict) or any(
            not isinstance(name, str)
            or not isinstance(path, str)
            or not path
            for name, path in variants.items()
        ):
            raise FlowExecutionError("structural-link variants are invalid")
        expected = raw.get("expected")
        if not isinstance(expected, dict) or set(expected) != {
            "macro_instances",
            "unresolved_references",
        }:
            raise FlowExecutionError("structural-link expected results are invalid")
        for name, value in expected.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise FlowExecutionError(
                    f"structural-link expected {name!r} must be non-negative"
                )
        claims = raw.get("claims")
        if not isinstance(claims, dict) or claims != {
            "timing_characterized": False,
            "power_characterized": False,
            "area_characterized": False,
        }:
            raise FlowExecutionError(
                "structural-link recipe must preserve uncharacterized claims"
            )
        return raw, members

    def _integration(
        self,
        context: ActionContext,
        recipe: Mapping[str, Any],
        recipe_members: Mapping[str, Path],
    ) -> dict[str, Any]:
        scope = context.require_project_scope()
        variant_name = self._variant(context)
        component = self._toml_member(
            recipe_members,
            recipe["component_contract"],
            "component contract",
        )
        dependency_lock = self._toml_member(
            recipe_members,
            recipe["dependency_lock"],
            "dependency lock",
        )
        variant_path = recipe["variants"].get(variant_name)
        variant = self._toml_member(
            recipe_members,
            variant_path,
            "variant contract",
        )
        if (
            component.get("schema") != 1
            or component.get("contract_kind") != "ip-component"
            or component.get("path_scope") != "owner"
            or component.get("owner") != scope.owner
            or component.get("name") != scope.owner
        ):
            raise FlowExecutionError(
                "structural-link component snapshot identity is invalid"
            )
        if (
            dependency_lock.get("schema") != 1
            or dependency_lock.get("contract_kind") != "ip-dependency-lock"
            or dependency_lock.get("path_scope") != "owner"
            or dependency_lock.get("owner") != scope.owner
            or dependency_lock.get("ip") != scope.owner
        ):
            raise FlowExecutionError(
                "structural-link dependency-lock snapshot identity is invalid"
            )
        if (
            variant.get("schema") != 1
            or variant.get("contract_kind") != "ip-operating-variant"
            or variant.get("path_scope") != "variant"
            or variant.get("owner") != scope.owner
        ):
            raise FlowExecutionError(
                "structural-link variant snapshot identity is invalid"
            )

        component_project_path = self._project_member_path(
            context,
            recipe["component_contract"],
        )
        lock_project_path = self._project_member_path(
            context,
            recipe["dependency_lock"],
        )
        variant_project_path = self._project_member_path(context, variant_path)
        variants = component.get("variants")
        variant_integration = variant.get("integration")
        if (
            component.get("dependency_lock") != lock_project_path
            or not isinstance(variants, Mapping)
            or variants.get(variant_name) != variant_project_path
            or not isinstance(variant_integration, Mapping)
            or variant_integration.get("component_contract")
            != component_project_path
            or variant_integration.get("variant") != variant_name
        ):
            raise FlowExecutionError(
                "structural-link component, lock and variant snapshots disagree"
            )

        filesets = variant.get("filesets")
        fileset = (
            filesets.get(recipe["fileset"])
            if isinstance(filesets, Mapping)
            else None
        )
        if (
            not isinstance(fileset, Mapping)
            or fileset.get("required_capability") != "synthesis"
        ):
            raise FlowExecutionError(
                "structural-link variant omitted its synthesis fileset"
            )
        dependency_roles = fileset.get("dependency_roles")
        if not isinstance(dependency_roles, Mapping) or len(dependency_roles) != 1:
            raise FlowExecutionError(
                "structural-link fileset must select one macro dependency"
            )
        dependency_name, roles = next(iter(dependency_roles.items()))
        if roles != [recipe["liberty_role"]]:
            raise FlowExecutionError(
                "structural-link fileset did not select only its Liberty role"
            )

        dependencies = component.get("component")
        if not isinstance(dependencies, list):
            raise FlowExecutionError(
                "structural-link component dependencies are invalid"
            )
        selected = [
            item
            for item in dependencies
            if isinstance(item, Mapping) and item.get("name") == dependency_name
        ]
        if len(selected) != 1 or not isinstance(selected[0].get("release"), Mapping):
            raise FlowExecutionError(
                "structural-link macro dependency is not uniquely released"
            )
        provider_contract = selected[0].get("contract")
        release_contract = selected[0]["release"]
        release_roles = release_contract.get("roles")
        if (
            not isinstance(provider_contract, str)
            or not isinstance(release_roles, list)
            or any(not isinstance(role, str) or not role for role in release_roles)
            or len(release_roles) != len(set(release_roles))
            or recipe["liberty_role"] not in release_roles
            or not isinstance(release_contract.get("export"), str)
            or release_contract.get("required_maturity")
            not in RELEASE_MATURITY_LEVELS
            or not isinstance(release_contract.get("interface"), Mapping)
        ):
            raise FlowExecutionError(
                "structural-link macro release contract is invalid"
            )
        self._safe_relative(provider_contract, "provider component contract")

        locked_dependencies = dependency_lock.get("dependency")
        if not isinstance(locked_dependencies, list):
            raise FlowExecutionError(
                "structural-link dependency lock has no dependencies"
            )
        locked = [
            item
            for item in locked_dependencies
            if isinstance(item, Mapping) and item.get("name") == dependency_name
        ]
        if len(locked) != 1:
            raise FlowExecutionError(
                "structural-link dependency lock did not pin the macro release"
            )
        released = self._locked_release(
            context,
            recipe,
            dependency_name,
            provider_contract,
            release_contract,
            locked[0],
        )

        filelist_value = fileset.get("filelist")
        filelist_member = self._owner_member_from_project_path(
            context,
            recipe_members,
            filelist_value,
            "synthesis filelist",
        )
        try:
            lines = filelist_member.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise FlowExecutionError(
                "structural-link synthesis filelist is unreadable"
            ) from exc
        source_files = [line.strip() for line in lines if line.strip()]
        if not source_files or any(line.startswith("#") for line in source_files):
            raise FlowExecutionError(
                "structural-link synthesis filelist must contain only source paths"
            )
        return {
            "passed": True,
            "owner": scope.owner,
            "variant": variant_name,
            "fileset": recipe["fileset"],
            "source_files": source_files,
            "dependency_releases": [released],
        }

    def _locked_release(
        self,
        context: ActionContext,
        recipe: Mapping[str, Any],
        dependency_name: object,
        provider_contract: str,
        release_contract: Mapping[str, Any],
        locked: Mapping[str, Any],
    ) -> dict[str, Any]:
        manifest_value = locked.get("manifest")
        manifest_sha256 = locked.get("manifest_sha256")
        source_commit = locked.get("source_commit")
        release_id = locked.get("release_id")
        if (
            not isinstance(dependency_name, str)
            or not isinstance(manifest_value, str)
            or not isinstance(manifest_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is None
            or not isinstance(source_commit, str)
            or re.fullmatch(r"[0-9a-f]{40}", source_commit) is None
            or not isinstance(release_id, str)
            or not release_id
        ):
            raise FlowExecutionError(
                "structural-link locked release identity is invalid"
            )
        artifact_root = context.require_project_scope().project.artifact_root.resolve()
        manifest_relative = self._safe_relative(
            manifest_value,
            "release manifest",
        )
        manifest = self._artifact_member(
            artifact_root,
            manifest_relative,
            "release manifest",
        )
        if not manifest.is_file():
            raise FlowExecutionError(
                "structural-link release manifest is unavailable"
            )
        try:
            manifest_bytes = manifest.read_bytes()
            raw = json.loads(manifest_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FlowExecutionError(
                "structural-link release manifest is unreadable"
            ) from exc
        try:
            audited = audit_ip_release_manifest(manifest)
        except (OSError, ValueError, RuntimeError) as exc:
            raise FlowExecutionError(
                "structural-link release package audit failed"
            ) from exc
        if audited != raw:
            raise FlowExecutionError(
                "structural-link release manifest changed during package audit"
            )
        maturity = raw.get("maturity") if isinstance(raw, Mapping) else None
        actual_maturity = maturity.get("level") if isinstance(maturity, Mapping) else None
        required_maturity = release_contract["required_maturity"]
        maturity_checks = maturity.get("checks") if isinstance(maturity, Mapping) else None
        if (
            sha256(manifest_bytes).hexdigest() != manifest_sha256
            or raw.get("schema") != 1
            or raw.get("contract_kind") != "ip-release-manifest"
            or raw.get("release_kind") != "source-package"
            or raw.get("owner") != dependency_name
            or raw.get("ip_name") != dependency_name
            or raw.get("release_id") != release_id
            or raw.get("source_commit") != source_commit
            or actual_maturity != locked.get("maturity")
            or actual_maturity not in RELEASE_MATURITY_LEVELS
            or RELEASE_MATURITY_LEVELS.index(actual_maturity)
            < RELEASE_MATURITY_LEVELS.index(required_maturity)
            or not isinstance(maturity_checks, list)
            or not maturity_checks
            or any(
                not isinstance(check, Mapping) or check.get("passed") is not True
                for check in maturity_checks
            )
        ):
            raise FlowExecutionError(
                "structural-link release manifest disagrees with its snapshot lock"
            )

        provider_relative = self._safe_relative(
            provider_contract,
            "provider component contract",
        )
        component_identity = raw.get("component")
        provenance = raw.get("provenance")
        source_files = raw.get("source_files")
        if (
            not isinstance(component_identity, Mapping)
            or component_identity.get("name") != dependency_name
            or component_identity.get("contract") != provider_relative.as_posix()
            or not isinstance(component_identity.get("kind"), str)
            or not isinstance(provenance, Mapping)
            or provenance.get("working_tree_dirty") is not False
            or provenance.get("producer") != recipe["provider_owner"]
            or not isinstance(provenance.get("contract"), str)
            or not isinstance(source_files, list)
            or any(not isinstance(path, str) for path in source_files)
            or len(source_files) != len(set(source_files))
        ):
            raise FlowExecutionError(
                "structural-link release provider provenance is invalid"
            )
        producer_relative = self._safe_relative(
            recipe["provider_owner"],
            "release producer",
        )
        release_contract_relative = self._safe_relative(
            provenance["contract"],
            "provider release contract",
        )
        try:
            provider_relative.relative_to(producer_relative)
            release_contract_relative.relative_to(producer_relative)
        except ValueError as exc:
            raise FlowExecutionError(
                "structural-link release provenance escaped its provider owner"
            ) from exc
        for source in source_files:
            self._safe_relative(source, "release source")
        if (
            provider_relative.as_posix() not in source_files
            or release_contract_relative.as_posix() not in source_files
        ):
            raise FlowExecutionError(
                "structural-link release omitted its provider contracts"
            )

        export_name = release_contract["export"]
        exported = self._release_export(raw, export_name)
        if (
            not self._release_interface_matches(
                exported,
                release_contract["interface"],
                producer_relative,
                source_files,
            )
            or not isinstance(exported.get("availability"), Mapping)
            or exported["availability"].get("synthesis") is not True
        ):
            raise FlowExecutionError(
                "structural-link release export disagrees with integration intent"
            )
        role_exports = release_contract.get("role_exports", {})
        role_modules = release_contract.get("role_modules", {})
        if (
            not isinstance(role_exports, Mapping)
            or not isinstance(role_modules, Mapping)
            or any(
                role not in release_contract["roles"]
                or not isinstance(value, str)
                or not value
                for role, value in role_exports.items()
            )
            or any(
                role not in release_contract["roles"]
                or not isinstance(value, str)
                or not value
                for role, value in role_modules.items()
            )
        ):
            raise FlowExecutionError(
                "structural-link release role mapping is invalid"
            )
        role_export = role_exports.get(recipe["liberty_role"], export_name)
        role_exported = self._release_export(raw, role_export)
        availability = role_exported.get("availability")
        if (
            not isinstance(availability, Mapping)
            or availability.get("synthesis") is not True
        ):
            raise FlowExecutionError(
                "structural-link Liberty role is unavailable for synthesis"
            )
        views = raw.get("views")
        selected_views = [
            item
            for item in views
            if isinstance(item, Mapping)
            and item.get("export") == role_export
            and item.get("role") == recipe["liberty_role"]
            and isinstance(item.get("capabilities"), list)
            and "synthesis" in item["capabilities"]
        ] if isinstance(views, list) else []
        if len(selected_views) != 1 or not isinstance(
            selected_views[0].get("path"), str
        ) or (
            recipe["liberty_role"] in role_modules
            and selected_views[0].get("module")
            != role_modules[recipe["liberty_role"]]
        ):
            raise FlowExecutionError(
                "structural-link release has no unique Liberty view"
            )
        view_relative = self._safe_relative(
            selected_views[0]["path"],
            "release view",
        )
        macro_liberty = self._artifact_member(
            artifact_root,
            manifest_relative.parent / view_relative,
            "release Liberty view",
        )
        if (
            not macro_liberty.is_file()
            or selected_views[0].get("size") != macro_liberty.stat().st_size
        ):
            raise FlowExecutionError(
                "structural-link release Liberty view is unavailable"
            )
        return {
            "name": dependency_name,
            "export": export_name,
            "provider_contract": provider_relative.as_posix(),
            "provider_release_contract": release_contract_relative.as_posix(),
            "release_id": release_id,
            "source_commit": source_commit,
            "manifest": manifest_relative.as_posix(),
            "manifest_sha256": manifest_sha256,
            "maturity": actual_maturity,
            "roles": {
                recipe["liberty_role"]: macro_liberty.relative_to(
                    artifact_root
                ).as_posix(),
            },
        }

    @staticmethod
    def _release_export(
        manifest: Mapping[str, Any],
        name: object,
    ) -> Mapping[str, Any]:
        exports = manifest.get("exports")
        selected = [
            item
            for item in exports
            if isinstance(item, Mapping) and item.get("name") == name
        ] if isinstance(exports, list) and isinstance(name, str) else []
        if len(selected) != 1:
            raise FlowExecutionError(
                "structural-link release export identity is invalid"
            )
        return selected[0]

    @classmethod
    def _release_interface_matches(
        cls,
        exported: Mapping[str, Any],
        expected: Mapping[str, Any],
        producer: PurePosixPath,
        source_files: list[str],
    ) -> bool:
        interface = exported.get("interface")
        if not isinstance(interface, Mapping):
            return False
        kind = expected.get("kind")
        if kind == "oa-native":
            contract = interface.get("contract")
            if (
                set(expected)
                != {"kind", "library", "cell", "schematic_view", "layout_view"}
                or set(interface) != {"kind", "contract"}
                or interface.get("kind") != kind
                or not isinstance(contract, str)
            ):
                return False
            try:
                contract_path = cls._safe_relative(
                    contract,
                    "release interface contract",
                )
                contract_path.relative_to(producer)
            except (FlowExecutionError, ValueError):
                return False
            oa = exported.get("oa")
            return (
                contract_path.as_posix() in source_files
                and isinstance(oa, Mapping)
                and all(
                    oa.get(field) == expected[field]
                    for field in ("library", "cell", "schematic_view", "layout_view")
                )
            )
        if kind == "oa-mixed-signal":
            return set(expected) == {"kind", "logical", "physical"} and all(
                interface.get(field) == expected[field]
                for field in ("logical", "physical")
            )
        if kind == "rtl":
            contract = interface.get("contract")
            if set(expected) != {"kind", "module"} or not isinstance(contract, str):
                return False
            try:
                contract_path = cls._safe_relative(
                    contract,
                    "release interface contract",
                )
                contract_path.relative_to(producer)
            except (FlowExecutionError, ValueError):
                return False
            return (
                contract_path.as_posix() in source_files
                and interface.get("kind") == kind
                and interface.get("module") == expected["module"]
            )
        return False

    @staticmethod
    def _toml_member(
        members: Mapping[str, Path],
        value: object,
        label: str,
    ) -> dict[str, Any]:
        path = SynopsysStructuralLinkAdapter._recipe_member(members, value)
        try:
            with path.open("rb") as stream:
                return tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise FlowExecutionError(
                f"structural-link {label} snapshot is unreadable"
            ) from exc

    @staticmethod
    def _safe_relative(value: str, label: str) -> PurePosixPath:
        relative = PurePosixPath(value)
        if (
            not value
            or not relative.parts
            or relative.is_absolute()
            or "\\" in value
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise FlowExecutionError(f"structural-link {label} path is unsafe")
        return relative

    @staticmethod
    def _artifact_member(
        artifact_root: Path,
        relative: PurePosixPath,
        label: str,
    ) -> Path:
        candidate = artifact_root
        for component in relative.parts:
            candidate /= component
            if candidate.is_symlink():
                raise FlowExecutionError(
                    f"structural-link {label} path contains a symlink"
                )
        if not candidate.resolve().is_relative_to(artifact_root):
            raise FlowExecutionError(
                f"structural-link {label} escaped artifact root"
            )
        return candidate

    @staticmethod
    def _project_member_path(context: ActionContext, value: object) -> str:
        if not isinstance(value, str):
            raise FlowExecutionError(
                "structural-link owner member path must be text"
            )
        owner_relative = SynopsysStructuralLinkAdapter._safe_relative(
            value,
            "owner member",
        )
        scope = context.require_project_scope()
        owner_prefix = scope.owner_root.relative_to(
            scope.project.project_root
        ).as_posix()
        return f"{owner_prefix}/{owner_relative.as_posix()}"

    @staticmethod
    def _owner_relative_project_path(
        context: ActionContext,
        value: object,
        label: str,
    ) -> str:
        if not isinstance(value, str):
            raise FlowExecutionError(f"structural-link {label} path must be text")
        project_relative = SynopsysStructuralLinkAdapter._safe_relative(value, label)
        scope = context.require_project_scope()
        owner_prefix = PurePosixPath(
            scope.owner_root.relative_to(scope.project.project_root).as_posix()
        )
        try:
            return project_relative.relative_to(owner_prefix).as_posix()
        except ValueError as exc:
            raise FlowExecutionError(
                f"structural-link {label} escaped owner root"
            ) from exc

    @staticmethod
    def _owner_member_from_project_path(
        context: ActionContext,
        members: Mapping[str, Path],
        value: object,
        label: str,
    ) -> Path:
        relative = SynopsysStructuralLinkAdapter._owner_relative_project_path(
            context,
            value,
            label,
        )
        return SynopsysStructuralLinkAdapter._recipe_member(members, relative)

    def _source_members(
        self,
        context: ActionContext,
        integration: Mapping[str, Any],
    ) -> tuple[tuple[str, Path], ...]:
        artifact = context.input("rtl-sources")
        members = _manifest_members(
            artifact.path,
            artifact.kind,
            artifact.qualifiers,
        )
        expected: list[str] = []
        for value in integration.get("source_files", ()):
            if not isinstance(value, str):
                raise FlowExecutionError(
                    "structural-link integration source identity is invalid"
                )
            expected.append(
                self._owner_relative_project_path(context, value, "RTL source")
            )
        if tuple(relative for relative, _path in members) != tuple(expected):
            raise FlowExecutionError(
                "structural-link RTL source snapshot drifted from IP integration"
            )
        return members

    def _released_macro(
        self,
        context: ActionContext,
        recipe: Mapping[str, Any],
        integration: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any], Path]:
        releases = integration.get("dependency_releases")
        if not isinstance(releases, list) or len(releases) != 1:
            raise FlowExecutionError(
                "structural-link requires one immutable macro release"
            )
        released = releases[0]
        roles = released.get("roles") if isinstance(released, Mapping) else None
        role = recipe["liberty_role"]
        if not isinstance(roles, Mapping) or set(roles) != {role}:
            raise FlowExecutionError(
                "structural-link dependency did not resolve the declared Liberty role"
            )
        relative = roles[role]
        if not isinstance(relative, str):
            raise FlowExecutionError("structural-link Liberty identity is invalid")
        artifact_root = context.require_project_scope().project.artifact_root.resolve()
        macro_liberty = self._artifact_member(
            artifact_root,
            self._safe_relative(relative, "released Liberty"),
            "released Liberty",
        )
        if not macro_liberty.is_file():
            raise FlowExecutionError(
                "structural-link released Liberty artifact is unavailable"
            )
        return released, macro_liberty

    def _pinned_release_source_sha256(
        self,
        context: ActionContext,
        recipe: Mapping[str, Any],
        released: Mapping[str, Any],
        macro_liberty: Path,
    ) -> str:
        artifact_root = context.require_project_scope().project.artifact_root.resolve()
        try:
            macro_relative = PurePosixPath(
                macro_liberty.relative_to(artifact_root).as_posix()
            )
        except ValueError as exc:
            raise FlowExecutionError(
                "structural-link released Liberty escaped artifact root"
            ) from exc
        macro_liberty = self._artifact_member(
            artifact_root,
            macro_relative,
            "released Liberty",
        )
        manifest_value = released.get("manifest")
        source_commit = released.get("source_commit")
        if (
            not isinstance(manifest_value, str)
            or not isinstance(source_commit, str)
            or re.fullmatch(r"[0-9a-f]{40}", source_commit) is None
        ):
            raise FlowExecutionError(
                "structural-link release provenance is incomplete"
            )
        manifest_relative = self._safe_relative(
            manifest_value,
            "release manifest",
        )
        manifest = self._artifact_member(
            artifact_root,
            manifest_relative,
            "release manifest",
        )
        if not manifest.is_file():
            raise FlowExecutionError(
                "structural-link release manifest is unavailable"
            )
        try:
            manifest_bytes = manifest.read_bytes()
            raw = json.loads(manifest_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FlowExecutionError(
                "structural-link release manifest is unreadable"
            ) from exc
        if sha256(manifest_bytes).hexdigest() != released.get("manifest_sha256"):
            raise FlowExecutionError(
                "structural-link release manifest drifted from its dependency lock"
            )
        views = raw.get("views") if isinstance(raw, Mapping) else None
        if not isinstance(views, list):
            raise FlowExecutionError("structural-link release views are invalid")
        matching_views: list[Mapping[str, Any]] = []
        for value in views:
            if not isinstance(value, Mapping) or value.get("role") != recipe["liberty_role"]:
                continue
            view_path = value.get("path")
            if not isinstance(view_path, str):
                continue
            relative = PurePosixPath(view_path)
            if (
                relative.is_absolute()
                or "\\" in view_path
                or any(part in {"", ".", ".."} for part in relative.parts)
            ):
                continue
            candidate = self._artifact_member(
                artifact_root,
                manifest_relative.parent / relative,
                "release source view",
            )
            if candidate.resolve() == macro_liberty.resolve():
                matching_views.append(value)
        if len(matching_views) != 1:
            raise FlowExecutionError(
                "structural-link release has no unique Liberty source view"
            )
        source_value = matching_views[0].get("source")
        if not isinstance(source_value, str):
            raise FlowExecutionError(
                "structural-link release Liberty omitted its source identity"
            )
        source = PurePosixPath(source_value)
        if (
            source.is_absolute()
            or "\\" in source_value
            or any(part in {"", ".", ".."} for part in source.parts)
        ):
            raise FlowExecutionError(
                "structural-link release Liberty source path is unsafe"
            )
        try:
            content = run_readonly_capture(
                ("git", "show", f"{source_commit}:{source.as_posix()}"),
                cwd=context.require_project_scope().project.project_root,
            )
        except Exception as exc:
            raise FlowExecutionError(
                "structural-link cannot read the pinned Liberty source blob"
            ) from exc
        return sha256(content).hexdigest()

    def _variant_top(
        self,
        context: ActionContext,
        recipe: Mapping[str, Any],
        members: Mapping[str, Path],
    ) -> str:
        variant = self._variant(context)
        if variant not in recipe["variants"]:
            raise FlowExecutionError(
                f"structural-link recipe does not support variant {variant!r}"
            )
        path = self._recipe_member(members, recipe["variants"][variant])
        try:
            with path.open("rb") as stream:
                raw = tomllib.load(stream)
            top = raw["filesets"][recipe["fileset"]]["top_module"]
        except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
            raise FlowExecutionError(
                "structural-link variant omitted its synthesis top"
            ) from exc
        if not isinstance(top, str) or _VERILOG_IDENTIFIER.fullmatch(top) is None:
            raise FlowExecutionError("structural-link top must be an identifier")
        return top

    @staticmethod
    def _variant(context: ActionContext) -> str:
        variant = context.action_config.get("variant")
        if not isinstance(variant, str) or not variant:
            raise FlowExecutionError("structural-link Action requires a variant")
        qualifiers = context.input("rtl-sources").qualifiers
        if qualifiers.get("variant") != variant:
            raise FlowExecutionError(
                "structural-link variant does not match RTL source qualifiers"
            )
        return variant

    @staticmethod
    def _recipe_member(members: Mapping[str, Path], value: object) -> Path:
        if not isinstance(value, str) or value not in members:
            raise FlowExecutionError(
                "structural-link recipe member is not pinned by its typed input"
            )
        return members[value]

    def _executables(self, context: ActionContext) -> tuple[Path, Path]:
        paths: list[Path] = []
        for name in self._CAPABILITIES:
            capability = context.capabilities.get(name)
            executable = None if capability is None else capability.executable
            if (
                executable is None
                or not executable.is_file()
                or not os.access(executable, os.X_OK)
            ):
                raise FlowExecutionError(
                    f"structural-link capability {name!r} is unavailable"
                )
            paths.append(executable)
        return paths[0], paths[1]

    @staticmethod
    def _timeout(context: ActionContext) -> int:
        timeout = context.adapter_config.get("timeout_seconds")
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
            raise FlowExecutionError(
                "structural-link Adapter configuration requires a positive timeout_seconds"
            )
        return timeout


