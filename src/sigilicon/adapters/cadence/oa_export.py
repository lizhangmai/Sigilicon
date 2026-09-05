"""Export one owned OA view under a read lease, without running a generator."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from sigilicon.artifacts import read_nofollow_text
from sigilicon.canonical import canonical_digest
from sigilicon.contracts import ContractReader
from sigilicon.domain.oa_library import find_oa_assembly, load_oa_library_source
from sigilicon.domain.platform import load_platform
from sigilicon.execution.adapter import AdapterPreparation
from sigilicon.execution._values import ContractError, ExecutionError
from sigilicon.execution._io import ExecutionIO
from sigilicon.execution._plan import PreflightCheck, Step
from sigilicon.execution._resources import ResourceBinding, Resources
from sigilicon.execution._source import Source
from sigilicon.execution._result import StepResult
from sigilicon.execution._workspace import ExecutionWorkspace
from sigilicon.external_tools import owned_input_file, owned_scratch_directory, process_group_cleanup_uncertainty
from sigilicon.project import Project
from sigilicon.virtuoso.workspace import OperationPolicy, workspace_operation
from sigilicon.virtuoso.xstream import XStreamExportError, XStreamExportRequest, run_xstream_export
from sigilicon.adapters.cadence._common import (
    _BRIDGE_RESOURCES,
    _OA_CAPABILITIES,
    _bridge_check,
    _capability_checks,
    _executable_check,
)


@dataclass(frozen=True)
class OaExportRequest:
    owner: str
    library: str
    cell: str
    view: str
    technology_library: str
    workspace_root: Path
    layermap_resource: str
    cds_lib_resource: str
    flatten_pcells: bool
    suppressed_warnings: tuple[str, ...]
    timeout_seconds: int

    @property
    def record(self) -> dict:
        return {"owner": self.owner, "library": self.library, "cell": self.cell, "view": self.view,
                "technology_library": self.technology_library, "workspace_root": str(self.workspace_root),
                "layermap_resource": self.layermap_resource, "cds_lib_resource": self.cds_lib_resource,
                "flatten_pcells": self.flatten_pcells,
                "suppressed_warnings": list(self.suppressed_warnings), "timeout_seconds": self.timeout_seconds}

    @property
    def identity(self) -> str:
        return canonical_digest(self.record)


def export_oa_stream(
    request: OaExportRequest, client: Any, *, artifacts: ExecutionWorkspace, resources: Resources,
    layermap_source: str, cds_lib_source: str, operation_id: str, bind_operation: Callable[[Any], None],
    record_uncertainty: Callable[[str], None],
) -> Path:
    layermap = artifacts.write_text("inputs", ("layermap",), layermap_source)
    cds_lib = request.workspace_root / "cds.lib"
    if not cds_lib.is_file() or cds_lib.is_symlink():
        raise RuntimeError(f"workspace cds.lib is unavailable: {cds_lib}")
    operation = None
    deferred = None
    try:
        with owned_input_file(cds_lib, require_single_link=False), workspace_operation(client, request.workspace_root, "export-layout",
                                 policy=OperationPolicy.READ_ONLY, operation_id=operation_id) as operation:
            bind_operation(operation)
            if read_nofollow_text(cds_lib) != cds_lib_source:
                raise RuntimeError("OA export cds.lib changed after planning")
            with operation.view_lease(request.library, cells=(request.cell,), views=((request.cell, request.view),)):
                if operation.require_project_library_target(client, request.library) != request.workspace_root / request.library:
                    raise RuntimeError("OA export library does not resolve to its expected workspace directory")
                info = client.library.get(request.library, timeout=30)
                if str(info.technology_library or "") != request.technology_library:
                    raise RuntimeError("OA export library uses an unexpected technology")
                try:
                    with resources.owned_tool("cadence.xstream") as launcher:
                        exported = run_xstream_export(
                            XStreamExportRequest(
                                library=request.library, cell=request.cell, view=request.view,
                                technology_library=request.technology_library, layer_map=layermap,
                                cds_lib=cds_lib, work_root=artifacts.directory("work"),
                                timeout_seconds=request.timeout_seconds, flatten_pcells=request.flatten_pcells,
                                suppressed_warnings=request.suppressed_warnings,
                            ), launcher=launcher, environment=resources.environment,
                        )
                except XStreamExportError as exc:
                    for name in ("strmout.log", "strmout.sum"):
                        path = artifacts.directory("work") / name
                        if path.is_file() and not path.is_symlink():
                            artifacts.copy_file("outputs", (name,), path)
                    if exc.diagnostic_path is not None and exc.diagnostic_path.is_file():
                        artifacts.copy_file("outputs", ("xstream-failure.log",), exc.diagnostic_path)
                    raise
                artifacts.write_text("outputs", ("xstream-stdout.log",), exported.stdout)
                artifacts.write_text("outputs", ("xstream-stderr.log",), exported.stderr)
                artifacts.copy_file("outputs", ("strmout.log",), exported.native_log_path)
                artifacts.copy_file("outputs", ("strmout.sum",), exported.summary_path)

                def commit() -> Path:
                    return artifacts.copy_file("outputs", ("layout.gds",), exported.gds_path)

                deferred = operation.defer_commit(commit)
    except BaseException:
        reason = getattr(operation, "uncertain_reason", None)
        if isinstance(reason, str) and reason:
            record_uncertainty(reason)
        raise
    if deferred is None or not deferred.completed:
        raise RuntimeError("OA export completed without a committed stream")
    return artifacts.output_root / "layout.gds"


class OaExportAdapter:
    name = "cadence.oa-export"

    def prepare(self, project: Project, step: Step, resources: Resources) -> AdapterPreparation:
        config = ContractReader(step.config, "OA export config")
        owner = config.text("owner")
        cell = config.text("cell")
        view = config.text("view")
        timeout = config.integer("timeout_seconds", minimum=1)
        config.finish()
        manifest = find_oa_assembly(project, project.owner(owner).root)
        if manifest is None:
            raise ContractError("OA export requires an owner assembly")
        assembly = load_oa_library_source(manifest, project=project)
        selected = [item for item in assembly.cells if item.cell == cell]
        if len(selected) != 1 or not any(item.name == view and item.kind == "layout" for item in selected[0].views):
            raise ContractError("OA export must select an owned layout view")
        platform = load_platform(project, assembly.pdk, resources=resources)
        if platform.oa is None or platform.layout is None or platform.layout.layermap is None:
            raise ContractError("OA export requires OA technology and a layout stream map")
        identity = f"pdk:{platform.key}:layout/layermap"
        cds_identity = f"oa:{owner}:cds-lib"
        action = OaExportRequest(owner, assembly.name, cell, view, platform.oa.technology_library,
                                 assembly.workspace_root, identity, cds_identity, platform.layout.xstream_flatten_pcells,
                                 platform.layout.xstream_suppressed_warnings, timeout)
        owner_root = project.owner(owner).root
        documents = {**assembly.source_documents, **platform.source_documents,
                     platform.source_paths[0]: platform.catalog_document}
        return AdapterPreparation(action=action,
            sources=tuple(Source.capture_document(path, document=document,
                                         root=owner_root if path.is_relative_to(owner_root) else project.project_root,
                                         scope="owner" if path.is_relative_to(owner_root) else "project")
                          for path, document in sorted(documents.items())),
            resources=(ResourceBinding.capture(platform.layout.layermap.require_path(), identity=identity),
                       ResourceBinding.capture(assembly.workspace_root / "cds.lib", identity=cds_identity),
                       *(resources.capture(name) for name in (*_BRIDGE_RESOURCES, "cadence.xstream"))))

    def preflight(self, step: Step, resources: Resources) -> tuple[PreflightCheck, ...]:
        step.validate_action()
        if not isinstance(step.action, OaExportRequest):
            raise ContractError("OA export requires its compiled request")
        return (_bridge_check(resources), _executable_check(resources, "cadence.xstream"),
                *_capability_checks(resources, _OA_CAPABILITIES))

    def run(self, context: ExecutionIO) -> StepResult:
        from sigilicon.adapters.cadence.oa_client import get_client

        context.step.validate_action()
        action = context.step.action
        if not isinstance(action, OaExportRequest):
            raise ExecutionError("OA export requires its compiled request")
        uncertainty = []
        try:
            with owned_scratch_directory(prefix=f"sigilicon-export-{context.run_id}-",
                    retain_on_error=lambda exc: bool(uncertainty) or process_group_cleanup_uncertainty(exc) is not None) as scratch:
                workspace = context.workspace("export", action.record, tool_work_root=scratch.path)
                stream = export_oa_stream(action, get_client(context.runtime), artifacts=workspace,
                    resources=context.runtime, layermap_source=read_nofollow_text(context.resource_path(action.layermap_resource)),
                    cds_lib_source=read_nofollow_text(context.resource_path(action.cds_lib_resource)),
                    operation_id=context.operation_id, bind_operation=context.register_mutation,
                    record_uncertainty=uncertainty.append)
                output = context.copy_output("layout-stream", "layout.gds", stream, "layout.gds")
        except Exception:
            if uncertainty:
                return StepResult("uncertain", context.output_artifacts("export", "evidence.oa-export"), message=" | ".join(uncertainty))
            raise
        return StepResult.succeeded(artifacts=(output, *context.output_artifacts("export", "evidence.oa-export")))
