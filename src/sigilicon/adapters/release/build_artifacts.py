"""Publish generated collateral over an immutable source package and audited run."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Mapping

from sigilicon.artifacts import read_nofollow_bytes
from sigilicon.contracts import ContractReader, freeze_toml_document, thaw_toml_document
from sigilicon.execution.adapter import AdapterPreparation
from sigilicon.execution.artifact_reference import ArtifactReference, ArtifactProduct, StepContract
from sigilicon.execution._source import Source
from sigilicon.execution._resources import ResourceBinding
from sigilicon.execution._result import Artifact, StepResult
from sigilicon.execution._plan import PreflightCheck
from sigilicon.execution._values import ContractError, ExecutionError
from sigilicon.execution.runs import RunStore
from sigilicon.release_store import ReleaseRef, ReleaseStore, release_store_resource
from sigilicon.adapters.release.ip_packaging import validate_ip_release_package
from sigilicon.adapters.release.release_semantics import ExportSemantics
from sigilicon.adapters.release.run_evidence import validate_execution
from sigilicon.adapters.release.source_control import inspect_checkout, verify_source_commit


@dataclass(frozen=True)
class BuildAction:
    manifest: Mapping
    proof: Mapping
    views: tuple[Mapping, ...]
    resources: tuple[ResourceBinding, ...]
    source_paths: tuple[tuple[str, str], ...]
    base_paths: tuple[tuple[str, str], ...]
    artifact_paths: tuple[str, ...]
    project_root: Path
    checkout: str
    store: str
    destination: Path

    @property
    def record(self) -> dict:
        return {"base": thaw_toml_document(self.manifest), "run": thaw_toml_document(self.proof),
                "views": [thaw_toml_document(view) for view in self.views], "checkout": self.checkout}


class BuildArtifactReleaseAdapter:
    name = "sigilicon.release-artifacts"

    def _configuration(self, step):
        config = ContractReader(step.config, "build artifact release")
        base = ContractReader(config.table("base"), "source package reference")
        reference = ReleaseRef(base.text("store"), base.text("manifest_sha256"))
        base.finish()
        maturity = config.text("maturity", "development")
        run = config.table("run")
        views = config.take("views")
        config.finish()
        if not isinstance(views, (tuple, list)) or not views:
            raise ContractError("build release requires generated view declarations")
        if maturity not in {"development", "implementation", "signoff"}:
            raise ContractError("unsupported build release maturity")
        declared = []
        for raw in views:
            reader = ContractReader(raw, "generated release view")
            row = {key: reader.text(key) for key in ("export", "name", "role", "format", "package_path")}
            row.update({key: reader.take(key, default) for key, default in
                        (("capabilities", ()), ("condition", {}), ("variant", None),
                         ("library", None), ("cell", None), ("view", None))})
            selection = ArtifactReference.from_record(reader.table("artifact"))
            reader.finish()
            if selection.cardinality != "one":
                raise ContractError("each release view must select one generated artifact")
            expected_kinds = {"gds": "layout.gds", "oasis": "layout.oasis", "cdl": "netlist.cdl",
                              "lef": "abstract.lef", "liberty": "library.liberty", "db": "library.synopsys-db"}
            if row["format"] in expected_kinds and selection.kind != expected_kinds[row["format"]]:
                raise ContractError("generated physical view format disagrees with its artifact kind")
            row["artifact"] = selection.record
            declared.append(freeze_toml_document(row))
        return reference, maturity, run, tuple(declared)

    def contract(self, project, step):
        self._configuration(step)
        return StepContract(produces=(ArtifactProduct("release", "summary.ip-release", "many"),))

    def prepare(self, project, step, resources):
        reference, maturity, run, views = self._configuration(step)
        destination = resources.require_destination(release_store_resource(reference.store))
        package = ReleaseStore(destination).open(reference, validate=validate_ip_release_package)
        if package.manifest.get("release_kind") != "source-package":
            raise ContractError("build release base must be a source package")
        from sigilicon.domain.ip_release import load_ip_contract
        owner_contract = project.owner(package.manifest["owner"]).release_contract
        contract = load_ip_contract(owner_contract, project=project)
        manifest = json.loads(json.dumps(dict(package.manifest)))
        manifest["maturity"]["level"] = maturity
        for exported in manifest["exports"]:
            declaration = contract.get_export(exported["name"])
            exported["maturity"]["required_views"] = list(declaration.required_views[maturity])
            exported["receipts"] = {name: policy.record for name, policy in declaration.receipts.items()}
        materialization = RunStore(project.artifact_root).materialization_plan(**dict(run))
        if materialization.result.owner != package.manifest.get("owner"):
            raise ContractError("generated collateral must belong to the source package owner")
        declared = views
        artifacts = [materialization.artifacts(ArtifactReference.from_record(row["artifact"]))[0].path
                     for row in declared]
        source_paths = []
        owner = project.owner(materialization.result.owner)
        for row in materialization.execution_plan["sources"]:
            original = (owner.root if row["scope"] == "owner" else project.project_root) / row["path"]
            sealed = materialization.result.run_root / "inputs/sources" / row["path"]
            if original.is_relative_to(project.project_root) and original.relative_to(project.project_root).as_posix() in package.manifest["source_files"]:
                source_paths.append((original, sealed))
        if not source_paths:
            raise ContractError("execution has no source closure shared with the source package")
        verify_source_commit(project.project_root, resources, package.manifest["source_commit"], dict(source_paths))
        selected = {package.manifest_path, *artifacts, *(artifact.path for artifact in package.artifacts),
                    *(path for _, path in source_paths)}
        captured = tuple(ResourceBinding.capture(path, identity=f"release-input:{index}")
                         for index, path in enumerate(sorted(selected)))
        names = {binding.location: binding.identity for binding in captured}
        checkout = inspect_checkout(project.project_root, resources)
        action = BuildAction(freeze_toml_document(manifest),
                             freeze_toml_document(materialization.record), tuple(declared), captured,
                             tuple((str(original), names[sealed]) for original, sealed in source_paths),
                             tuple((item.relative_path, names[item.path]) for item in package.artifacts),
                             tuple(names[path] for path in artifacts), project.project_root, checkout.commit,
                             reference.store, destination)
        return AdapterPreparation(action=action,
                                  sources=(Source.capture_document(owner_contract, document=contract.document, root=owner.root, scope="owner"),),
                                  resources=(*captured, resources.capture(release_store_resource(reference.store)),
                                             resources.capture("vcs.git")))

    def preflight(self, step, resources):
        action = step.action
        checkout = inspect_checkout(action.project_root, resources)
        ready = checkout.commit == action.checkout and not checkout.working_tree_dirty
        return (PreflightCheck("source-checkout", action.checkout, "ready" if ready else "blocked",
                               "clean build publication checkout"),)

    def run(self, context):
        context.step.validate_action()
        action = context.step.action
        checkout = inspect_checkout(action.project_root, context.runtime)
        if checkout.working_tree_dirty or checkout.commit != action.checkout:
            raise ExecutionError("build publication checkout changed")
        manifest = thaw_toml_document(action.manifest)
        verify_source_commit(action.project_root, context.runtime, manifest["source_commit"],
                             {Path(original): context.resource_path(name) for original, name in action.source_paths})
        payloads = {path: context.resource_path(name) for path, name in action.base_paths}
        exports = {row["name"]: row for row in manifest["exports"]}
        receipts = []
        for declared, source_name in zip(action.views, action.artifact_paths):
            row = thaw_toml_document(declared)
            selection = row.pop("artifact")
            row["path"] = row.pop("package_path")
            exported = exports[row["export"]]
            if row["name"] in exported.get("receipts", {}):
                receipts.append((row, selection, source_name))
            else:
                source = context.resource_path(source_name)
                payload = read_nofollow_bytes(source)
                row.update(size=len(payload), sha256=hashlib.sha256(payload).hexdigest(),
                           generated_from={"run_id": action.proof["result"]["run_id"], "artifact": selection})
                payloads[row["path"]] = source
            old = next((item for item in manifest["views"] if (item["export"], item["name"]) == (row["export"], row["name"])), None)
            if old is not None:
                manifest["views"].remove(old)
                if old["path"] != row["path"]:
                    payloads.pop(old["path"])
            manifest["views"].append(row)
        # Receipt identities exclude receipt bytes, preventing a circular content identity.
        for row, selection, source_name in receipts:
            exported = exports[row["export"]]
            for candidate in manifest["views"]:
                if candidate["name"] in exported.get("receipts", {}) and "sha256" not in candidate:
                    candidate.update(size=0, sha256="0" * 64)
            semantics = ExportSemantics.from_record(exported, manifest["views"])
            execution = {"kind": "managed-run", "proof": thaw_toml_document(action.proof),
                         "reference": selection, "payload": context.resource_text(source_name)}
            validate_execution(execution)
            policy = semantics.receipts[row["name"]]
            views = {view.name: view for view in semantics.views}
            receipt = {"schema": 4, "contract_kind": "release-receipt", "name": row["name"],
                       "status": "passed", "source_identity": semantics.source_identity,
                       "subject": semantics.subject, "execution": execution,
                       "tool": {"name": next(step["uses"] for step in action.proof["result"]["steps"] if step["id"] == selection["step"]),
                                "version": "software-identity:" + str(action.proof["result"]["plan_identity"])},
                       "variant": row["variant"], "condition": row["condition"], "coverage": list(policy.coverage),
                       **{field: [{"name": name, "size": views[name].size, "sha256": views[name].sha256}
                                  for name in getattr(policy, field)] for field in ("inputs", "outputs")}}
            path = context.workspace("release", {}).write_text("work", (row["name"] + ".json",), json.dumps(receipt))
            payload = read_nofollow_bytes(path)
            row.update(size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
            payloads[row["path"]] = path
        availability = []
        for exported in manifest["exports"]:
            semantics = ExportSemantics.from_record(exported, manifest["views"])
            def read_receipt(name):
                row = next(view for view in manifest["views"] if view["export"] == exported["name"] and view["name"] == name)
                return json.loads(read_nofollow_bytes(payloads[row["path"]]))
            available, problems = semantics.assess(manifest["maturity"]["level"], read_receipt)
            if problems:
                raise ExecutionError("generated release is incomplete: " + ", ".join(problems))
            exported["availability"] = available.record
            exported["maturity"]["missing_items"] = []
            availability.append(available.record)
        manifest["availability"] = {key: all(row[key] for row in availability) for key in availability[0]}
        manifest["maturity"].update(missing_items=[], checks=[{"name": "generated-collateral-and-evidence", "passed": True}])
        manifest["release_kind"] = "build-artifact-package"
        manifest["release_id"] = "build-" + action.proof["result"]["run_id"]
        manifest["provenance"]["build"] = {"source_commit": action.checkout, "execution": thaw_toml_document(action.proof)}
        package = ReleaseStore(action.destination).publish(action.store, manifest, payloads, validate=validate_ip_release_package)
        summary = context.write_text("release", "summary.json", json.dumps({"store": package.ref.store,
                    "manifest_sha256": package.ref.manifest_sha256, "release_id": manifest["release_id"]}))
        return StepResult.succeeded(artifacts=(Artifact("release", "summary.ip-release", summary),))
