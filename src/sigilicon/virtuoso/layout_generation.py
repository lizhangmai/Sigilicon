"""Project-owned OA writer for the stable Laygo2 layout plan subset."""

from __future__ import annotations

import math
from typing import Any

from sigilicon.virtuoso.bridge import decode_skill_output
from sigilicon.virtuoso.bridge import skill_quote

from sigilicon.layout.ir import LayoutInstance, LayoutPlan
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
) -> tuple[str, ...]:
    """Return the exact caller-declared terminal set for one OA master."""

    if instance.expected_master_terminals:
        return tuple(sorted(instance.expected_master_terminals))
    return tuple(sorted(name for name, _net in instance.terminals))


def render_layout_plan_skill(
    plan: LayoutPlan,
    *,
    overwrite: bool = False,
) -> str:
    """Render a source-owned OA layout transaction.

    Rebuilds may replace an existing disposable OA cache view.  The workspace
    lease still prevents replacement while a user has the target open.
    """

    statements: list[str] = []
    for instance in plan.instances:
        x, y = instance.origin_dbu
        hierarchical = not instance.parameters and instance.library == plan.library
        if len(instance.callback_parameters) > 1:
            raise ValueError(
                f"instance {instance.name} declares more than one CDF callback parameter"
            )
        callback_parameter = (
            instance.callback_parameters[0] if instance.callback_parameters else None
        )
        callback_value = (
            None
            if callback_parameter is None
            else next(
                (
                    value
                    for name, value_type, value in instance.parameters
                    if name == callback_parameter and value_type == "string"
                ),
                None,
            )
        )
        if callback_parameter is not None and callback_value is None:
            raise ValueError(
                f"instance {instance.name} callback parameter {callback_parameter!r} "
                "must name a string PCell parameter"
            )
        creation_parameters = tuple(
            parameter
            for parameter in instance.parameters
            if callback_parameter is None or parameter[0] != callback_parameter
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
        if callback_parameter is not None:
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
                    % skill_quote(callback_parameter),
                    "unless(routeParam error(\"declared CDF callback parameter not found\"))",
                    "routeParam~>value = %s" % skill_quote(callback_value),
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
                        skill_quote(callback_parameter),
                        skill_quote(callback_value),
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
            "dbReplaceProp(cv \"flowLayoutStage\" \"string\" %s)"
            % skill_quote(plan.stage),
            'unless(dbSave(cv) error("generated layout save failed"))',
            "result = list(%s length(cv~>instances) length(cv~>shapes) length(cv~>terminals))"
            % skill_quote(plan.stage),
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
                    render_layout_plan_skill(plan, overwrite=overwrite),
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
      unless(equal(terminals list({" ".join(skill_quote(name) for name in _expected_master_terminal_names(instance))}))
        error(sprintf(nil {skill_quote(f"generated layout master terminals mismatch for {instance.name}: %L")} terminals)))'''
        for instance in plan.instances
    )
    source = f'''prog((cv expected actual expectedPins actualPins stage result inst item terminal terminals)
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
      stage = dbGetq(cv flowLayoutStage)
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
        "adapter": "Cadence SKILL dbOpenCellViewByType/read",
        "checks": [
            "instance_names",
            "top_level_terminals",
            "instance_master_presence",
            "instance_master_geometry",
            "master_terminals",
            "flowLayoutStage",
        ],
        "stage": plan.stage,
        "instance_count": len(plan.instances),
        "pin_count": len(plan.pins),
    }
