"""Project safety adapters around public bridge schematic operations."""

from __future__ import annotations

import re
from typing import Any, Mapping

from sigilicon.virtuoso.bridge import decode_skill_output
from sigilicon.virtuoso.bridge import bridge_read_schematic

from sigilicon.virtuoso.capability import (
    dispatch_oa_mutation,
    require_workspace_capability,
)
from sigilicon.virtuoso.confirmation import require_bridge_confirmation
from sigilicon.virtuoso.oa import (
    audit_cellview_delta_skill,
    own_synchronous_cellview_delta_skill,
    skill_quote,
)


_PARAMETER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class _SchematicReadClient:
    """Harden one public bridge reader call without owning its parser."""

    def __init__(
        self,
        client: Any,
        *,
        library: str,
        cell: str,
        operation: Any,
    ) -> None:
        self._client = client
        self._library = library
        self._cell = cell
        self._operation = operation
        self._label = f"schematic read {library}/{cell}"

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def execute_skill(self, source: str, **kwargs: Any) -> Any:
        """Validate the bridge-owned read lifecycle, then audit its delta."""

        bridge_open = (
            f"dbOpenCellViewByType({skill_quote(self._library)} "
            f'{skill_quote(self._cell)} "schematic" "schematic" "r")'
        )
        owned_prefix = (
            "let((cv result)\n"
            "  unwindProtect(\n"
            "    progn(\n"
            f"  cv = {bridge_open}"
        )
        owned_suffix = '''  )
    progn(
      when(cv
        unless(dbClose(cv)
          error("schematic reader close failed"))
        cv = nil))))'''
        stripped_source = source.strip()
        if (
            source.count(bridge_open) != 1
            or source.count("unwindProtect(") != 1
            or source.count("dbClose(cv)") != 1
            or not stripped_source.startswith(owned_prefix)
            or not stripped_source.endswith(owned_suffix)
        ):
            raise RuntimeError(
                "unsupported virtuoso-bridge owned schematic reader source"
            )
        protected = audit_cellview_delta_skill(
            own_synchronous_cellview_delta_skill(
                stripped_source,
                label=self._label,
            ),
            label=self._label,
        )
        return require_bridge_confirmation(
            self._operation,
            self._label,
            lambda: self._client.execute_skill(protected, **kwargs),
        )


def read_schematic(
    client: Any,
    library: str,
    cell: str,
    *,
    include_positions: bool,
    timeout: int = 300,
    operation: Any,
) -> dict[str, Any]:
    """Read a schematic while closing the exact read handle in all paths."""

    require_workspace_capability(
        operation,
        client,
        library=library,
        cell=cell,
        view="schematic",
    )

    safe_client = _SchematicReadClient(
        client,
        library=library,
        cell=cell,
        operation=operation,
    )
    try:
        schematic = bridge_read_schematic(
            safe_client,
            library,
            cell,
            include_positions=include_positions,
            param_filters=None,
            timeout=timeout,
        )
        saved = _read_saved_string_parameters(
            client, library, cell, operation=operation, timeout=timeout
        )
        for instance in schematic["instances"]:
            parameters = instance.get("params", {})
            for name, value in saved.get(instance["name"], {}).items():
                if name in parameters:
                    parameters[name] = value
        return schematic
    except RuntimeError as exc:
        raise RuntimeError(f"cannot read schematic {library}/{cell}: {exc}") from exc


def _read_saved_string_parameters(
    client: Any,
    library: str,
    cell: str,
    *,
    operation: Any,
    timeout: int,
) -> dict[str, dict[str, str]]:
    """Read saved expressions before CDF applies display precision formatting."""

    source = f'''let((cv out)
  out = ""
  unwindProtect(
    progn(
      cv = dbOpenCellViewByType({skill_quote(library)} {skill_quote(cell)} "schematic" "schematic" "r")
      unless(cv error("cannot read saved schematic parameters"))
      foreach(inst cv~>instances
        foreach(prop inst~>prop
          when(equal(prop~>valueType "string")
            out = strcat(out sprintf(nil "%s|%s|%L\\n"
              inst~>name prop~>name prop~>value)))))
      out)
    when(cv unless(dbClose(cv) error("saved parameter reader close failed"))))
)'''
    label = f"saved schematic parameters {library}/{cell}"
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
        raise RuntimeError(f"{label}: {result.errors[0]}")
    saved: dict[str, dict[str, str]] = {}
    for row in decode_skill_output(result.output or "").splitlines():
        fields = row.split("|", 2)
        if len(fields) != 3:
            raise RuntimeError(f"{label}: invalid saved parameter row")
        instance, name, value = fields
        saved.setdefault(instance, {})[name] = decode_skill_output(value)
    return saved


def normalize_instance_parameters(params: Mapping[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for raw_name, value in params.items():
        if not _PARAMETER_RE.fullmatch(raw_name):
            raise ValueError(f"unsupported CDF parameter name: {raw_name!r}")
        normalized[raw_name] = value
    return normalized


def read_instance_parameters(
    client: Any,
    library: str,
    cell: str,
    instance: str,
    names: tuple[str, ...],
    *,
    operation: Any,
) -> dict[str, str]:
    """Read selected instance parameters with a guaranteed cellview close."""

    require_workspace_capability(
        operation,
        client,
        library=library,
        cell=cell,
        view="schematic",
    )
    for name in names:
        if not _PARAMETER_RE.fullmatch(name):
            raise ValueError(f"unsupported CDF parameter name: {name!r}")
    wanted = " ".join(skill_quote(name) for name in names)
    source = f'''let((cv inst iCDF param out attempt)
  cv = nil
  out = ""
  attempt = errset(
    unwindProtect(
      progn(
        cv = dbOpenCellViewByType({skill_quote(library)} {skill_quote(cell)} "schematic" "schematic" "r")
        unless(cv error("cannot open target schematic"))
        inst = car(setof(item cv~>instances item~>name == {skill_quote(instance)}))
        unless(inst error("instance not found"))
        iCDF = cdfGetInstCDF(inst)
        unless(iCDF error("instance has no CDF"))
        foreach(name list({wanted})
          param = get(iCDF name)
          when(param out = strcat(out name "|" sprintf(nil "%L" param~>value) "\\n")))
        out
      )
      when(cv unless(dbClose(cv) error("schematic read close failed")) cv = nil)
    )
    nil
  )
  unless(attempt error("instance parameter read failed"))
  car(attempt)
)'''
    result = require_bridge_confirmation(
        operation,
        f"read parameters {library}/{cell}/{instance}",
        lambda: client.execute_skill(
            audit_cellview_delta_skill(
                own_synchronous_cellview_delta_skill(
                    source,
                    label=f"parameter read {library}/{cell}/{instance}",
                ),
                label=f"parameter read {library}/{cell}/{instance}",
            ),
            timeout=60,
        ),
    )
    if result.errors:
        raise RuntimeError(
            f"failed to read {library}/{cell}/{instance} parameters: {result.errors[0]}"
        )
    output = decode_skill_output(result.output or "")
    values: dict[str, str] = {}
    for line in output.splitlines():
        name, separator, value = line.partition("|")
        if separator and name in names:
            values[name] = value.strip().strip('"')
    return values


def set_instance_parameters(
    client: Any,
    library: str,
    cell: str,
    instance: str,
    params: Mapping[str, str],
    *,
    operation: Any,
    invoke_callbacks: bool = True,
) -> dict[str, str]:
    """Apply CDF values/callbacks and save with full CDF and OA cleanup."""

    normalized = normalize_instance_parameters(params)
    if not normalized:
        return {}
    names = " ".join(skill_quote(name) for name in normalized)
    bindings = "\n".join(
        f"  setarray(paramVals {skill_quote(name)} {skill_quote(value)})"
        for name, value in normalized.items()
    )
    callbacks = (
        f'''        foreach(name list({names})
          param = get(cCDF name)
          callback = param~>callback
          when(callback && callback != ""
            unless(errset(evalstring(callback) t)
              error(sprintf(nil "CDF callback failed: %s" name)))))'''
        if invoke_callbacks
        else ""
    )
    # CDF numeric formatting can round string-valued source expressions. With
    # callbacks disabled, retain the caller's exact spelling after CDF update.
    exact_strings = (
        f'''        foreach(name list({names})
          param = get(cCDF name)
          when(equal(param~>paramType "string")
            unless(dbReplaceProp(inst name "string" arrayref(paramVals name))
              error(sprintf(nil "cannot preserve exact CDF value: %s" name)))))'''
        if not invoke_callbacks
        else ""
    )
    source = f'''let((cv inst iCDF cCDF saved paramVals param callback attempt)
  cv = nil
  cCDF = nil
  saved = nil
  paramVals = makeTable('flowParamValues)
{bindings}
  attempt = errset(
    unwindProtect(
      progn(
        cv = dbOpenCellViewByType({skill_quote(library)} {skill_quote(cell)} "schematic" "schematic" "a")
        unless(cv error("cannot open target schematic for update"))
        inst = car(setof(item cv~>instances item~>name == {skill_quote(instance)}))
        unless(inst error("instance not found"))
        iCDF = cdfGetInstCDF(inst)
        unless(iCDF error("instance has no CDF"))
        cCDF = cdfGetCellCDF(ddGetObj(inst~>libName inst~>cellName))
        unless(cCDF error("cell CDF not found"))
        saved = makeTable('flowSavedCdfValues)
        foreach(param cCDF~>parameters
          setarray(saved param~>name param~>value)
          when(get(iCDF param~>name)
            putpropq(param get(iCDF param~>name)~>value value)))
        foreach(name list({names})
          param = get(cCDF name)
          unless(param error(sprintf(nil "unknown CDF parameter: %s" name)))
          if(equal(param~>paramType "int")
            then param~>value = atoi(arrayref(paramVals name))
            else param~>value = arrayref(paramVals name)))
{callbacks}
        cdfUpdateInstParam(inst)
{exact_strings}
        schCheck(cv)
        unless(dbSave(cv) error("schematic save failed"))
        t
      )
      progn(
        when(cCDF && saved
          foreach(param cCDF~>parameters
            putpropq(param arrayref(saved param~>name) value)))
        when(cv unless(dbClose(cv) error("schematic update close failed")) cv = nil)
      )
    )
    nil
  )
  unless(attempt && car(attempt) error("instance parameter update failed"))
  t
)'''
    result = require_bridge_confirmation(
        operation,
        f"update parameters {library}/{cell}/{instance}",
        lambda: dispatch_oa_mutation(
            operation,
            client,
            library=library,
            cell=cell,
            phase="set instance parameters SKILL dispatch",
            callback=lambda: client.execute_skill(
                audit_cellview_delta_skill(
                    own_synchronous_cellview_delta_skill(
                        source,
                        label=f"parameter update {library}/{cell}/{instance}",
                    ),
                    label=f"parameter update {library}/{cell}/{instance}",
                    mutation_target=(library, (cell,)),
                ),
                timeout=120,
            ),
        ),
    )
    if result.errors:
        raise RuntimeError(
            f"failed to update {library}/{cell}/{instance}: {result.errors[0]}"
        )
    return normalized
