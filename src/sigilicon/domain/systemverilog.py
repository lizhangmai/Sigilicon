"""Small dependency-free readers for public SystemVerilog module contracts."""

from __future__ import annotations

import ast
from dataclasses import dataclass
import operator
import re
from typing import Mapping


@dataclass(frozen=True)
class ModulePort:
    """One elaborated ANSI port in declaration order."""

    direction: str
    width: int


def _matching(text: str, start: int, opening: str, closing: str) -> int:
    if start >= len(text) or text[start] != opening:
        raise ValueError(f"expected {opening!r} at offset {start}")
    depth = 0
    for index in range(start, len(text)):
        if text[index] == opening:
            depth += 1
        elif text[index] == closing:
            depth -= 1
            if depth == 0:
                return index
    raise ValueError(f"unterminated {opening}{closing} group")


def _split_commas(text: str) -> tuple[str, ...]:
    values: list[str] = []
    start = 0
    depths = {"(": 0, "[": 0, "{": 0}
    pairs = {")": "(", "]": "[", "}": "{"}
    for index, character in enumerate(text):
        if character in depths:
            depths[character] += 1
        elif character in pairs:
            opening = pairs[character]
            depths[opening] -= 1
            if depths[opening] < 0:
                raise ValueError("unbalanced SystemVerilog declaration")
        elif character == "," and not any(depths.values()):
            values.append(text[start:index].strip())
            start = index + 1
    if any(depths.values()):
        raise ValueError("unbalanced SystemVerilog declaration")
    values.append(text[start:].strip())
    return tuple(value for value in values if value)


def _module_header(source: str, module: str) -> tuple[str, str, str]:
    clean = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    clean = re.sub(r"//[^\n]*", "", clean)
    marker = re.search(rf"\bmodule\s+{re.escape(module)}\b", clean)
    if marker is None:
        raise ValueError(f"SystemVerilog module not found: {module}")
    cursor = marker.end()
    while cursor < len(clean) and clean[cursor].isspace():
        cursor += 1
    parameters = ""
    if cursor < len(clean) and clean[cursor] == "#":
        cursor += 1
        while cursor < len(clean) and clean[cursor].isspace():
            cursor += 1
        stop = _matching(clean, cursor, "(", ")")
        parameters = clean[cursor + 1 : stop]
        cursor = stop + 1
    while cursor < len(clean) and clean[cursor].isspace():
        cursor += 1
    stop = _matching(clean, cursor, "(", ")")
    terminator = re.search(r"\bendmodule\b", clean[stop + 1 :])
    if terminator is None:
        raise ValueError(f"unterminated SystemVerilog module: {module}")
    body_end = stop + 1 + terminator.start()
    return parameters, clean[cursor + 1 : stop], clean[stop + 1 : body_end]


def _integer_expression(expression: str, parameters: Mapping[str, int]) -> int:
    value = expression.strip()
    while match := re.search(r"\$clog2\s*\(", value):
        opening = value.find("(", match.start())
        closing = _matching(value, opening, "(", ")")
        operand_text = value[opening + 1 : closing]
        operand = _integer_expression(operand_text, parameters)
        if operand <= 0:
            raise ValueError(f"cannot elaborate $clog2 operand: {operand_text}")
        replacement = str((operand - 1).bit_length())
        value = value[: match.start()] + replacement + value[closing + 1 :]

    while len(value) >= 2 and value[0] == "(" and _matching(
        value, 0, "(", ")"
    ) == len(value) - 1:
        value = value[1:-1].strip()

    depths = {"(": 0, "[": 0, "{": 0}
    pairs = {")": "(",
        "]": "[",
        "}": "{",
    }
    question = None
    for index, character in enumerate(value):
        if character in depths:
            depths[character] += 1
        elif character in pairs:
            depths[pairs[character]] -= 1
        elif character == "?" and not any(depths.values()):
            question = index
            break
    if question is not None:
        depths = {"(": 0, "[": 0, "{": 0}
        nested_questions = 0
        colon = None
        for index in range(question + 1, len(value)):
            character = value[index]
            if character in depths:
                depths[character] += 1
            elif character in pairs:
                depths[pairs[character]] -= 1
            elif not any(depths.values()):
                if character == "?":
                    nested_questions += 1
                elif character == ":":
                    if nested_questions:
                        nested_questions -= 1
                    else:
                        colon = index
                        break
        if colon is None:
            raise ValueError(f"unterminated SystemVerilog ternary expression: {expression}")
        condition = _integer_expression(value[:question], parameters)
        branch = value[question + 1 : colon] if condition else value[colon + 1 :]
        return _integer_expression(branch, parameters)

    for name, number in sorted(parameters.items(), key=lambda item: -len(item[0])):
        value = re.sub(rf"\b{re.escape(name)}\b", str(number), value)
    try:
        tree = ast.parse(value, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"unsupported SystemVerilog integer expression: {expression}") from exc
    binary = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
              ast.LShift: operator.lshift, ast.RShift: operator.rshift,
              ast.BitAnd: operator.and_, ast.BitOr: operator.or_, ast.BitXor: operator.xor}
    comparisons = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt,
                   ast.GtE: operator.ge, ast.Eq: operator.eq, ast.NotEq: operator.ne}
    unary = {ast.UAdd: operator.pos, ast.USub: operator.neg, ast.Invert: operator.invert}

    def evaluate(node: ast.AST) -> int:
        if isinstance(node, ast.Constant) and type(node.value) is int:
            return node.value
        if isinstance(node, ast.UnaryOp) and type(node.op) in unary:
            return unary[type(node.op)](evaluate(node.operand))
        if isinstance(node, ast.BinOp):
            left, right = evaluate(node.left), evaluate(node.right)
            if isinstance(node.op, (ast.Div, ast.Mod)):
                if right == 0:
                    raise ValueError("division by zero in SystemVerilog integer expression")
                quotient = (abs(left) // abs(right)) * (-1 if (left < 0) != (right < 0) else 1)
                return quotient if isinstance(node.op, ast.Div) else left - quotient * right
            if type(node.op) in binary:
                return binary[type(node.op)](left, right)
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in comparisons:
            return int(comparisons[type(node.ops[0])](evaluate(node.left), evaluate(node.comparators[0])))
        if isinstance(node, ast.BoolOp):
            values = (evaluate(item) != 0 for item in node.values)
            return int(all(values) if isinstance(node.op, ast.And) else any(values))
        raise ValueError(f"unsupported SystemVerilog integer expression: {expression}")

    return evaluate(tree.body)


def _parameters(source: str, module: str) -> tuple[dict[str, int], str, str]:
    declarations, ports, body = _module_header(source, module)
    values: dict[str, int] = {}
    for declaration in _split_commas(declarations):
        item = re.sub(r"^\s*parameter\b", "", declaration).strip()
        assignment = re.search(r"(?<![<>=!])=(?!=)", item)
        if assignment is None:
            raise ValueError(f"unsupported SystemVerilog parameter: {module}.{item}")
        left = item[: assignment.start()]
        expression = item[assignment.end() :]
        names = re.findall(r"[A-Za-z_][A-Za-z0-9_$]*", left)
        if not names:
            raise ValueError(f"unsupported SystemVerilog parameter: {module}.{item}")
        values[names[-1]] = _integer_expression(expression, values)
    return values, ports, body


def module_port_signatures(source: str, module: str) -> dict[str, ModulePort]:
    """Return elaborated ANSI port direction and width in declaration order."""

    parameters, header, _body = _parameters(source, module)
    result: dict[str, ModulePort] = {}
    direction: str | None = None
    width = 1
    for declaration in _split_commas(header):
        item = declaration.strip()
        match = re.match(r"^(input|output|inout)\b(.*)$", item, re.DOTALL)
        if match is not None:
            direction = match.group(1)
            item = match.group(2).strip()
            while True:
                type_match = re.match(r"^(?:wire|logic|reg|var|signed|unsigned)\b\s*", item)
                if type_match is None:
                    break
                item = item[type_match.end() :].strip()
            width = 1
            dimension = re.match(r"^\[([^]]+)\]\s*(.*)$", item, re.DOTALL)
            if dimension is not None:
                bounds = dimension.group(1).split(":")
                if len(bounds) != 2:
                    raise ValueError(f"unsupported packed dimension: {module}.{item}")
                left = _integer_expression(bounds[0], parameters)
                right = _integer_expression(bounds[1], parameters)
                width = abs(left - right) + 1
                item = dimension.group(2).strip()
        if direction is None:
            raise ValueError(f"port declaration has no direction: {module}.{item}")
        name_match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_$]*)", item)
        if name_match is None:
            raise ValueError(f"unsupported SystemVerilog port declaration: {module}.{item}")
        name = name_match.group(1)
        if name in result:
            raise ValueError(f"duplicate SystemVerilog port: {module}.{name}")
        result[name] = ModulePort(direction=direction, width=width)
    if not result:
        raise ValueError(f"no ANSI ports parsed from SystemVerilog module: {module}")
    return result


def module_ports(source: str, module: str) -> dict[str, str]:
    """Return ANSI port names and directions in declaration order."""

    return {
        name: signature.direction
        for name, signature in module_port_signatures(source, module).items()
    }


def named_port_connections(
    source: str, parent_module: str, instantiated_module: str
) -> dict[str, str]:
    """Return one instance's explicit named connections in source order."""

    _parameters_raw, _ports, body = _module_header(source, parent_module)
    matches = list(
        re.finditer(
            rf"\b{re.escape(instantiated_module)}\b\s+"
            rf"[A-Za-z_][A-Za-z0-9_$]*\s*\(",
            body,
        )
    )
    if len(matches) != 1:
        raise ValueError(
            f"{parent_module} must instantiate exactly one {instantiated_module}"
        )
    opening = body.find("(", matches[0].start())
    closing = _matching(body, opening, "(", ")")
    connection_text = body[opening + 1 : closing]
    connections: dict[str, str] = {}
    cursor = 0
    while cursor < len(connection_text):
        prefix = re.match(r"[\s,]*", connection_text[cursor:])
        assert prefix is not None
        cursor += prefix.end()
        if cursor == len(connection_text):
            break
        pin = re.match(r"\.([A-Za-z_][A-Za-z0-9_$]*)\s*\(", connection_text[cursor:])
        if pin is None:
            raise ValueError(
                f"{parent_module}.{instantiated_module} must use explicit named ports"
            )
        name = pin.group(1)
        opening = cursor + pin.end() - 1
        closing = _matching(connection_text, opening, "(", ")")
        expression = connection_text[opening + 1 : closing].strip()
        if not expression:
            raise ValueError(f"unconnected named port: {instantiated_module}.{name}")
        if name in connections:
            raise ValueError(f"duplicate named port: {instantiated_module}.{name}")
        connections[name] = re.sub(r"\s+", "", expression)
        cursor = closing + 1
    if not connections:
        raise ValueError(f"no named connections found for {instantiated_module}")
    return connections
