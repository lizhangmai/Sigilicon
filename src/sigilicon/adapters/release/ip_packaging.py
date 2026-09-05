"""Publish and audit immutable custom-IP packages."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import uuid
from typing import Any, Mapping

from sigilicon.artifacts import (
    SafeTree,
    _inspect_nofollow_file,
    atomic_write_json,
    copy_immutable_file,
    ensure_nofollow_directory,
    read_json_object,
    read_nofollow_bytes,
    write_immutable_text,
)
from sigilicon.contracts import (
    read_toml,
    require_relative_path,
)
from sigilicon.domain.ip_release import (
    RELEASE_MATURITY_LEVELS,
    IpContract,
)
from sigilicon.domain.netlist import (
    load_netlist_snapshot,
    resolve_netlist_hierarchy,
    subckt_ports,
)
from sigilicon.domain.systemverilog import (
    module_port_signatures,
    named_port_connections,
)
from sigilicon.external_tools import owned_directory
from sigilicon.release_views import ViewSelector, view_condition
from sigilicon.release_store import (
    ReleasePackage,
    ReleaseRef,
    ReleaseStore,
    _release_object_name,
    audit_release_package,
)
from sigilicon.adapters.release.source_control import inspect_checkout, verify_source_commit
from sigilicon.adapters.release.release_contract_checks import (
    _identity_module,
    _interface_ports,
    _native_oa_interface_contract,
    _oa_port_contract,
    _project_path,
    _rtl_module_contract,
    _table,
)
from sigilicon.adapters.release.release_plan_record import (
    IpReleaseError,
    IpReleasePlan,
)


def _publish_ip_release(
    plan: IpReleasePlan,
    *,
    store_root: Path,
    source_paths: Mapping[Path, Path],
    resources: Any,
) -> dict[str, Any]:
    """Publish one planned release exclusively from its sealed source closure."""

    if not isinstance(plan, IpReleasePlan):
        raise TypeError("release publication requires an IpReleasePlan")
    contract = plan.contract
    record = plan.record
    expected_sources = {
        (contract.project_root / relative).absolute()
        for relative in plan.source_files
    }
    selected_sources = {
        Path(source).absolute(): Path(sealed).absolute()
        for source, sealed in source_paths.items()
    }
    if set(selected_sources) != expected_sources:
        raise IpReleaseError("release execution source closure disagrees with its plan")
    if plan.missing_items:
        missing = ", ".join(plan.missing_items)
        raise IpReleaseError(
            f"cannot build {plan.maturity} IP release; missing: {missing}"
        )
    if plan.working_tree_dirty:
        raise IpReleaseError(
            "IP releases require a clean source checkout"
        )
    source_state = inspect_checkout(
        contract.project_root,
        resources,
    )
    if (
        source_state.commit != plan.source_commit
        or source_state.working_tree_dirty
    ):
        raise IpReleaseError("source checkout changed during release build")
    verify_source_commit(contract.project_root, resources, plan.source_commit, selected_sources)
    store = ReleaseStore(store_root)
    namespace = store.root / plan.store / "objects"
    with owned_directory(namespace, create_missing=True) as release_namespace:
        temporary_name = f".{plan.release_id}.{uuid.uuid4().hex}.tmp"
        os.mkdir(temporary_name, dir_fd=release_namespace.fd)
        temporary = namespace / temporary_name
        installed = False
        try:
            views: list[dict[str, Any]] = []
            planned_collateral = {
                (item["export"], item["name"]): item
                for item in record["collateral"]
            }
            for item in contract.collateral:
                source = _project_path(
                    contract.project_root,
                    Path(item.source),
                    "collateral source",
                )
                destination = temporary / item.package_path
                ensure_nofollow_directory(destination.parent)
                expected = planned_collateral[(item.export, item.name)]
                sealed_source = selected_sources[source]
                source_metadata, source_digest = _inspect_nofollow_file(sealed_source)
                if (
                    source_metadata.st_size != expected["source_size"]
                    or source_digest != expected["source_sha256"]
                ):
                    raise IpReleaseError(
                        "release source changed after planning: "
                        f"{item.export}/{item.role}"
                    )
                if expected.get("composition") == "reachable-spectre-hierarchy":
                    key = (item.export, item.name)
                    try:
                        text = plan.native_bundles[key]
                    except KeyError as exc:
                        raise RuntimeError(
                            "native OA release bundle is absent from its plan"
                        ) from exc
                    if hashlib.sha256(text.encode("utf-8")).hexdigest() != expected.get(
                        "sha256"
                    ):
                        raise RuntimeError(
                            f"native OA release bundle identity drift: {item.export}"
                        )
                    write_immutable_text(destination, text)
                else:
                    copy_immutable_file(
                        sealed_source,
                        destination,
                        expected_size=expected["source_size"],
                        expected_sha256=expected["source_sha256"],
                    )
                packaged_metadata, packaged_digest = _inspect_nofollow_file(destination)
                view = {
                    "export": item.export,
                    "name": item.name,
                    "role": item.role,
                    "path": item.package_path.as_posix(),
                    "source": item.source.as_posix(),
                    "size": packaged_metadata.st_size,
                    "sha256": packaged_digest,
                    "format": item.format,
                    "module": item.module,
                    "library": item.library,
                    "cell": item.cell,
                    "view": item.view,
                    "variant": item.variant,
                    "condition": dict(item.condition),
                    "capabilities": list(item.capabilities),
                }
                for field in (
                    "composition",
                    "subcircuits",
                    "primitive_masters",
                    "sha256",
                ):
                    if field in expected:
                        view[field] = expected[field]
                views.append(view)
            manifest: dict[str, Any] = {
                "schema": 3,
                "contract_kind": "ip-release-manifest",
                "release_kind": "source-package",
                "ip_name": record["ip_name"],
                "owner": record["owner"],
                "release_id": plan.release_id,
                "source_commit": plan.source_commit,
                "source_files": list(plan.source_files),
                "component": record["component"],
                "exports": record["exports"],
                "views": views,
                "maturity": {
                    "level": plan.maturity,
                    "checks": record["maturity_checks"],
                    "missing_items": list(plan.missing_items),
                },
                "provenance": {
                    "contract": record["contract"],
                    "producer": record["producer"],
                    "generator": "flow-ip-packaging",
                },
                "availability": record["availability"],
            }
            atomic_write_json(temporary / "manifest.json", manifest)
            manifest_digest = hashlib.sha256(
                read_nofollow_bytes(temporary / "manifest.json")
            ).hexdigest()
            ref = ReleaseRef(
                plan.store,
                manifest_digest,
            )
            object_name = _release_object_name(ref)
            try:
                existing = os.stat(
                    object_name,
                    dir_fd=release_namespace.fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                SafeTree(temporary).make_readonly()
                os.rename(
                    temporary_name,
                    object_name,
                    src_dir_fd=release_namespace.fd,
                    dst_dir_fd=release_namespace.fd,
                )
                installed = True
            else:
                if not stat.S_ISDIR(existing.st_mode):
                    raise RuntimeError(
                        f"release store object path is unsafe: {object_name}"
                    )
        finally:
            if not installed:
                try:
                    temporary_tree = SafeTree(temporary)
                    expected_temporary = os.stat(
                        temporary_name,
                        dir_fd=release_namespace.fd,
                        follow_symlinks=False,
                    )
                    temporary_tree.remove(expected_temporary)
                except FileNotFoundError:
                    pass
    audited = store.open(ref, validate=validate_ip_release_package)
    return _audit_loaded_ip_release(contract, record, audited)


def _manifest_exports(manifest: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    values = manifest.get("exports")
    if not isinstance(values, list) or not values:
        raise RuntimeError("IP release manifest has no exports")
    exports: dict[str, Mapping[str, Any]] = {}
    for index, item in enumerate(values):
        if not isinstance(item, Mapping):
            raise RuntimeError(f"IP release export {index} is invalid")
        name = item.get("name")
        if not isinstance(name, str) or not name or name in exports:
            raise RuntimeError("IP release export identities must be non-empty and unique")
        exports[name] = item
    return exports


def release_view(
    manifest: Mapping[str, Any], selection: str | ViewSelector, *, export: str
) -> Mapping[str, Any]:
    """Select one named or semantically unambiguous view from an exact export."""

    _manifest_exports(manifest)[export]
    views = manifest.get("views")
    if not isinstance(views, list):
        raise RuntimeError("IP release manifest has no views")
    matches = [
        item
        for item in views
        if isinstance(item, Mapping)
        and item.get("export") == export
        and ((isinstance(selection, str) and item.get("name") == selection)
             or (isinstance(selection, ViewSelector) and selection.matches(role=item.get("role"),
                 variant=item.get("variant"), condition=view_condition(item.get("condition", {})))))
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"IP release must contain exactly one {export}/{selection} view"
        )
    return matches[0]


def _packaged_rtl_interface_check(
    manifest: Mapping[str, Any],
    manifest_path: Path,
    *,
    export_name: str,
    interface: Mapping[str, Any],
) -> None:
    required_fields = {"kind", "contract", "module", "source_view"}
    fields = set(interface)
    if fields != required_fields and fields != required_fields | {"variant"}:
        raise RuntimeError(
            f"packaged {export_name} RTL interface fields are invalid"
        )
    module_name = interface.get("module")
    source_view = interface.get("source_view")
    contract_source = interface.get("contract")
    variant = interface.get("variant")
    if (
        not isinstance(module_name, str)
        or not module_name
        or not isinstance(source_view, str)
        or not source_view
        or not isinstance(contract_source, str)
        or not contract_source
        or (variant is not None and (not isinstance(variant, str) or not variant))
    ):
        raise RuntimeError(
            f"packaged {export_name} RTL interface identity is invalid"
        )
    try:
        require_relative_path(
            contract_source,
            f"exports.{export_name}.interface.contract",
        )
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    contract_path = resolve_release_view(
        manifest, manifest_path, ViewSelector("interface_contract"), export=export_name
    )
    contract_view = release_view(
        manifest, ViewSelector("interface_contract"), export=export_name
    )
    if contract_view.get("source") != contract_source:
        raise RuntimeError(
            f"packaged {export_name} interface contract provenance drifted"
        )
    rtl_source = resolve_release_view(
        manifest, manifest_path, source_view, export=export_name
    )
    if release_view(
        manifest, source_view, export=export_name
    ).get("module") != module_name:
        raise RuntimeError(
            f"packaged {export_name}/{source_view} module disagrees with its interface"
    )
    try:
        raw = read_toml(contract_path)
        module, public_module = _rtl_module_contract(
            raw,
            module_name=module_name,
            variant=variant,
        )
        source_view = release_view(
            manifest, source_view, export=export_name
        )
        if source_view.get("source") != module.get("source"):
            raise ValueError("RTL interface source provenance drifted")
        expected_ports = _interface_ports(
            public_module.get("ports"), "module.ports"
        )
        actual_ports = module_port_signatures(
            rtl_source.read_text(encoding="utf-8"), module_name
        )
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"packaged {export_name} RTL interface is invalid: {exc}"
        ) from exc
    if actual_ports != expected_ports:
        raise RuntimeError(
            f"packaged {export_name} RTL signature disagrees with its interface"
        )


def _packaged_native_oa_interface_check(
    manifest: Mapping[str, Any],
    manifest_path: Path,
    *,
    export_name: str,
    exported: Mapping[str, Any],
    interface: Mapping[str, Any],
) -> None:
    if set(interface) != {"kind", "contract"}:
        raise RuntimeError(
            f"packaged {export_name} native OA interface fields are invalid"
        )
    contract_source = interface.get("contract")
    if not isinstance(contract_source, str) or not contract_source:
        raise RuntimeError(
            f"packaged {export_name} native OA interface identity is invalid"
        )
    try:
        require_relative_path(
            contract_source,
            f"exports.{export_name}.interface.contract",
        )
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    oa = exported.get("oa")
    if not isinstance(oa, Mapping) or any(
        not isinstance(oa.get(field), str) or not oa.get(field)
        for field in ("library", "cell", "schematic_view", "layout_view")
    ):
        raise RuntimeError(
            f"packaged {export_name} native OA identity is invalid"
        )

    contract_path = resolve_release_view(
        manifest, manifest_path, ViewSelector("interface_contract"), export=export_name
    )
    contract_view = release_view(
        manifest, ViewSelector("interface_contract"), export=export_name
    )
    if contract_view.get("source") != contract_source:
        raise RuntimeError(
            f"packaged {export_name} interface contract provenance drifted"
    )
    try:
        raw = read_toml(contract_path)
        owner = manifest.get("owner")
        if not isinstance(owner, str) or not owner:
            raise ValueError("release owner identity is missing")
        port_count, port_contract_relative = _native_oa_interface_contract(
            raw,
            path=contract_path,
            owner=owner,
            library=str(oa["library"]),
            cell=str(oa["cell"]),
        )
        port_contract_source = port_contract_relative.as_posix()
        if release_view(
            manifest, ViewSelector("oa_port_contract"), export=export_name
        ).get("source") != port_contract_source:
            raise ValueError("OA port contract provenance drifted")
        port_contract_path = resolve_release_view(
            manifest, manifest_path, ViewSelector("oa_port_contract"), export=export_name
        )
        expected_ports = _oa_port_contract(read_toml(port_contract_path))
        if len(expected_ports) != port_count:
            raise ValueError("OA port count disagrees with the interface")
        circuit_path = resolve_release_view(
            manifest, manifest_path, ViewSelector("circuit_netlist"), export=export_name
        )
        circuit_view = release_view(
            manifest, ViewSelector("circuit_netlist"), export=export_name
        )
        if circuit_view.get("composition") != "reachable-spectre-hierarchy":
            raise ValueError(
                "native OA circuit is not a closed reachable Spectre hierarchy"
            )
        subcircuits = circuit_view.get("subcircuits")
        primitive_masters = circuit_view.get("primitive_masters")
        if (
            not isinstance(subcircuits, list)
            or not subcircuits
            or any(not isinstance(value, str) or not value for value in subcircuits)
            or len(set(subcircuits)) != len(subcircuits)
        ):
            raise ValueError("native OA circuit subcircuit inventory is invalid")
        if (
            not isinstance(primitive_masters, list)
            or any(
                not isinstance(value, str) or not value
                for value in primitive_masters
            )
            or len(set(primitive_masters)) != len(primitive_masters)
        ):
            raise ValueError("native OA circuit primitive inventory is invalid")
        expected_digest = circuit_view.get("sha256")
        if (
            not isinstance(expected_digest, str)
            or len(expected_digest) != 64
            or any(value not in "0123456789abcdef" for value in expected_digest)
        ):
            raise ValueError("native OA circuit digest is invalid")
        circuit_snapshot = load_netlist_snapshot(circuit_path)
        actual_digest = hashlib.sha256(
            circuit_snapshot.text.encode("utf-8")
        ).hexdigest()
        if actual_digest != expected_digest:
            raise ValueError("native OA circuit digest drifted")
        hierarchy = resolve_netlist_hierarchy(
            (circuit_snapshot,),
            top=str(oa["cell"]),
            primitive_masters=primitive_masters,
        )
        if list(hierarchy.dependency_order) != subcircuits:
            raise ValueError("native OA circuit subcircuit inventory drifted")
        if hierarchy.unreachable_subckts:
            raise ValueError("native OA circuit contains unreachable subcircuits")
        if set(hierarchy.primitive_counts) != set(primitive_masters):
            raise ValueError("native OA circuit primitive inventory drifted")
        circuit_ports = hierarchy.definitions[str(oa["cell"])].ports
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"packaged {export_name} native OA interface is invalid: {exc}"
        ) from exc
    if circuit_ports != tuple(expected_ports):
        raise RuntimeError(
            f"packaged {export_name} circuit pin order disagrees with its OA ports"
        )


def _packaged_interface_check(
    manifest: Mapping[str, Any], manifest_path: Path
) -> None:
    for export_name, exported in _manifest_exports(manifest).items():
        interface = exported.get("interface")
        if not isinstance(interface, Mapping):
            raise RuntimeError(f"IP release export {export_name} has no interface")
        interface_kind = interface.get("kind")
        if interface_kind == "rtl":
            if "oa" in exported:
                raise RuntimeError(
                    f"IP release export {export_name} RTL interface cannot declare OA"
                )
            _packaged_rtl_interface_check(
                manifest,
                manifest_path,
                export_name=export_name,
                interface=interface,
            )
            continue
        if interface_kind == "oa-native":
            _packaged_native_oa_interface_check(
                manifest,
                manifest_path,
                export_name=export_name,
                exported=exported,
                interface=interface,
            )
            continue
        if interface_kind != "oa-mixed-signal":
            raise RuntimeError(
                f"IP release export {export_name} interface kind is unsupported"
            )
        physical_identity = interface.get("physical")
        logical_identity = interface.get("logical")
        if not isinstance(physical_identity, str) or not isinstance(
            logical_identity, str
        ):
            raise RuntimeError(
                f"IP release export {export_name} interface identities are invalid"
            )
        try:
            physical_module = _identity_module(
                physical_identity, f"exports.{export_name}.interface.physical"
            )
            logical_module = _identity_module(
                logical_identity, f"exports.{export_name}.interface.logical"
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc

        contract_path = resolve_release_view(
            manifest, manifest_path, ViewSelector("interface_contract"), export=export_name
        )
        try:
            raw = read_toml(contract_path)
            physical = _table(raw.get("physical_macro"), "physical_macro")
            transaction = _table(
                raw.get("transaction_boundary"), "transaction_boundary"
            )
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"packaged {export_name} interface contract is invalid: {exc}"
            ) from exc
        shell_module = transaction.get("ams_wrapper_module")
        expected_modules = {
            "physical_blackbox": physical_module,
            "transaction_model": logical_module,
            "integration_adapter": shell_module,
        }
        if (
            physical.get("module") != physical_module
            or transaction.get("module") != logical_module
        ):
            raise RuntimeError(
                f"packaged {export_name} interface identities disagree with its manifest"
            )
        for role, module in expected_modules.items():
            if not isinstance(module, str) or release_view(
                manifest, ViewSelector(role), export=export_name
            ).get("module") != module:
                raise RuntimeError(
                    f"packaged {export_name}/{role} module disagrees with its interface"
                )

        try:
            expected_transaction_ports = _interface_ports(
                transaction.get("ports"), "transaction_boundary.ports"
            )
        except ValueError as exc:
            raise RuntimeError(
                f"packaged {export_name} transaction ports are invalid: {exc}"
            ) from exc
        sources = {
            role: resolve_release_view(
                manifest, manifest_path, ViewSelector(role), export=export_name
            )
            for role in (
                "transaction_model",
                "integration_adapter",
                "physical_blackbox",
                "oa_port_contract",
                "circuit_netlist",
            )
        }
        try:
            actual_transaction_ports = module_port_signatures(
                sources["transaction_model"].read_text(encoding="utf-8"),
                logical_module,
            )
            shell_text = sources["integration_adapter"].read_text(encoding="utf-8")
            actual_shell_ports = module_port_signatures(
                shell_text, str(shell_module)
            )
            actual_physical_ports = module_port_signatures(
                sources["physical_blackbox"].read_text(encoding="utf-8"),
                physical_module,
            )
            expected_physical_ports = _oa_port_contract(
                read_toml(sources["oa_port_contract"])
            )
            circuit_ports = subckt_ports(
                sources["circuit_netlist"], physical_module
            )
            bindings = named_port_connections(
                shell_text, str(shell_module), physical_module
            )
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"packaged {export_name} SystemVerilog interface is invalid: {exc}"
            ) from exc
        if actual_transaction_ports != expected_transaction_ports:
            raise RuntimeError(
                f"packaged {export_name} transaction model signature disagrees"
            )
        if (
            dict(list(actual_shell_ports.items())[: len(expected_transaction_ports)])
            != expected_transaction_ports
        ):
            raise RuntimeError(
                f"packaged {export_name} physical shell signature disagrees"
            )
        if actual_physical_ports != expected_physical_ports:
            raise RuntimeError(
                f"packaged {export_name} physical blackbox disagrees with its OA ports"
            )
        if circuit_ports != tuple(expected_physical_ports):
            raise RuntimeError(
                f"packaged {export_name} circuit pin order disagrees with its OA ports"
            )
        if tuple(bindings) != tuple(expected_physical_ports):
            raise RuntimeError(
                f"packaged {export_name} physical named bindings disagree"
            )


def _packaged_maturity_check(manifest: Mapping[str, Any], manifest_path: Path) -> None:
    from sigilicon.adapters.release.release_semantics import ExportSemantics

    maturity = manifest.get("maturity")
    if not isinstance(maturity, Mapping) or maturity.get("level") not in RELEASE_MATURITY_LEVELS:
        raise RuntimeError("IP release maturity level is invalid")
    checks = maturity.get("checks")
    if (maturity.get("missing_items") != [] or not isinstance(checks, list) or not checks
            or any(not isinstance(check, Mapping) or check.get("passed") is not True for check in checks)):
        raise RuntimeError("IP release maturity checks are incomplete or failed")
    views = manifest.get("views")
    if not isinstance(views, list):
        raise RuntimeError("IP release views must be a list")
    problems = []
    availability = []
    for name, exported in _manifest_exports(manifest).items():
        export_maturity = exported.get("maturity")
        if not isinstance(export_maturity, Mapping) or export_maturity.get("missing_items") != []:
            problems.append(f"{name}:maturity-missing-items")
        def read_receipt(role: str):
            path = resolve_release_view(manifest, manifest_path, role, export=name)
            return read_json_object(path, f"{name}/{role} receipt")
        try:
            semantics = ExportSemantics.from_record(exported, views)
            available, issues = semantics.assess(maturity["level"], read_receipt)
        except (KeyError, ValueError) as exc:
            raise RuntimeError(f"IP release qualified view semantics are invalid: {exc}") from exc
        problems.extend(issues)
        availability.append(available.record)
        claimed = exported.get("availability")
        if (claimed != available.record or not isinstance(claimed, Mapping)
                or any(type(value) is not bool for value in claimed.values())):
            problems.append(f"{name}:availability")
    expected = {key: all(row[key] for row in availability)
                for key in ("simulation", "synthesis", "physical_implementation")}
    claimed = manifest.get("availability")
    if (claimed != expected or not isinstance(claimed, Mapping)
            or any(type(value) is not bool for value in claimed.values())):
        problems.append("release:availability")
    if problems:
        raise RuntimeError("IP release qualified view semantics are invalid: " + ", ".join(sorted(problems)))


def validate_ip_release_package(package: ReleasePackage) -> None:
    _packaged_interface_check(package.manifest, package.manifest_path)
    _packaged_maturity_check(package.manifest, package.manifest_path)


def audit_ip_release_manifest(manifest_path: Path) -> dict[str, Any]:
    """Audit an exact immutable package without consulting producer source."""

    package = audit_release_package(
        manifest_path,
        validate=validate_ip_release_package,
    )
    return dict(package.manifest)


def _audit_loaded_ip_release(
    contract: IpContract,
    plan: Mapping[str, Any],
    audited: ReleasePackage,
) -> dict[str, Any]:
    release_root = audited.manifest_path.parent
    manifest = audited.manifest
    expected = {
        "ip_name": plan["ip_name"],
        "owner": plan["owner"],
        "release_id": plan["release_id"],
        "source_commit": plan["source_commit"],
        "source_files": plan["source_files"],
        "component": plan["component"],
        "exports": plan["exports"],
        "availability": plan["availability"],
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise RuntimeError(f"IP release manifest {key} does not match its source plan")
    provenance = manifest.get("provenance")
    if not isinstance(provenance, Mapping) or any(
        provenance.get(key) != plan[key] for key in ("contract", "producer")
    ):
        raise RuntimeError("IP release provenance does not match its source plan")
    source_commit = manifest.get("source_commit")
    if (
        not isinstance(source_commit, str)
        or len(source_commit) not in {40, 64}
        or any(character not in "0123456789abcdef" for character in source_commit)
    ):
        raise RuntimeError("IP release source_commit is invalid")
    maturity_data = manifest.get("maturity")
    if not isinstance(maturity_data, Mapping):
        raise RuntimeError("IP release maturity record is missing")
    if (
        maturity_data.get("level") != plan["maturity_level"]
        or maturity_data.get("missing_items") != []
        or maturity_data.get("checks") != plan["maturity_checks"]
    ):
        raise RuntimeError("IP release maturity record is inconsistent")
    views = manifest.get("views")
    if not isinstance(views, list):
        raise RuntimeError("IP release views must be a list")
    expected_roles = {
        (item["export"], item["name"]) for item in plan["collateral"]
    }
    actual_roles: set[tuple[str, str]] = set()
    expected_views = {
        (item["export"], item["name"]): item for item in plan["collateral"]
    }
    audited_views = {
        (artifact.export, artifact.name): artifact
        for artifact in audited.artifacts
    }
    for view in views:
        if not isinstance(view, Mapping):
            raise RuntimeError("IP release view entry is invalid")
        role = str(view.get("role"))
        export = str(view.get("export"))
        role_key = (export, view.get("name"))
        expected_view = expected_views.get(role_key)
        audited_view = audited_views.get(role_key)
        if (
            expected_view is None
            or audited_view is None
            or view.get("path") != audited_view.relative_path
            or view.get("path") != expected_view["package_path"]
            or any(
                view.get(field) != expected_view.get(field)
                for field in (
                    "source",
                    "format",
                    "module",
                    "condition",
                    "variant",
                    "role",
                    "capabilities",
                    "composition",
                    "subcircuits",
                    "primitive_masters",
                )
            )
        ):
            raise RuntimeError(
                f"IP release view metadata drifted: {export}/{role}"
            )
        actual_roles.add(role_key)
    if actual_roles != expected_roles:
        raise RuntimeError("IP release view identities do not match the producer contract")
    expected_files = {
        Path("manifest.json"),
        *(Path(str(view["path"])) for view in views),
    }
    expected_directories = {
        parent
        for file in expected_files
        for parent in file.parents
        if parent != Path(".")
    }
    inventory = SafeTree(release_root).inventory()
    writable = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    for relative, file in inventory.files.items():
        if file.mode & writable:
            raise RuntimeError(f"IP release file is writable: {relative}")
    for relative, mode in inventory.directories.items():
        if mode & writable:
            raise RuntimeError(
                f"IP release directory is writable: {relative}"
            )
    if (
        set(inventory.files) != expected_files
        or set(inventory.directories) != expected_directories
    ):
        raise RuntimeError("IP release inventory does not match its manifest")
    if audited.ref is None:
        raise RuntimeError("stored IP release lost its release reference")
    return {
        **manifest,
        "store": audited.ref.store,
        "manifest_sha256": audited.ref.manifest_sha256,
        "audit": {"passed": True},
    }


def resolve_release_view(
    manifest: Mapping[str, Any],
    manifest_path: Path,
    selection: str | ViewSelector,
    *,
    export: str,
) -> Path:
    view = release_view(manifest, selection, export=export)
    try:
        file = SafeTree(manifest_path.parent).file(
            view.get("path"), f"IP release view {export}/{selection}"
        )
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(f"IP release view {export}/{selection} is missing") from exc
    if file.size != view.get("size") or file.sha256 != view.get("sha256"):
        raise RuntimeError(f"IP release view {export}/{selection} content drifted")
    return file.path
