"""Capture explicit native authoring content as a managed, non-conclusive artifact."""
from __future__ import annotations
from dataclasses import dataclass
import json
from pathlib import Path

from sigilicon.canonical import canonical_digest
from sigilicon.contracts import ContractReader
from sigilicon.domain.oa_library import find_oa_assembly, load_oa_library_source
from sigilicon.domain.oa_snapshot import NativeOaSnapshot
from sigilicon.domain.platform import load_platform
from sigilicon.execution.adapter import AdapterPreparation
from sigilicon.execution.artifact_reference import ArtifactProduct, StepContract
from sigilicon.execution._result import Artifact, StepResult
from sigilicon.execution._source import Source
from sigilicon.execution._values import ContractError
from sigilicon.virtuoso.workspace import OperationPolicy, workspace_operation
from sigilicon.adapters.cadence._common import _BRIDGE_RESOURCES, _OA_CAPABILITIES, _bridge_check, _capability_checks


@dataclass(frozen=True)
class NativeCaptureRequest:
    library: str
    cell: str
    view: str
    technology_library: str
    workspace_root: Path

    @property
    def record(self) -> dict:
        return {"library": self.library, "cell": self.cell, "view": self.view,
                "technology_library": self.technology_library, "workspace_root": str(self.workspace_root)}

    @property
    def identity(self) -> str:
        return canonical_digest(self.record)


class OaCaptureAdapter:
    name = "cadence.oa-capture"

    def _configuration(self, project, step, resources=None):
        reader = ContractReader(step.config, "OA capture")
        owner, cell, view = reader.text("owner"), reader.text("cell"), reader.text("view")
        reader.finish()
        manifest = find_oa_assembly(project, project.owner(owner).root)
        if manifest is None:
            raise ContractError("OA capture requires an owner assembly")
        assembly = load_oa_library_source(manifest, project=project)
        if not any(item.cell == cell and any(row.name == view for row in item.views) for item in assembly.cells):
            raise ContractError("OA capture must select an owned view")
        platform = load_platform(
            project, owner, assembly.pdk, resources=resources
        )
        if platform.oa is None:
            raise ContractError("OA capture requires a technology binding")
        return owner, cell, view, assembly, platform

    def contract(self, project, step):
        self._configuration(project, step)
        return StepContract(produces=(ArtifactProduct("native-source", "source.native-oa", path="view.json"),))

    def prepare(self, project, step, resources):
        owner, cell, view, assembly, platform = self._configuration(project, step, resources)
        owner_root = project.owner(owner).root
        documents = {**assembly.source_documents, **platform.source_documents,
                     platform.source_paths[0]: platform.catalog_document}
        return AdapterPreparation(
            action=NativeCaptureRequest(assembly.name, cell, view, platform.oa.technology_library, assembly.workspace_root),
            sources=tuple(Source.capture_document(path, document=document, root=owner_root if path.is_relative_to(owner_root) else project.project_root,
                                                          scope="owner" if path.is_relative_to(owner_root) else "project") for path, document in sorted(documents.items())),
            resources=tuple(resources.capture(name) for name in _BRIDGE_RESOURCES),
        )

    def preflight(self, step, resources):
        step.validate_action()
        return (_bridge_check(resources), *_capability_checks(resources, _OA_CAPABILITIES))

    def run(self, context):
        from sigilicon.adapters.cadence.oa_client import get_client
        context.step.validate_action()
        action = context.step.action
        client = get_client(context.runtime)
        with workspace_operation(client, action.workspace_root, "capture-native-oa-source", policy=OperationPolicy.READ_ONLY,
                                 operation_id=context.operation_id) as operation, operation.view_lease(
                action.library, cells=(action.cell,), views=((action.cell, action.view),)):
            context.register_mutation(operation)
            library = operation.require_project_library_target(client, action.library)
            info = client.library.get(action.library, timeout=30)
            if str(info.technology_library or "") != action.technology_library:
                raise ValueError("native OA capture technology drift")
            snapshot = NativeOaSnapshot.capture(library / action.cell / action.view, library=action.library,
                                               cell=action.cell, view=action.view, technology_library=action.technology_library)
            path = context.write_text("native-source", "view.json", json.dumps(snapshot.record, sort_keys=True) + "\n")
        return StepResult.succeeded(artifacts=(Artifact("native-source", "source.native-oa", path),))
