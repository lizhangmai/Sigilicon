"""Read-only semantic attestation of source-owned ADE/Maestro setup objects."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
import math
from pathlib import Path
import re
from typing import Any

from sigilicon.virtuoso.bridge import decode_skill_output

from sigilicon.virtuoso.capability import WorkspaceAuthority, require_workspace_capability
from sigilicon.virtuoso.confirmation import require_bridge_confirmation
from sigilicon.virtuoso.oa import (
    audit_cellview_delta_skill,
    own_synchronous_cellview_delta_skill,
    skill_quote,
)


ATTESTATION_SCHEMA = 1

_CALCULATOR_NUMBER = re.compile(
    r"(?<![A-Za-z0-9_.])"
    r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
    r"(p|n|u|m|k|K|M|G|T)?"
    r"(?![A-Za-z])"
)
_ENGINEERING_SCALE = {
    "p": Decimal("1e-12"),
    "n": Decimal("1e-9"),
    "u": Decimal("1e-6"),
    "m": Decimal("1e-3"),
    "k": Decimal("1e3"),
    "K": Decimal("1e3"),
    "M": Decimal("1e6"),
    "G": Decimal("1e9"),
    "T": Decimal("1e12"),
}


def _skill_text(value: str) -> str:
    return skill_quote(value)


def build_native_setup_attestation_skill(library: str, cell: str) -> str:
    """Build a pure-read SKILL query over HDB and the persisted Maestro setup.

    All values in the result are emitted by Cadence APIs.  The HDB traversal
    uses ``pcdb*`` plus ``hdbBind`` so a config's effective binding is queried
    rather than inferred from ``expand.cfg`` or from the source SKILL text.
    """

    lib = _skill_text(library)
    tb = _skill_text(cell)
    return f'''let((cfg path pc masterGen master instGen inst bind
  session setupDb tests test toolArgs testSession analyses analysis analysisName
  analysisOptions option envOptions corners corner cornerNames models modelNames model
  outputs output specAttempt overallAttempt beforeSessions afterSessions
  variables varNames varName varAttempt varHandle varEnabled testVarEnabled
  currentRunMode runOptions optionName optionHandle sweepsEnabled allVarsDisabled
  closeAttempt flowPair flowText attestationText result)
  flowPair = lambda((flowKey flowPairs)
    let((flowPairValue)
      flowPairValue = assoc(flowKey flowPairs)
      if(flowPairValue then cadr(flowPairValue) else nil)))
  flowText = lambda((flowValue)
    if(flowValue == nil then ""
      else if(stringp(flowValue) then flowValue
        else sprintf(nil "%L" flowValue))))
  beforeSessions = maeGetSessions()
  attestationText = ""
  cfg = nil
  path = nil
  pc = nil
  session = nil
  result = unwindProtect(
    progn(
      cfg = hdbOpen({lib} {tb} "config" "r" "CDBA")
      unless(cfg error("native attestation could not open config"))
      attestationText = strcat(attestationText sprintf(nil "CONFIG|%s|%s|%s|%s|%s|%s\\n"
        funcall(flowText hdbGetLibName(cfg))
        funcall(flowText hdbGetCellName(cfg))
        funcall(flowText hdbGetViewName(cfg))
        funcall(flowText hdbGetTopLibName(cfg))
        funcall(flowText hdbGetTopCellName(cfg))
        funcall(flowText hdbGetTopViewName(cfg))))
      path = hdbCreatePathVector(cfg)
      pc = pcdbOpen(hdbGetTopLibName(cfg) hdbGetTopCellName(cfg)
        hdbGetTopViewName(cfg) "r" "CDBA")
      when(pc
        masterGen = pcdbGetInstMasterGen(pc)
        master = pcdbNextInstMaster(masterGen)
        while(master
          instGen = pcdbGetInstGen(master)
          inst = pcdbNextInst(instGen)
          while(inst
            bind = errset(hdbBind(path
              pcdbInstMasterLib(master) pcdbInstMasterCell(master)
              pcdbInstMasterView(master) pcdbInstName(inst)
              nil nil nil) t)
            when(bind && car(bind)
              bind = car(bind)
              attestationText = strcat(attestationText sprintf(nil "BIND|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s\\n"
                funcall(flowText hdbGetPathStr(path))
                funcall(flowText pcdbInstName(inst))
                funcall(flowText pcdbInstMasterLib(master))
                funcall(flowText pcdbInstMasterCell(master))
                funcall(flowText pcdbInstMasterView(master))
                if(pcdbInstMasterLibFixed(master) then "true" else "false")
                if(pcdbInstMasterCellFixed(master) then "true" else "false")
                if(pcdbInstMasterViewFixed(master) then "true" else "false")
                funcall(flowText if(car(bind) == nil then "" else car(bind)))
                funcall(flowText if(nth(1 bind) == nil then "" else nth(1 bind)))
                funcall(flowText if(nth(2 bind) == nil then "" else nth(2 bind)))
                funcall(flowText if(nth(3 bind) == nil then "" else nth(3 bind))))) )
            inst = pcdbNextInst(instGen))
          master = pcdbNextInstMaster(masterGen)))
      when(pc pcdbClose(pc) pc = nil)
      when(path hdbDestroyPathVector(path) path = nil)
      when(cfg hdbClose(cfg) cfg = nil)

      session = maeOpenSetup({lib} {tb} "maestro"
        ?application "Explorer" ?mode "r")
      unless(session error("native attestation could not open Maestro setup"))
      attestationText = strcat(attestationText sprintf(nil "SESSION|before|%s\\n" funcall(flowText beforeSessions)))
      attestationText = strcat(attestationText sprintf(nil "SESSION|opened|%s\\n" funcall(flowText session)))
      setupDb = axlGetMainSetupDB(session)
      currentRunMode = maeGetCurrentRunMode(?session session)
      attestationText = strcat(attestationText sprintf(nil "RUNMODE|%s\\n"
        funcall(flowText currentRunMode)))
      sweepsEnabled = axlGetAllSweepsEnabled(setupDb)
      attestationText = strcat(attestationText sprintf(nil "SWEEPS_ENABLED|%s\\n"
        if(sweepsEnabled then "true" else "false")))
      allVarsDisabled = axlGetAllVarsDisabled(setupDb)
      attestationText = strcat(attestationText sprintf(nil
        "GLOBAL_VARIABLES_ENABLED|%s\\n"
        if(allVarsDisabled then "false" else "true")))
      variables = axlGetVars(setupDb)
      when(variables
        varNames = cadr(variables)
        foreach(varName varNames
          varAttempt = errset(maeGetVar(varName ?expanded t ?session session) t)
          attestationText = strcat(attestationText sprintf(nil "VARIABLE|%s|%s\\n"
            funcall(flowText varName)
            if(varAttempt then funcall(flowText car(varAttempt)) else "undefined")))
          varHandle = axlGetVar(setupDb varName)
          varEnabled = if(varHandle then axlGetEnabled(varHandle) else nil)
          attestationText = strcat(attestationText sprintf(nil
            "VARIABLE_ENABLED|%s|%s\\n"
            funcall(flowText varName)
            if(varEnabled then "true" else "false")))
      ))
      runOptions = axlGetRunOptions(setupDb "Monte Carlo Sampling")
      when(runOptions
        foreach(optionName cadr(runOptions)
          optionHandle = axlGetRunOption(
            setupDb "Monte Carlo Sampling" optionName)
          when(optionHandle
            attestationText = strcat(attestationText sprintf(nil
              "RUNOPTION|Monte Carlo Sampling|%s|%s\\n"
              funcall(flowText optionName)
              funcall(flowText axlGetRunOptionValue(optionHandle))))))
      )
      tests = maeGetSetup(?session session)
      attestationText = strcat(attestationText sprintf(nil "PERSISTENCE|tests|%d|setup=%s\\n"
        length(tests) funcall(flowText tests)))
      foreach(test tests
        foreach(varName varNames
          testVarEnabled = axlGetEnabledGlobalVarPerTest(setupDb varName test)
          attestationText = strcat(attestationText sprintf(nil
            "TEST_VARIABLE_ENABLED|%s|%s|%s\\n"
            funcall(flowText test)
            funcall(flowText varName)
            if(testVarEnabled then "true" else "false"))))
        toolArgs = axlGetTestToolArgs(axlGetTest(setupDb test))
        attestationText = strcat(
          attestationText
          sprintf(nil "TEST|%s|%s|%s|%s|%s|%s|%s\\n"
          funcall(flowText test)
          funcall(flowText funcall(flowPair "lib" toolArgs))
          funcall(flowText funcall(flowPair "cell" toolArgs))
          funcall(flowText funcall(flowPair "view" toolArgs))
          funcall(flowText funcall(flowPair "sim" toolArgs))
          funcall(flowText funcall(flowPair "state" toolArgs))
          funcall(flowText funcall(flowPair "path" toolArgs))))
        testSession = maeGetTestSession(test ?session session)
        analyses = errset(asiGetEnabledAnalysisList(testSession) t)
        when(analyses
          foreach(analysis car(analyses)
            analysisName = errset(asiGetAnalysisName(analysis) t)
            when(analysisName
              analysisName = funcall(flowText car(analysisName))
              attestationText = strcat(
                attestationText
                sprintf(nil "ANALYSIS|%s|%s|%s\\n" funcall(flowText test)
                  funcall(flowText analysisName)
                  funcall(flowText asiGetAnalysisType(analysis))))
              analysisOptions = errset(maeGetAnalysis(test analysisName
                ?includeEmpty t ?session session) t)
              when(analysisOptions
                foreach(optionName list("stop" "maxstep")
                  option = assoc(optionName car(analysisOptions))
                  when(option
                    attestationText = strcat(
                      attestationText
                      sprintf(nil "ANALYSIS_OPTION|%s|%s|%s|%s\\n" funcall(flowText test)
                        funcall(flowText analysisName)
                        funcall(flowText car(option))
                        funcall(flowText cadr(option))))
                  )
                )
              )
            )
          )
        )
        envOptions = maeGetEnvOption(test ?includeEmpty t ?session session)
        foreach(option envOptions
          when(member(car(option) list("modelFiles" "amsIEsList"
              "useIeSetup" "ieUseUcmAsDefault"))
            attestationText = strcat(
              attestationText
              sprintf(nil "ENV|%s|%s|%s\\n" funcall(flowText test)
                funcall(flowText car(option))
                funcall(flowText cadr(option))))
          )
        )
        outputs = maeGetTestOutputs(test ?session session)
        foreach(output outputs
          specAttempt = errset(maeGetSpecStatus(output~>name test) t)
          attestationText = strcat(
            attestationText
            sprintf(nil "OUTPUT|%s|%s|%s|%s|%s|%s|%s|%s|%s\\n"
              funcall(flowText test)
              funcall(flowText output~>name)
              funcall(flowText output~>type)
              funcall(flowText output~>signal)
              funcall(flowText output~>expression)
              funcall(flowText output~>evalType)
              if(output~>save then "true" else "false")
              if(output~>plot then "true" else "false")
              if(specAttempt then funcall(flowText car(specAttempt)) else "undefined")))
        )
        overallAttempt = errset(maeGetOverallSpecStatus(?verbose nil) t)
        attestationText = strcat(
          attestationText
          sprintf(nil "SPEC_OVERALL|%s|%s\\n" funcall(flowText test)
            if(overallAttempt then funcall(flowText car(overallAttempt)) else "undefined")))
      )
      corners = axlGetCorners(setupDb)
      when(corners
        cornerNames = cadr(corners)
        foreach(cornerName cornerNames
          corner = axlGetCorner(setupDb cornerName)
          models = axlGetModels(corner)
          modelNames = if(models then cadr(models) else nil)
          foreach(modelName modelNames
            model = axlGetModel(corner modelName)
            attestationText = strcat(attestationText sprintf(nil "MODEL|%s|%s|%s|%s\\n"
              funcall(flowText cornerName) funcall(flowText modelName)
              funcall(flowText axlGetModelFile(model))
              funcall(flowText axlGetModelSection(model))))
          )
        )
      )
      closeAttempt = errset(maeCloseSession(?session session ?forceClose nil) t)
      unless(closeAttempt error("native attestation Maestro close failed"))
      session = nil
      afterSessions = maeGetSessions()
      attestationText = strcat(attestationText sprintf(nil "SESSION|closed|%s\\n" funcall(flowText afterSessions)))
      attestationText
    )
    progn(
      when(pc pcdbClose(pc))
      when(path hdbDestroyPathVector(path))
      when(cfg hdbClose(cfg))
      when(session
        errset(maeCloseSession(?session session ?forceClose nil) t))
    )
  )
  result
)'''


def _field(value: str) -> str:
    return value.strip()


def _normalize_calculator_expression(expression: str) -> str:
    """Compare Cadence's numeric spelling without evaluating the expression.

    Maestro commonly returns ``2e-09`` for a source expression written as
    ``2n``.  This is a lexical normalization of numeric tokens only; it does
    not execute, simplify, or otherwise reimplement Calculator semantics.
    """

    def replace(match: re.Match[str]) -> str:
        literal, suffix = match.group(1), match.group(2)
        try:
            value = Decimal(literal)
            if suffix:
                value *= _ENGINEERING_SCALE[suffix]
            normalized = format(value.normalize(), "e")
            mantissa, exponent = normalized.split("e")
            exponent_value = int(exponent)
            if mantissa.endswith(".0"):
                mantissa = mantissa[:-2]
            return (
                mantissa
                if exponent_value == 0
                else f"{mantissa}e{exponent_value}"
            )
        except (InvalidOperation, ValueError):
            return match.group(0)

    normalized = " ".join(_CALCULATOR_NUMBER.sub(replace, expression).split())
    compacted: list[str] = []
    quoted = False
    escaped = False
    skip_after_operator = False
    for character in normalized:
        if quoted:
            compacted.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
            continue
        if character == '"':
            quoted = True
            compacted.append(character)
        elif character in "+-*/^":
            while compacted and compacted[-1] == " ":
                compacted.pop()
            compacted.append(character)
            skip_after_operator = True
        elif skip_after_operator and character.isspace():
            continue
        else:
            skip_after_operator = False
            compacted.append(character)
    normalized = "".join(compacted)
    while normalized.startswith("(") and normalized.endswith(")"):
        depth = 0
        quoted = False
        escaped = False
        wraps_expression = True
        for index, character in enumerate(normalized):
            if quoted:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    quoted = False
                continue
            if character == '"':
                quoted = True
            elif character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0 and index != len(normalized) - 1:
                    wraps_expression = False
                    break
        if wraps_expression and depth == 0 and not quoted:
            normalized = normalized[1:-1].strip()
        else:
            break
    return normalized


def _engineering_number(value: object) -> float | None:
    """Parse one Cadence engineering-number spelling without evaluation."""

    matches = list(_CALCULATOR_NUMBER.finditer(str(value)))
    if len(matches) != 1:
        return None
    literal, suffix = matches[0].group(1), matches[0].group(2)
    try:
        number = Decimal(literal)
        if suffix:
            number *= _ENGINEERING_SCALE[suffix]
        result = float(number)
    except (InvalidOperation, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _parse_rows(output: str) -> dict[str, Any]:
    parsed: dict[str, Any] = {
        "config": None,
        "bindings": [],
        "tests": [],
        "analyses": [],
        "analysis_options": [],
        "environment": [],
        "outputs": [],
        "models": [],
        "spec_status": [],
        "overall_spec_status": [],
        "variables": [],
        "variable_enabled": [],
        "test_variable_enabled": [],
        "global_variables_enabled": [],
        "run_modes": [],
        "sweeps_enabled": [],
        "run_options": [],
        "sessions": [],
        "persistence": [],
    }
    for line in output.splitlines():
        fields = line.strip().split("|")
        if not fields or not fields[0] in {
            "CONFIG",
            "BIND",
            "TEST",
            "ANALYSIS",
            "ANALYSIS_OPTION",
            "ENV",
            "OUTPUT",
            "MODEL",
            "SPEC_OVERALL",
            "VARIABLE",
            "VARIABLE_ENABLED",
            "TEST_VARIABLE_ENABLED",
            "GLOBAL_VARIABLES_ENABLED",
            "RUNMODE",
            "SWEEPS_ENABLED",
            "RUNOPTION",
            "SESSION",
            "PERSISTENCE",
        }:
            continue
        kind = fields[0]
        if kind == "CONFIG" and len(fields) >= 7:
            parsed["config"] = {
                "library": _field(fields[1]),
                "cell": _field(fields[2]),
                "view": _field(fields[3]),
                "top_library": _field(fields[4]),
                "top_cell": _field(fields[5]),
                "top_view": _field(fields[6]),
            }
        elif kind == "BIND" and len(fields) >= 13:
            parsed["bindings"].append(
                {
                    "path": fields[1],
                    "instance": fields[2],
                    "requested_library": fields[3],
                    "requested_cell": fields[4],
                    "requested_view": fields[5],
                    "library_fixed": fields[6] == "true",
                    "cell_fixed": fields[7] == "true",
                    "view_fixed": fields[8] == "true",
                    "bound_library": fields[9],
                    "bound_cell": fields[10],
                    "bound_view": fields[11],
                    "signature": fields[12],
                }
            )
        elif kind == "TEST" and len(fields) >= 8:
            parsed["tests"].append(
                {
                    "name": fields[1],
                    "library": fields[2],
                    "cell": fields[3],
                    "view": fields[4],
                    "simulator": fields[5],
                    "state": fields[6],
                    "path": fields[7],
                }
            )
        elif kind == "ANALYSIS" and len(fields) >= 4:
            parsed["analyses"].append(
                {"test": fields[1], "name": fields[2], "type": fields[3]}
            )
        elif kind == "ANALYSIS_OPTION" and len(fields) >= 5:
            parsed["analysis_options"].append(
                {
                    "test": fields[1],
                    "analysis": fields[2],
                    "name": fields[3],
                    "value": fields[4],
                }
            )
        elif kind == "ENV" and len(fields) >= 4:
            parsed["environment"].append(
                {"test": fields[1], "name": fields[2], "value": fields[3]}
            )
        elif kind == "OUTPUT":
            if len(fields) != 10:
                raise ValueError("native attestation OUTPUT row must have 10 fields")
            output = {
                "test": fields[1],
                "name": fields[2],
                "type": fields[3],
                "signal": fields[4],
                "expression": fields[5],
                "eval_type": fields[6],
                "save": fields[7] == "true",
                "plot": fields[8] == "true",
                "spec_status": fields[9],
            }
            parsed["outputs"].append(output)
            parsed["spec_status"].append(
                {
                    "test": fields[1],
                    "name": fields[2],
                    "status": fields[9],
                }
            )
        elif kind == "MODEL" and len(fields) >= 5:
            parsed["models"].append(
                {
                    "corner": fields[1],
                    "name": fields[2],
                    "file": fields[3],
                    "section": fields[4],
                }
            )
        elif kind == "SPEC_OVERALL" and len(fields) >= 3:
            parsed["overall_spec_status"].append(
                {"test": fields[1], "status": fields[2]}
            )
        elif kind == "VARIABLE" and len(fields) >= 3:
            parsed["variables"].append(
                {"name": fields[1], "value": fields[2]}
            )
        elif kind == "VARIABLE_ENABLED" and len(fields) >= 3:
            parsed["variable_enabled"].append(
                {"name": fields[1], "enabled": fields[2] == "true"}
            )
        elif kind == "TEST_VARIABLE_ENABLED" and len(fields) >= 4:
            parsed["test_variable_enabled"].append(
                {
                    "test": fields[1],
                    "name": fields[2],
                    "enabled": fields[3] == "true",
                }
            )
        elif kind == "GLOBAL_VARIABLES_ENABLED" and len(fields) >= 2:
            parsed["global_variables_enabled"].append(fields[1] == "true")
        elif kind == "RUNMODE" and len(fields) >= 2:
            parsed["run_modes"].append(fields[1])
        elif kind == "SWEEPS_ENABLED" and len(fields) >= 2:
            parsed["sweeps_enabled"].append(fields[1] == "true")
        elif kind == "RUNOPTION" and len(fields) >= 4:
            parsed["run_options"].append(
                {"mode": fields[1], "name": fields[2], "value": fields[3]}
            )
        elif kind == "SESSION" and len(fields) >= 3:
            parsed["sessions"].append({"state": fields[1], "value": fields[2]})
        elif kind == "PERSISTENCE" and len(fields) >= 4:
            parsed["persistence"].append(
                {"kind": fields[1], "count": fields[2], "value": fields[3]}
            )
    return parsed


def _expected_output_contract(spec: Any) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    contract = spec.native_setup.rdb_contract
    if contract is None:
        return set(), set()
    return set(contract.waveform_outputs), set(contract.scalar_outputs)


def _diagnostic(
    passed: bool,
    *,
    expected: Any,
    observed: Any,
    missing: Any = (),
    unexpected: Any = (),
) -> dict[str, Any]:
    """Build JSON-safe field-level comparison evidence."""

    return {
        "passed": passed,
        "expected": expected,
        "observed": observed,
        "missing": list(missing),
        "unexpected": list(unexpected),
    }


def compare_native_setup_attestation(
    spec: Any,
    attestation: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare official observations with the declarative identity model."""

    config = attestation.get("config") or {}
    tests = list(attestation.get("tests") or ())
    bindings = list(attestation.get("bindings") or ())
    analyses = list(attestation.get("analyses") or ())
    analysis_options = list(attestation.get("analysis_options") or ())
    outputs = list(attestation.get("outputs") or ())
    models = list(attestation.get("models") or ())
    environment = list(attestation.get("environment") or ())
    variables = list(attestation.get("variables") or ())
    variable_enabled = list(attestation.get("variable_enabled") or ())
    test_variable_enabled = list(attestation.get("test_variable_enabled") or ())
    global_variables_enabled = list(
        attestation.get("global_variables_enabled") or ()
    )
    run_modes = list(attestation.get("run_modes") or ())
    sweeps_enabled = list(attestation.get("sweeps_enabled") or ())
    run_options = list(attestation.get("run_options") or ())
    persistence = list(attestation.get("persistence") or ())
    contract = spec.native_setup.rdb_contract
    diagnostic_contract = None if contract is None else contract.diagnostic_equivalence
    diagnostic_requirements = (
        {}
        if diagnostic_contract is None
        else diagnostic_contract.attestation_requirements
    )
    expected_run_options = dict(diagnostic_requirements.get("run_options", {}))
    actual_run_options = {
        str(row.get("name")): str(row.get("value"))
        for row in run_options
        if row.get("mode") == "Monte Carlo Sampling"
    }
    expected_design_variables = {
        str(name): _engineering_number(value)
        for name, value in diagnostic_requirements.get("design_variables", {}).items()
    }
    actual_design_variables = {
        str(row.get("name")): _engineering_number(row.get("value"))
        for row in variables
        if row.get("name")
    }
    actual_variable_enabled = {
        str(row.get("name")): bool(row.get("enabled"))
        for row in variable_enabled
    }
    actual_test_variable_enabled = {
        (str(row.get("test")), str(row.get("name"))): bool(row.get("enabled"))
        for row in test_variable_enabled
    }
    expected_tests = set(contract.tests) if contract is not None else set()
    expected_analysis_options = {
        tuple(key): _engineering_number(value)
        for key, value in diagnostic_requirements.get("analysis_options", {}).items()
    }
    actual_analysis_options = {
        (
            str(row.get("test")),
            str(row.get("analysis")),
            str(row.get("name")),
        ): _engineering_number(row.get("value"))
        for row in analysis_options
    }
    actual_tests = {str(row.get("name")) for row in tests}
    dut_source_bindings = [
        row
        for row in bindings
        if row.get("requested_library") == spec.library
        and row.get("requested_cell") == spec.dut
        and row.get("bound_library") == spec.library
        and row.get("bound_cell") == spec.dut
        and bool(row.get("bound_view"))
    ]
    expected_waveforms, expected_scalars = _expected_output_contract(spec)
    actual_waveforms = {
        (row.get("name"), row.get("signal"))
        for row in outputs
        if row.get("type") == "net"
    }
    actual_scalars = {
        (row.get("name"), _normalize_calculator_expression(str(row.get("expression") or "")))
        for row in outputs
        if row.get("type") == "point" or row.get("eval_type") == "point"
    }
    expected_scalar_names = {name for name, _expression in expected_scalars}
    materialized_scalar_names = {
        str(row.get("name"))
        for row in outputs
        if (row.get("type") == "point" or row.get("eval_type") == "point")
        and row.get("save") is True
        and row.get("plot") is True
    }
    expected_scalars = {
        (name, _normalize_calculator_expression(expression))
        for name, expression in expected_scalars
    }
    platform_model = spec.native_setup.pdk.simulation.default
    default_model = (platform_model.file.name, platform_model.single_section)
    explicit_setup_models = set(
        () if contract is None else contract.setup_model_identities
    )
    expected_models = set(
        diagnostic_requirements.get(
            "models", explicit_setup_models or {default_model}
        )
    )
    actual_models = {
        (Path(str(row.get("file"))).name, row.get("section")) for row in models
    }
    actual_corners = {str(row.get("corner")) for row in models}
    actual_simulators = sorted({str(row.get("simulator")) for row in tests})
    actual_analysis_rows = [
        {
            "test": row.get("test"),
            "name": row.get("name"),
            "type": row.get("type"),
        }
        for row in analyses
    ]
    actual_dut_bindings = [
        {
            "path": row.get("path"),
            "instance": row.get("instance"),
            "requested_view": row.get("requested_view"),
            "bound_library": row.get("bound_library"),
            "bound_cell": row.get("bound_cell"),
            "bound_view": row.get("bound_view"),
            "view_fixed": row.get("view_fixed"),
        }
        for row in bindings
        if row.get("requested_library") == spec.library
        and row.get("requested_cell") == spec.dut
    ]
    expected_waveforms_sorted = sorted(expected_waveforms)
    actual_waveforms_sorted = sorted(actual_waveforms)
    expected_scalars_sorted = sorted(expected_scalars)
    actual_scalars_sorted = sorted(actual_scalars)
    expected_corners = sorted(contract.corners) if contract is not None else []
    actual_models_sorted = sorted(actual_models)
    expected_tests_sorted = sorted(expected_tests)
    actual_tests_sorted = sorted(actual_tests)
    output_spec_status = list(attestation.get("spec_status") or ())
    checks = {
        "config_top": config.get("top_library") == spec.library
        and config.get("top_cell") == spec.cell
        and config.get("top_view") == spec.top_view,
        "config_view": config.get("library") == spec.library
        and config.get("cell") == spec.cell
        and config.get("view") == "config",
        "dut_source_view": (
            bool(dut_source_bindings)
        ),
        "simulator": bool(tests)
        and all(row.get("simulator") == spec.simulator for row in tests),
        "test_identity": bool(actual_tests)
        and (not expected_tests or expected_tests.issubset(actual_tests)),
        "analysis": bool(analyses),
        "analysis_options": not diagnostic_requirements
        or all(
            actual_analysis_options.get(key) == value
            for key, value in expected_analysis_options.items()
        ),
        "corner": bool(models)
        and (not contract or set(contract.corners).issubset({row.get("corner") for row in models})),
        "model_file_section": bool(actual_models)
        and expected_models.issubset(actual_models),
        "ams_interface": spec.simulator != "ams"
        or any(
            row.get("name") == "amsIEsList" and row.get("value") not in {"", "nil"}
            for row in environment
        ),
        "waveform_outputs": not expected_waveforms
        or expected_waveforms.issubset(actual_waveforms),
        "calculator_scalars": not expected_scalars
        or expected_scalars.issubset(actual_scalars),
        "calculator_materialization": not diagnostic_requirements
        or expected_scalar_names.issubset(materialized_scalar_names),
        "spec_status_api": bool(attestation.get("overall_spec_status"))
        and all(
            row.get("status") in {"pass", "fail", "undefined", ""}
            for row in attestation.get("overall_spec_status") or ()
        )
        and len(output_spec_status) == len(outputs)
        and all(
            row.get("status") in {"pass", "fail", "undefined", ""}
            for row in output_spec_status
        ),
        "persistence": bool(attestation.get("sessions"))
        and bool(persistence)
        and any(row.get("state") == "closed" for row in attestation["sessions"]),
        "design_variables": not diagnostic_requirements
        or actual_design_variables == expected_design_variables,
        "global_variables_enabled": not diagnostic_requirements
        or not expected_design_variables
        or True in global_variables_enabled,
        "design_variable_enabled": not diagnostic_requirements
        or all(
            actual_variable_enabled.get(name) is True
            for name in expected_design_variables
        ),
        "test_design_variable_enabled": not diagnostic_requirements
        or all(
            actual_test_variable_enabled.get((test, name)) is True
            for test in expected_tests
            for name in expected_design_variables
        ),
        "run_mode": not diagnostic_requirements
        or diagnostic_requirements.get("run_mode") in run_modes,
        "point_sweeps_enabled": not diagnostic_requirements
        or diagnostic_requirements.get("sweeps_enabled") in sweeps_enabled,
        "monte_carlo_options": not diagnostic_requirements
        or all(
            actual_run_options.get(name) == value
            for name, value in expected_run_options.items()
        ),
    }
    diagnostics = {
        "config_top": _diagnostic(
            checks["config_top"],
            expected={
                "top_library": spec.library,
                "top_cell": spec.cell,
                "top_view": spec.top_view,
            },
            observed={
                key: config.get(key)
                for key in ("top_library", "top_cell", "top_view")
            },
        ),
        "config_view": _diagnostic(
            checks["config_view"],
            expected={"library": spec.library, "cell": spec.cell, "view": "config"},
            observed={key: config.get(key) for key in ("library", "cell", "view")},
        ),
        "dut_source_view": _diagnostic(
            checks["dut_source_view"],
            expected={
                "library": spec.library,
                "cell": spec.dut,
                "bound_view": "non-empty official binding",
            },
            observed=actual_dut_bindings,
        ),
        "simulator": _diagnostic(
            checks["simulator"],
            expected=spec.simulator,
            observed=actual_simulators,
        ),
        "test_identity": _diagnostic(
            checks["test_identity"],
            expected=expected_tests_sorted,
            observed=actual_tests_sorted,
            missing=sorted(expected_tests - actual_tests),
            unexpected=sorted(actual_tests - expected_tests) if expected_tests else (),
        ),
        "analysis": _diagnostic(
            checks["analysis"],
            expected="at least one official persisted analysis",
            observed=actual_analysis_rows,
        ),
        "analysis_options": _diagnostic(
            checks["analysis_options"],
            expected=(
                "not-required"
                if not diagnostic_requirements
                else [
                    [*key, value]
                    for key, value in sorted(expected_analysis_options.items())
                ]
            ),
            observed=[
                [*key, value]
                for key, value in sorted(actual_analysis_options.items())
            ],
            missing=(
                ()
                if not diagnostic_requirements
                else [
                    [*key, value]
                    for key, value in sorted(expected_analysis_options.items())
                    if actual_analysis_options.get(key) != value
                ]
            ),
        ),
        "corner": _diagnostic(
            checks["corner"],
            expected=expected_corners,
            observed=sorted(actual_corners),
            missing=sorted(set(expected_corners) - actual_corners),
        ),
        "model_file_section": _diagnostic(
            checks["model_file_section"],
            expected=[[file, section] for file, section in sorted(expected_models)],
            observed=[[file, section] for file, section in actual_models_sorted],
            missing=[
                [file, section]
                for file, section in sorted(expected_models - actual_models)
            ],
        ),
        "ams_interface": _diagnostic(
            checks["ams_interface"],
            expected=(
                "not-required"
                if spec.simulator != "ams"
                else {"name": "amsIEsList", "value": "non-empty"}
            ),
            observed=[
                {"name": row.get("name"), "value": row.get("value")}
                for row in environment
                if row.get("name") in {"amsIEsList", "useIeSetup", "ieUseUcmAsDefault"}
            ],
        ),
        "waveform_outputs": _diagnostic(
            checks["waveform_outputs"],
            expected=[[name, signal] for name, signal in expected_waveforms_sorted],
            observed=[[name, signal] for name, signal in actual_waveforms_sorted],
            missing=[
                [name, signal]
                for name, signal in sorted(expected_waveforms - actual_waveforms)
            ],
        ),
        "calculator_scalars": _diagnostic(
            checks["calculator_scalars"],
            expected=[[name, expression] for name, expression in expected_scalars_sorted],
            observed=[[name, expression] for name, expression in actual_scalars_sorted],
            missing=[
                [name, expression]
                for name, expression in sorted(expected_scalars - actual_scalars)
            ],
        ),
        "calculator_materialization": _diagnostic(
            checks["calculator_materialization"],
            expected=(
                "not-required"
                if not diagnostic_requirements
                else sorted(expected_scalar_names)
            ),
            observed=sorted(materialized_scalar_names),
            missing=(
                ()
                if not diagnostic_requirements
                else sorted(expected_scalar_names - materialized_scalar_names)
            ),
        ),
        "spec_status_api": _diagnostic(
            checks["spec_status_api"],
            expected="overall and per-output status observations from Maestro API",
            observed={
                "overall": list(attestation.get("overall_spec_status") or ()),
                "outputs": output_spec_status,
            },
        ),
        "persistence": _diagnostic(
            checks["persistence"],
            expected="persisted setup plus an observed exact Maestro close",
            observed={
                "sessions": list(attestation.get("sessions") or ()),
                "persistence": list(persistence),
            },
        ),
        "design_variables": _diagnostic(
            checks["design_variables"],
            expected=(
                "not-required"
                if not diagnostic_requirements
                else expected_design_variables
            ),
            observed=actual_design_variables,
        ),
        "global_variables_enabled": _diagnostic(
            checks["global_variables_enabled"],
            expected="not-required" if not diagnostic_requirements else True,
            observed=global_variables_enabled,
        ),
        "design_variable_enabled": _diagnostic(
            checks["design_variable_enabled"],
            expected=(
                "not-required"
                if not diagnostic_requirements
                else [
                    {"name": name, "enabled": True}
                    for name in sorted(expected_design_variables)
                ]
            ),
            observed=variable_enabled,
        ),
        "test_design_variable_enabled": _diagnostic(
            checks["test_design_variable_enabled"],
            expected=(
                "not-required"
                if not diagnostic_requirements
                else [
                    {"test": test, "name": name, "enabled": True}
                    for test in sorted(expected_tests)
                    for name in sorted(expected_design_variables)
                ]
            ),
            observed=test_variable_enabled,
        ),
        "run_mode": _diagnostic(
            checks["run_mode"],
            expected=(
                "not-required"
                if not diagnostic_requirements
                else diagnostic_requirements.get("run_mode")
            ),
            observed=run_modes,
        ),
        "point_sweeps_enabled": _diagnostic(
            checks["point_sweeps_enabled"],
            expected=(
                "not-required"
                if not diagnostic_requirements
                else diagnostic_requirements.get("sweeps_enabled")
            ),
            observed=sweeps_enabled,
        ),
        "monte_carlo_options": _diagnostic(
            checks["monte_carlo_options"],
            expected=(
                "not-required"
                if not diagnostic_requirements
                else expected_run_options
            ),
            observed=actual_run_options,
        ),
    }
    return {
        "checks": checks,
        "diagnostics": diagnostics,
        "mismatches": [
            {"check": name, **diagnostics[name]}
            for name, passed in checks.items()
            if not passed
        ],
        "passed": all(checks.values()),
    }


def compare_native_setup_attestation_output(
    spec: Any,
    output: str,
) -> dict[str, Any]:
    """Parse one official attestation transcript and compare it at the public seam."""

    observations = _parse_rows(output)
    return {
        "observations": observations,
        **compare_native_setup_attestation(spec, observations),
    }


def attest_native_setup(
    spec: Any,
    client: Any,
    *,
    operation: Any,
    timeout: int = 300,
) -> dict[str, Any]:
    """Query and compare one existing config/Maestro pair without mutation."""

    for view in ("config", "maestro"):
        require_workspace_capability(
            operation,
            client,
            authority=WorkspaceAuthority.READ,
            library=spec.library,
            cell=spec.cell,
            view=view,
        )
    source = audit_cellview_delta_skill(
        own_synchronous_cellview_delta_skill(
            build_native_setup_attestation_skill(spec.library, spec.cell),
            label=f"native setup semantic attestation {spec.library}/{spec.cell}",
        ),
        label=f"native setup semantic attestation {spec.library}/{spec.cell}",
    )
    result = require_bridge_confirmation(
        operation,
        f"native setup semantic attestation {spec.library}/{spec.cell}",
        lambda: client.execute_skill(source, timeout=timeout),
    )
    if result.errors:
        raise RuntimeError(result.errors[0])
    comparison = compare_native_setup_attestation_output(
        spec,
        decode_skill_output(result.output or ""),
    )
    payload = {
        "schema": ATTESTATION_SCHEMA,
        "kind": "cadence-native-setup-semantic-attestation",
        "source": "Cadence HDB/ADE/Maestro official API observations",
        "library": spec.library,
        "cell": spec.cell,
        **comparison,
        "simulation_run": False,
    }
    if not payload["passed"]:
        raise RuntimeError(
            f"native setup semantic attestation failed for {spec.cell}: "
            f"{payload['mismatches']}"
        )
    return payload
