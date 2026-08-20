"""Project-owned OA writer for the stable Laygo2 layout plan subset."""

from __future__ import annotations

import math
from typing import Any

from sigilicon.virtuoso.bridge import decode_skill_output
from sigilicon.virtuoso.bridge import skill_quote

from sigilicon.layout.ir import LayoutInstance, LayoutPlan
from sigilicon.domain.platform import PcellPolicy
from sigilicon.virtuoso.capability import require_workspace_capability
from sigilicon.virtuoso.confirmation import require_bridge_confirmation
from sigilicon.virtuoso.oa import (
    audit_cellview_delta_skill,
    own_synchronous_cellview_delta_skill,
)


def _micron(value: int, dbu_per_micron: int) -> str:
    result = value / dbu_per_micron
    return f"{result:.6f}".rstrip("0").rstrip(".") or "0"


def _skill_value(value_type: str, value: str) -> str:
    if value_type == "string":
        return skill_quote(value)
    if value_type == "int":
        return str(int(value))
    if value_type == "float":
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("PCell float values must be finite")
        return repr(parsed)
    if value_type == "boolean":
        if value in {"True", "true", "t", "1"}:
            return "t"
        if value in {"False", "false", "nil", "0"}:
            return "nil"
        raise ValueError(f"invalid PCell boolean value: {value!r}")
    raise ValueError(f"unsupported PCell value type: {value_type}")


def _parameter_list(parameters: tuple[tuple[str, str, str], ...]) -> str:
    if not parameters:
        return "nil"
    values = " ".join(
        "list(%s %s %s)"
        % (skill_quote(name), skill_quote(value_type), _skill_value(value_type, value))
        for name, value_type, value in parameters
    )
    return f"list({values})"


def _expected_master_terminal_names(
    instance: LayoutInstance,
    pcell_policy: PcellPolicy | None = None,
) -> tuple[str, ...]:
    """Return the exact terminal set expected from a MOS PCell master.

    Multi-finger MOS PCells expose the additional alternating diffusion
    islands as indexed aliases. The canonical instance mapping deliberately
    stays at the electrical terminal level; this check derives physical
    master aliases from the PDK-declared finger-count policy.
    """

    names = {name for name, _net in instance.terminals}
    policy = pcell_policy or PcellPolicy()
    if policy.finger_count_parameter is None:
        return tuple(sorted(names))
    fingers_value = next(
        (
            value
            for name, _kind, value in instance.parameters
            if name == policy.finger_count_parameter
        ),
        None,
    )
    if fingers_value is None:
        return tuple(sorted(names))
    try:
        fingers = int(fingers_value)
    except ValueError as exc:
        raise ValueError(
            f"instance {instance.name} has non-integral "
            f"{policy.finger_count_parameter}={fingers_value!r}"
        ) from exc
    if fingers < 1:
        raise ValueError(f"instance {instance.name} must have at least one finger")
    if fingers > 1:
        if not {policy.drain_terminal, policy.source_terminal}.issubset(names):
            raise ValueError(
                f"multi-finger instance {instance.name} must map "
                f"{policy.drain_terminal} and {policy.source_terminal} terminals"
            )
        diffusion_count = fingers + 1
        source_count = (diffusion_count + 1) // 2
        drain_count = diffusion_count // 2
        names.update(
            f"{policy.source_alias_prefix}{index}"
            for index in range(1, source_count)
        )
        names.update(
            f"{policy.drain_alias_prefix}{index}"
            for index in range(1, drain_count)
        )
    return tuple(sorted(names))


def render_layout_plan_skill(
    plan: LayoutPlan,
    *,
    pcell_policy: PcellPolicy | None = None,
    overwrite: bool = False,
) -> str:
    """Render a source-owned OA layout transaction.

    Rebuilds may replace an existing disposable OA cache view.  The workspace
    lease still prevents replacement while a user has the target open.
    """

    policy = pcell_policy or PcellPolicy()
    statements: list[str] = []
    for instance in plan.instances:
        x, y = instance.origin_dbu
        hierarchical = not instance.parameters and instance.library == plan.library
        bypasses_callback = any(
            name in policy.cdf_callback_bypass_parameters
            for name, _value_type, _value in instance.parameters
        )
        route_poly = (
            None
            if bypasses_callback or policy.cdf_callback_parameter is None
            else next(
                (
                    value
                    for name, value_type, value in instance.parameters
                    if name == policy.cdf_callback_parameter
                    and value_type == "string"
                ),
                None,
            )
        )
        creation_parameters = tuple(
            parameter
            for parameter in instance.parameters
            if route_poly is None
            or parameter[0] != policy.cdf_callback_parameter
        )
        statements.extend(
            (
                "master = dbOpenCellViewByType(%s %s %s \"\" \"r\")"
                % (
                    skill_quote(instance.library),
                    skill_quote(instance.cell),
                    skill_quote(instance.view),
                ),
                "unless(master error(%s))"
                % skill_quote(
                    f"cannot open layout master {instance.library}/{instance.cell}/{instance.view}"
                ),
                (
                    "inst = dbCreateInst(cv master %s list(%s %s) %s)"
                    if hierarchical
                    else "inst = dbCreateParamInst(cv master %s list(%s %s) %s 1 %s)"
                )
                % (
                    (
                        skill_quote(instance.name),
                        _micron(x, plan.dbu_per_micron),
                        _micron(y, plan.dbu_per_micron),
                        skill_quote(instance.transform),
                    )
                    if hierarchical
                    else (
                        skill_quote(instance.name),
                        _micron(x, plan.dbu_per_micron),
                        _micron(y, plan.dbu_per_micron),
                        skill_quote(instance.transform),
                        _parameter_list(creation_parameters),
                    )
                ),
                "unless(inst error(%s))"
                % skill_quote(f"cannot create layout instance {instance.name}"),
            )
        )
        if route_poly is not None:
            statements.extend(
                (
                    "savedCdf = makeTable(gensym('flowSavedCdfValues))",
                    "callbackAttempt = errset(unwindProtect(progn(",
                    "iCDF = cdfGetInstCDF(inst)",
                    "unless(iCDF error(\"instance has no CDF\"))",
                    "cCDF = cdfGetCellCDF(ddGetObj(inst~>libName inst~>cellName))",
                    "unless(cCDF error(\"cell CDF not found\"))",
                    "foreach(param cCDF~>parameters "
                    "setarray(savedCdf param~>name param~>value) "
                    "when(get(iCDF param~>name) "
                    "putpropq(param get(iCDF param~>name)~>value value)))",
                    "routeParam = get(cCDF %s)"
                    % skill_quote(policy.cdf_callback_parameter),
                    "unless(routeParam error(\"declared CDF callback parameter not found\"))",
                    "routeParam~>value = %s" % skill_quote(route_poly),
                    "cdfgData = cCDF",
                    "callback = routeParam~>callback",
                    "unless(callback && callback != \"\" "
                    "error(\"declared PCell CDF callback not found\"))",
                    "unless(errset(evalstring(callback) t) "
                    "error(\"declared PCell CDF callback execution failed\"))",
                    "cdfUpdateInstParam(inst)",
                    "t) foreach(param cCDF~>parameters "
                    "putpropq(param arrayref(savedCdf param~>name) value))) nil)",
                    "unless(callbackAttempt && car(callbackAttempt) error(%s))"
                    % skill_quote(
                        f"PCell CDF callback failed for {instance.name}"
                    ),
                    "iCDF = cdfGetInstCDF(inst)",
                    "unless(equal(get(iCDF %s)~>value %s) error(%s))"
                    % (
                        skill_quote(policy.cdf_callback_parameter),
                        skill_quote(route_poly),
                        skill_quote(
                            f"PCell CDF callback failed for {instance.name}"
                        ),
                    ),
                )
            )
        statements.extend(
            (
                "unless(dbClose(master) error(%s))"
                % skill_quote(f"cannot close layout master for {instance.name}"),
                "master = nil",
            )
        )
    for rectangle in plan.rectangles:
        (x0, y0), (x1, y1) = rectangle.bbox_dbu
        statements.extend(
            (
                "fig = dbCreateRect(cv list(%s %s) list(list(%s %s) list(%s %s)))"
                % (
                    skill_quote(rectangle.layer),
                    skill_quote(rectangle.purpose),
                    _micron(x0, plan.dbu_per_micron),
                    _micron(y0, plan.dbu_per_micron),
                    _micron(x1, plan.dbu_per_micron),
                    _micron(y1, plan.dbu_per_micron),
                ),
                "unless(fig error(%s))"
                % skill_quote(f"cannot create route rectangle {rectangle.name}"),
            )
        )
    for via in plan.vias:
        x, y = via.origin_dbu
        statements.extend(
            (
                "viaDef = techFindViaDefByName(techGetTechFile(cv) %s)"
                % skill_quote(via.via_definition),
                "unless(viaDef error(%s))"
                % skill_quote(f"cannot find via definition {via.via_definition}"),
                "fig = dbCreateVia(cv viaDef list(%s %s) %s)"
                % (
                    _micron(x, plan.dbu_per_micron),
                    _micron(y, plan.dbu_per_micron),
                    skill_quote(via.transform),
                ),
                "unless(fig error(%s))"
                % skill_quote(f"cannot create via {via.name}"),
            )
        )
    for pin in plan.pins:
        (x0, y0), (x1, y1) = pin.bbox_dbu
        statements.extend(
            (
                "net = dbCreateNet(cv %s)" % skill_quote(pin.name),
                "unless(net error(%s))"
                % skill_quote(f"cannot create pin net {pin.name}"),
                "term = dbCreateTerm(net %s %s)"
                % (skill_quote(pin.name), skill_quote(pin.direction)),
                "unless(term error(%s))"
                % skill_quote(f"cannot create terminal {pin.name}"),
                "fig = dbCreateRect(cv list(%s %s) list(list(%s %s) list(%s %s)))"
                % (
                    skill_quote(pin.layer),
                    skill_quote(pin.purpose),
                    _micron(x0, plan.dbu_per_micron),
                    _micron(y0, plan.dbu_per_micron),
                    _micron(x1, plan.dbu_per_micron),
                    _micron(y1, plan.dbu_per_micron),
                ),
                "unless(fig error(%s))" % skill_quote(f"cannot create pin shape {pin.name}"),
                "pinObj = dbCreatePin(net fig)",
                "unless(pinObj error(%s))" % skill_quote(f"cannot create pin {pin.name}"),
            )
        )
    statements.extend(
        (
            "dbReplaceProp(cv \"flowLayoutFingerprint\" \"string\" %s)"
            % skill_quote(plan.fingerprint),
            "dbReplaceProp(cv \"flowLayoutSourceFingerprint\" \"string\" %s)"
            % skill_quote(plan.source_fingerprint),
            "dbReplaceProp(cv \"flowLayoutStage\" \"string\" %s)"
            % skill_quote(plan.stage),
            'unless(dbSave(cv) error("generated layout save failed"))',
            "result = list(%s %s length(cv~>instances) length(cv~>shapes) length(cv~>terminals))"
            % (skill_quote(plan.fingerprint), skill_quote(plan.stage)),
        )
    )
    body = "\n      ".join(statements)
    existing_view_guard = (
        f'''when(ddGetObj({skill_quote(plan.library)} {skill_quote(plan.cell)} {skill_quote(plan.view)})
    unless(ddDeleteObj(ddGetObj({skill_quote(plan.library)} {skill_quote(plan.cell)} {skill_quote(plan.view)}))
      error({skill_quote(f"cannot replace existing view {plan.library}/{plan.cell}/{plan.view}")})))'''
        if overwrite
        else f'''when(ddGetObj({skill_quote(plan.library)} {skill_quote(plan.cell)} {skill_quote(plan.view)})
    error({skill_quote(f"refusing to replace existing view {plan.library}/{plan.cell}/{plan.view}")}))'''
    )
    return f'''prog((cv master inst iCDF cCDF cdfgData param routeParam callback
  savedCdf callbackAttempt fig viaDef net term pinObj result)
  cv = nil
  master = nil
  {existing_view_guard}
  unwindProtect(
    progn(
      cv = dbOpenCellViewByType({skill_quote(plan.library)} {skill_quote(plan.cell)}
        {skill_quote(plan.view)} "maskLayout" "a")
      unless(cv error("cannot create generated layout target"))
      {body}
    )
    progn(
      when(master
        unless(dbClose(master) error("generated layout master close failed"))
        master = nil)
      when(cv
        unless(dbClose(cv) error("generated layout close failed"))
        cv = nil)))
  return(result)
)'''


def write_layout_plan(
    client: Any,
    plan: LayoutPlan,
    *,
    pcell_policy: PcellPolicy,
    operation: Any,
    overwrite: bool = False,
    timeout: int = 120,
) -> None:
    require_workspace_capability(
        operation,
        client,
        library=plan.library,
        cell=plan.cell,
        view=plan.view,
    )
    operation.require_active_mutation(
        client,
        plan.library,
        plan.cell,
        phase="generated layout atomic SKILL dispatch",
    )
    label = f"generate layout {plan.library}/{plan.cell}/{plan.view}"
    result = require_bridge_confirmation(
        operation,
        label,
        lambda: client.execute_skill(
            audit_cellview_delta_skill(
                own_synchronous_cellview_delta_skill(
                    render_layout_plan_skill(
                        plan, pcell_policy=pcell_policy, overwrite=overwrite
                    ),
                    label=label,
                ),
                label=label,
                mutation_target=(plan.library, (plan.cell,)),
            ),
            timeout=timeout,
        ),
    )
    if result.errors:
        raise RuntimeError(result.errors[0])
    output = decode_skill_output(result.output or "")
    if output.lstrip().startswith(("ERROR", "Error", "*Error*")):
        raise RuntimeError(output)


def validate_layout_plan(
    client: Any,
    plan: LayoutPlan,
    *,
    pcell_policy: PcellPolicy,
    operation: Any,
    timeout: int = 60,
) -> dict[str, object]:
    require_workspace_capability(
        operation,
        client,
        library=plan.library,
        cell=plan.cell,
        view=plan.view,
    )
    names = "list(" + " ".join(skill_quote(item.name) for item in plan.instances) + ")"
    pin_names = "list(" + " ".join(skill_quote(item.name) for item in plan.pins) + ")"
    terminal_validations = "\n".join(
        f'''      inst = car(setof(item cv~>instances item~>name == {skill_quote(instance.name)}))
      unless(inst error({skill_quote(f"generated layout instance missing: {instance.name}")}))
      terminals = sort(
        foreach(mapcar terminal inst~>master~>terminals terminal~>name)
        'alphalessp)
      unless(equal(terminals list({" ".join(skill_quote(name) for name in _expected_master_terminal_names(instance, pcell_policy))}))
        error(sprintf(nil {skill_quote(f"generated layout master terminals mismatch for {instance.name}: %L")} terminals)))'''
        for instance in plan.instances
    )
    source = f'''prog((cv expected actual expectedPins actualPins fingerprint stage result inst item terminal terminals)
  cv = nil
  unwindProtect(
    progn(
      cv = dbOpenCellViewByType({skill_quote(plan.library)} {skill_quote(plan.cell)}
        {skill_quote(plan.view)} "maskLayout" "r")
      unless(cv error("generated layout validation open failed"))
      expected = sort(copy({names}) 'alphalessp)
      actual = sort(foreach(mapcar inst cv~>instances inst~>name) 'alphalessp)
      unless(equal(expected actual)
        error(sprintf(nil "generated layout instances mismatch: %L" actual)))
      expectedPins = sort(copy({pin_names}) 'alphalessp)
      actualPins = sort(foreach(mapcar terminal cv~>terminals terminal~>name) 'alphalessp)
      unless(equal(expectedPins actualPins)
        error(sprintf(nil "generated layout top-level terminals mismatch: %L" actualPins)))
      foreach(inst cv~>instances
        unless(inst~>master
          error(sprintf(nil "generated layout instance has no master: %s" inst~>name)))
        unless(inst~>master~>shapes
          error(sprintf(nil "generated layout master has no geometry: %s" inst~>name))))
{terminal_validations}
      fingerprint = dbGetq(cv flowLayoutFingerprint)
      stage = dbGetq(cv flowLayoutStage)
      unless(equal(fingerprint {skill_quote(plan.fingerprint)})
        error("generated layout content fingerprint mismatch"))
      unless(equal(stage {skill_quote(plan.stage)})
        error("generated layout stage mismatch"))
      result = t)
    when(cv
      unless(dbClose(cv) error("generated layout validation close failed"))
      cv = nil))
  return(result)
)'''
    label = f"validate layout {plan.library}/{plan.cell}/{plan.view}"
    result = require_bridge_confirmation(
        operation,
        label,
        lambda: client.execute_skill(
            audit_cellview_delta_skill(
                own_synchronous_cellview_delta_skill(source, label=label),
                label=label,
            ),
            timeout=timeout,
        ),
    )
    if result.errors:
        raise RuntimeError(result.errors[0])
    output = decode_skill_output(result.output or "")
    if output != "t":
        raise RuntimeError(f"generated layout validation was not confirmed: {output}")
    return {
        "passed": True,
        "backend": "Cadence SKILL dbOpenCellViewByType/read",
        "checks": [
            "instance_names",
            "top_level_terminals",
            "instance_master_presence",
            "instance_master_geometry",
            "master_terminals",
            "flowLayoutFingerprint",
            "flowLayoutStage",
        ],
        "layout_fingerprint": plan.fingerprint,
        "stage": plan.stage,
        "instance_count": len(plan.instances),
        "pin_count": len(plan.pins),
    }


def delete_generated_layout_view(
    client: Any,
    *,
    library: str,
    cell: str,
    view: str,
    expected_fingerprint: str,
    expected_stage: str,
    operation: Any,
    timeout: int = 60,
) -> None:
    """Delete one failed generated view only after exact provenance matching."""

    require_workspace_capability(
        operation,
        client,
        library=library,
        cell=cell,
        view=view,
    )
    operation.require_active_mutation(
        client,
        library,
        cell,
        phase="failed generated layout rollback dispatch",
    )
    source = f'''prog((cv viewObj fingerprint stage result)
  cv = nil
  unwindProtect(
    progn(
      cv = dbOpenCellViewByType({skill_quote(library)} {skill_quote(cell)}
        {skill_quote(view)} "maskLayout" "r")
      unless(cv error("failed generated layout rollback target is absent"))
      fingerprint = dbGetq(cv flowLayoutFingerprint)
      stage = dbGetq(cv flowLayoutStage)
      unless(equal(fingerprint {skill_quote(expected_fingerprint)})
        error("refusing generated layout rollback: content fingerprint mismatch"))
      unless(equal(stage {skill_quote(expected_stage)})
        error("refusing generated layout rollback: stage mismatch"))
      unless(dbClose(cv) error("failed generated layout rollback close failed"))
      cv = nil
      viewObj = ddGetObj({skill_quote(library)} {skill_quote(cell)} {skill_quote(view)})
      unless(viewObj error("failed generated layout rollback object disappeared"))
      unless(ddDeleteObj(viewObj) error("failed generated layout rollback delete failed"))
      result = t)
    when(cv
      unless(dbClose(cv) error("failed generated layout rollback cleanup failed"))
      cv = nil))
  return(result)
)'''
    label = f"rollback generated layout {library}/{cell}/{view}"
    result = require_bridge_confirmation(
        operation,
        label,
        lambda: client.execute_skill(
            audit_cellview_delta_skill(
                own_synchronous_cellview_delta_skill(source, label=label),
                label=label,
                mutation_target=(library, (cell,)),
            ),
            timeout=timeout,
        ),
    )
    if result.errors:
        raise RuntimeError(result.errors[0])
    output = decode_skill_output(result.output or "")
    if output != "t":
        raise RuntimeError(f"generated layout rollback was not confirmed: {output}")


def validate_layout_view_absent(
    client: Any,
    *,
    library: str,
    cell: str,
    view: str,
    operation: Any,
    timeout: int = 60,
) -> None:
    """Confirm a rolled-back generated view no longer exists in OA."""

    require_workspace_capability(
        operation,
        client,
        library=library,
        cell=cell,
        view=view,
    )
    source = (
        "unless(ddGetObj(%s %s %s) t)"
        % (skill_quote(library), skill_quote(cell), skill_quote(view))
    )
    label = f"validate absent layout {library}/{cell}/{view}"
    result = require_bridge_confirmation(
        operation,
        label,
        lambda: client.execute_skill(
            audit_cellview_delta_skill(
                own_synchronous_cellview_delta_skill(source, label=label),
                label=label,
            ),
            timeout=timeout,
        ),
    )
    if result.errors:
        raise RuntimeError(result.errors[0])
    output = decode_skill_output(result.output or "")
    if output != "t":
        raise RuntimeError(f"generated layout absence was not confirmed: {output}")
