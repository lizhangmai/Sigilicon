"""Export one owned OA view under a read lease, without running a generator."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Callable

from sigilicon.artifacts import read_nofollow_text
from sigilicon.canonical import canonical_digest
from sigilicon.contracts import ContractReader
from sigilicon.domain.oa_snapshot import NativeOaSnapshot
from sigilicon.source import SourceReference
from sigilicon.virtuoso.oa_snapshot import attest_native_snapshot
from sigilicon.domain.oa_library import find_oa_assembly, load_oa_library_source
from sigilicon.domain.platform import load_platform
from sigilicon.execution.adapter import AdapterPreparation
from sigilicon.execution.artifact_reference import ArtifactProduct, StepContract
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
    snapshots: tuple[NativeOaSnapshot, ...]

    @property
    def record(self) -> dict:
        return {"source_snapshots": [item.identity for item in self.snapshots], "owner": self.owner, "library": self.library, "cell": self.cell, "view": self.view,
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
            with operation.view_lease(request.library, cells=tuple(sorted({item.cell for item in request.snapshots})),
                                      views=tuple((item.cell, item.view) for item in request.snapshots)):
                if operation.require_project_library_target(client, request.library) != request.workspace_root / request.library:
                    raise RuntimeError("OA export library does not resolve to its expected workspace directory")
                info = client.library.get(request.library, timeout=30)
                if str(info.technology_library or "") != request.technology_library:
                    raise RuntimeError("OA export library uses an unexpected technology")
                for snapshot in request.snapshots:
                    attest_native_snapshot(snapshot, client, operation)
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
                for snapshot in request.snapshots:
                    attest_native_snapshot(snapshot, client, operation)
                artifacts.write_json("outputs", ("source-identity.json",), {"snapshots": [item.identity for item in request.snapshots]})
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

    def _configuration(self, project: Project, step: Step, resources=None):
        config = ContractReader(step.config, "OA export config")
        owner = config.text("owner")
        cell = config.text("cell")
        view = config.text("view")
        timeout = config.integer("timeout_seconds", minimum=1)
        snapshot_refs = config.take("snapshots", ())
        config.finish()
        manifest = find_oa_assembly(project, project.owner(owner).root)
        if manifest is None:
            raise ContractError("OA export requires an owner assembly")
        assembly = load_oa_library_source(manifest, project=project)
        selected = [item for item in assembly.cells if item.cell == cell]
        if len(selected) != 1 or not any(item.name == view and item.kind in {"layout", "native_oa"} for item in selected[0].views):
            raise ContractError("OA export must select an owned layout view")
        platform = load_platform(project, assembly.pdk, resources=resources)
        if platform.oa is None or platform.layout is None or platform.layout.layermap is None:
            raise ContractError("OA export requires OA technology and a layout stream map")
        explicit = {}
        source_closure = {item.reference: item for item in step.source_closure}
        for row in snapshot_refs:
            reader = ContractReader(row, "OA export snapshot")
            reference = SourceReference(reader.text("component"), reader.text("source"))
            reader.finish()
            if reference not in source_closure:
                raise ContractError("OA export snapshot must be selected by a component fileset")
            path = source_closure[reference].location
            snapshot = NativeOaSnapshot.load(path)
            key = (snapshot.cell, snapshot.view)
            if key in explicit:
                raise ContractError("duplicate OA snapshot view")
            explicit[key] = (snapshot, path)
        views = {(item.cell, declared.name): declared for item in assembly.cells for declared in item.views}
        pending = [(cell, view)]
        snapshots = {}
        while pending:
            key = pending.pop()
            if key in snapshots:
                continue
            declared = views[key]
            if key in explicit:
                snapshot, path = explicit.pop(key)
            elif declared.kind == "native_oa":
                path = declared.source
                snapshot = NativeOaSnapshot.load(path)
            else:
                raise ContractError("OA export requires an explicit native snapshot for every consumed view")
            if (snapshot.library, snapshot.cell, snapshot.view, snapshot.technology_library) != (assembly.name, *key, platform.oa.technology_library):
                raise ContractError("OA export source snapshot identity drift")
            snapshots[key] = (snapshot, path)
            pending.extend((item.cell, item.view) for item in declared.dependencies)
        if explicit:
            raise ContractError("OA export snapshots contain unconsumed views")
        identity = f"pdk:{platform.key}:layout/layermap"
        cds_identity = f"oa:{owner}:cds-lib"
        action = OaExportRequest(owner, assembly.name, cell, view, platform.oa.technology_library,
                                 assembly.workspace_root, identity, cds_identity, platform.layout.xstream_flatten_pcells,
                                 platform.layout.xstream_suppressed_warnings, timeout,
                                 tuple(item[0] for _, item in sorted(snapshots.items())))
        return action, assembly, platform, snapshots

    def contract(self, project: Project, step: Step) -> StepContract:
        self._configuration(project, step)
        return StepContract(produces=(ArtifactProduct("layout-stream", "layout.gds", path="layout.gds"),
                                      ArtifactProduct("export", "evidence.oa-export", "many")))

    def prepare(self, project: Project, step: Step, resources: Resources) -> AdapterPreparation:
        action, assembly, platform, snapshots = self._configuration(project, step, resources)
        owner, identity, cds_identity = action.owner, action.layermap_resource, action.cds_lib_resource
        owner_root = project.owner(owner).root
        documents = {**assembly.source_documents, **platform.source_documents,
                     platform.source_paths[0]: platform.catalog_document}
        native_sources = []
        for snapshot, path in snapshots.values():
            source = Source.capture(path, root=owner_root if path.is_relative_to(owner_root) else project.project_root,
                                    scope="owner" if path.is_relative_to(owner_root) else "project")
            if json.loads(source.read_text()) != snapshot.record:
                raise ContractError("native OA export snapshot changed during planning")
            native_sources.append(source)
        return AdapterPreparation(action=action,
            sources=tuple(Source.capture_document(path, document=document,
                                         root=owner_root if path.is_relative_to(owner_root) else project.project_root,
                                         scope="owner" if path.is_relative_to(owner_root) else "project")
                          for path, document in sorted(documents.items())) +
                    tuple(native_sources),
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
