"""Public development fixtures for measuring actual, narrowly scoped code repair.

Responses are interpreted as a small numeric-expression subset of Python. No
model-generated code is passed to exec/eval or imported. This is neither a
general Python sandbox nor a realistic software-engineering benchmark.
"""
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import json
import math
import operator
from pathlib import Path
import random
from typing import Any

from .protocol import ArtifactStore, ProtocolError, canonical, parse_json


@dataclass(frozen=True)
class RepairTask:
    task_id: str
    parameters: tuple[str, ...]
    requirement: str
    buggy_source: str
    reference_source: str
    cases: tuple[tuple[tuple[int, ...], int | bool], ...]


TASKS = (
    RepairTask("clamp", ("x", "lower", "upper"),
        "Return integer x clamped to the inclusive interval [lower, upper]. Assume lower <= upper.",
        "def solve(x, lower, upper):\n    return min(lower, max(x, upper))\n",
        "def solve(x, lower, upper):\n    return min(upper, max(lower, x))\n",
        (((5, 0, 10), 5), ((-2, 0, 10), 0), ((15, 0, 10), 10),
         ((3, 3, 3), 3), ((-5, -9, -2), -5), ((0, -9, -2), -2))),
    RepairTask("leap_year", ("year",),
        "Return a bool: Gregorian leap years are divisible by 4 except centuries not divisible by 400.",
        "def solve(year):\n    return year % 4 == 0\n",
        "def solve(year):\n    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)\n",
        (((2000,), True), ((1900,), False), ((2024,), True), ((2023,), False),
         ((2100,), False), ((2400,), True))),
    RepairTask("ceil_div", ("n", "d"),
        "Return the integer ceiling of n / d. Inputs satisfy n >= 0 and d > 0.",
        "def solve(n, d):\n    return n // d\n",
        "def solve(n, d):\n    return (n + d - 1) // d\n",
        (((0, 7), 0), ((1, 7), 1), ((7, 7), 1), ((8, 7), 2),
         ((99, 10), 10), ((100, 10), 10))),
    RepairTask("inclusive_interval", ("value", "low", "high"),
        "Return a bool indicating whether value belongs to the inclusive interval [low, high].",
        "def solve(value, low, high):\n    return low < value < high\n",
        "def solve(value, low, high):\n    return low <= value <= high\n",
        (((0, 0, 5), True), ((5, 0, 5), True), ((3, 0, 5), True),
         ((6, 0, 5), False), ((-1, 0, 5), False), ((2, 2, 2), True))),
    RepairTask("inclusive_sum", ("n",),
        "Return the integer sum of all integers from 0 through n inclusive. Assume n >= 0.",
        "def solve(n):\n    return n * (n - 1) // 2\n",
        "def solve(n):\n    return n * (n + 1) // 2\n",
        (((0,), 0), ((1,), 1), ((2,), 3), ((5,), 15), ((100,), 5050))),
    RepairTask("distance", ("a", "b"),
        "Return the nonnegative integer distance between a and b.",
        "def solve(a, b):\n    return b - a\n",
        "def solve(a, b):\n    return abs(b - a)\n",
        (((5, 1), 4), ((1, 5), 4), ((-3, -8), 5), ((0, 0), 0), ((-5, 5), 10))),
)


class UnsupportedProgram(ValueError):
    pass


_BINARY = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
           ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod}
_COMPARE = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
            ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg, ast.Not: operator.not_}
_FUNCTIONS = {"abs": abs, "min": min, "max": max}


def _bounded(value: Any) -> int | float | bool:
    if type(value) not in (int, float, bool):
        raise UnsupportedProgram("Only scalar numeric and boolean results are supported")
    if not math.isfinite(value) or abs(value) > 10**12:
        raise UnsupportedProgram("Numeric value exceeds the evaluator's finite bounds")
    return value


def _validate_expression(node: ast.AST, names: set[str], depth: int = 0) -> None:
    if depth > 32:
        raise UnsupportedProgram("Expression is too deeply nested")
    children: list[ast.AST]
    if isinstance(node, ast.Constant):
        _bounded(node.value)
        return
    if isinstance(node, ast.Name) and node.id in names:
        return
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        children = [node.left, node.right]
    elif isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        children = [node.operand]
    elif isinstance(node, ast.BoolOp) and type(node.op) in (ast.And, ast.Or):
        children = list(node.values)
    elif isinstance(node, ast.Compare) and all(type(op) in _COMPARE for op in node.ops):
        children = [node.left, *node.comparators]
    elif isinstance(node, ast.IfExp):
        children = [node.test, node.body, node.orelse]
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCTIONS:
        if node.keywords or not 1 <= len(node.args) <= 8:
            raise UnsupportedProgram("Only 1-8 positional numeric call arguments are supported")
        if node.func.id == "abs" and len(node.args) != 1:
            raise UnsupportedProgram("abs requires one argument")
        if node.func.id in ("min", "max") and len(node.args) < 2:
            raise UnsupportedProgram("min/max require at least two scalar arguments")
        children = list(node.args)
    else:
        raise UnsupportedProgram(f"Unsupported expression: {type(node).__name__}")
    for child in children:
        _validate_expression(child, names, depth + 1)


def parse_program(source: str, parameters: tuple[str, ...]) -> ast.expr:
    if type(source) is not str or len(source.encode("utf-8")) > 8000:
        raise UnsupportedProgram("Expected source text of at most 8000 UTF-8 bytes")
    try:
        tree = ast.parse(source)
    except (SyntaxError, RecursionError) as exc:
        raise UnsupportedProgram("Invalid Python syntax") from exc
    if len(list(ast.walk(tree))) > 160 or len(tree.body) != 1:
        raise UnsupportedProgram("Expected one small function definition")
    fn = tree.body[0]
    if not isinstance(fn, ast.FunctionDef) or fn.name != "solve":
        raise UnsupportedProgram("Expected a function named solve")
    if (fn.decorator_list or fn.returns or getattr(fn, "type_params", [])
        or fn.args.posonlyargs or fn.args.kwonlyargs or fn.args.vararg
        or fn.args.kwarg or fn.args.defaults or fn.args.kw_defaults
        or tuple(arg.arg for arg in fn.args.args) != parameters
        or any(arg.annotation for arg in fn.args.args)
        or len(fn.body) != 1 or not isinstance(fn.body[0], ast.Return)
        or fn.body[0].value is None):
        raise UnsupportedProgram("Preserve the exact signature and use one return expression")
    expression = fn.body[0].value
    _validate_expression(expression, set(parameters))
    return expression


def _interpret(node: ast.AST, values: dict[str, int]) -> int | float | bool:
    if isinstance(node, ast.Constant):
        return _bounded(node.value)
    if isinstance(node, ast.Name):
        return _bounded(values[node.id])
    if isinstance(node, ast.BinOp):
        return _bounded(_BINARY[type(node.op)](_interpret(node.left, values), _interpret(node.right, values)))
    if isinstance(node, ast.UnaryOp):
        return _bounded(_UNARY[type(node.op)](_interpret(node.operand, values)))
    if isinstance(node, ast.BoolOp):
        result: int | float | bool = False
        for child in node.values:
            result = _interpret(child, values)
            if (isinstance(node.op, ast.And) and not result) or (isinstance(node.op, ast.Or) and result):
                break
        return result
    if isinstance(node, ast.Compare):
        left = _interpret(node.left, values)
        for op, right_node in zip(node.ops, node.comparators):
            right = _interpret(right_node, values)
            if not _COMPARE[type(op)](left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.IfExp):
        return _interpret(node.body if _interpret(node.test, values) else node.orelse, values)
    if isinstance(node, ast.Call):
        return _bounded(_FUNCTIONS[node.func.id](*(_interpret(arg, values) for arg in node.args)))
    raise UnsupportedProgram("Unexpected validated expression")


def assess_source(task: RepairTask, source: str) -> dict:
    try:
        expression = parse_program(source, task.parameters)
    except (UnsupportedProgram, UnicodeError, ValueError, OverflowError) as exc:
        return {"task": task.task_id, "success": False, "valid_program": False,
                "passed": 0, "total": len(task.cases), "error": str(exc)}
    passed = 0
    failures = []
    for args, expected in task.cases:
        try:
            actual = _interpret(expression, dict(zip(task.parameters, args)))
            correct = type(actual) is type(expected) and actual == expected
            if not correct:
                failures.append({"arguments": args, "expected": expected, "actual": actual})
        except (ArithmeticError, ValueError, TypeError) as exc:
            correct = False
            failures.append({"arguments": args, "error": type(exc).__name__})
        passed += int(correct)
    return {"task": task.task_id, "success": passed == len(task.cases),
            "valid_program": True, "passed": passed, "total": len(task.cases), "failures": failures}


def self_check() -> dict:
    records = [{"task": task.task_id,
                "buggy": assess_source(task, task.buggy_source),
                "reference": assess_source(task, task.reference_source)} for task in TASKS]
    return {"kind": "repair_evaluator_self_check", "model_called": False,
            "scope": "Six public synthetic numeric-function fixtures; no model performance measured",
            "success": all(not record["buggy"]["success"] and record["reference"]["success"]
                           for record in records), "records": records}


def repair_prompt(task: RepairTask, language: str, storage: str) -> tuple[str, str]:
    if language not in ("english", "structured") or storage not in ("inline", "shared"):
        raise ValueError("Unknown experimental condition")
    system = (
        "Repair the supplied numeric Python function. Return only a JSON object with one key, "
        "source, containing the complete repaired function. Preserve the exact function name and "
        "parameter names. Use a single return expression. Supported syntax: numeric/boolean "
        "constants, parameters, + - * // %, comparisons, and/or/not, conditional expressions, "
        "and abs/min/max with numeric arguments. No imports, loops, assignments, decorators, "
        "attributes or other calls. Use no more than 160 AST nodes. Acceptance cases are not "
        "provided in this prompt. Referenced artifacts are resolved below in the same request."
    )
    artifact = {"source": task.buggy_source}
    ref = ArtifactStore().add("function/" + task.task_id, artifact)
    resolved = ""
    if storage == "shared":
        resolved = "Resolved shared artifact (included in this request):\n" + canonical(
            {"ref": ref.to_dict(), "value": artifact}) + "\nHandoff:\n"
        content = {"ref": ref.to_dict()}
    else:
        content = {"value": artifact}
    if language == "structured":
        handoff = canonical({"task": task.task_id, "operation": "repair",
                             "requirement": task.requirement, "artifact": content})
    else:
        handoff = f"Repair task {task.task_id}. Requirement: {task.requirement}\n"
        handoff += ("Use artifact " + canonical(ref.to_dict()) if storage == "shared"
                    else "Current source:\n" + task.buggy_source)
    return system, resolved + handoff


def assess_response(task: RepairTask, content: str | None) -> dict:
    try:
        value = parse_json(content) if isinstance(content, str) else None
    except ProtocolError:
        value = None
    if type(value) is not dict or set(value) != {"source"} or type(value["source"]) is not str:
        return {"task": task.task_id, "success": False, "valid_program": False,
                "passed": 0, "total": len(task.cases), "error": "Expected JSON with one source string"}
    return assess_source(task, value["source"])


def run_repair_benchmark(config: Any, endpoint_caller: Any = None) -> dict:
    """Opt-in live calls; local/mock callers produce labeled infrastructure results."""
    from .model_benchmark import call_endpoint, summarize
    config.validate()
    caller = endpoint_caller or call_endpoint
    rng = random.Random(config.seed)
    records = []
    conditions = [(language, storage) for language in ("english", "structured")
                  for storage in ("inline", "shared")]
    for repetition in range(config.repetitions):
        for task in TASKS:
            order = conditions.copy()
            rng.shuffle(order)
            for language, storage in order:
                system, user = repair_prompt(task, language, storage)
                record = caller(config, system, user)
                assessment = assess_response(task, record.get("content"))
                correct = record.get("error") is None and assessment["success"]
                record.update({"fixture": task.task_id, "condition": f"{language}_{storage}",
                               "repetition": repetition, "request_order": len(records),
                               "correct": correct, "assessment": assessment,
                               "answer_error": None if correct else "repair_failed"})
                records.append(record)
    return {"schema_version": 1, "experiment": "numeric_function_repair",
            "execution": "custom_test_caller" if endpoint_caller else "configured_endpoint",
            "config": config.metadata(), "requests_attempted": len(records),
            "limitations": [
                "Six public development fixtures; these are not a realistic held-out software benchmark.",
                "The model must generate a repair; reference solutions and acceptance cases are withheld from prompts.",
                "The investigator is a fixed fixture, not another live model; this is a receiver-only assay.",
                "Only a bounded numeric-expression subset of Python is interpreted; arbitrary code is not executed.",
                "Shared artifacts are included in the actual request; they do not provide free model context.",
                "Each request contains one handoff; the shared condition tests a reference wrapper, not cross-turn cache savings.",
                "No retries; invalid responses and transport failures count as failures.",
                "Reported token usage is measured by the endpoint when available; no byte-to-token estimates.",
                "Server caching is uncontrolled; compare repeated runs and task-level outcomes before drawing conclusions."],
            "summary": summarize(records), "records": records}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--endpoint", help="Complete Chat Completions-style URL; specifying it makes live calls")
    parser.add_argument("--model")
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--seed", type=int, default=19)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--extra-body", default="{}")
    parser.add_argument("--allow-local-http", action="store_true")
    args = parser.parse_args()
    if args.endpoint:
        if not args.model:
            parser.error("--model is required with --endpoint")
        from .model_benchmark import EndpointConfig
        config = EndpointConfig(endpoint=args.endpoint, model=args.model,
                                api_key_env=args.api_key_env, timeout=args.timeout,
                                repetitions=args.repetitions, seed=args.seed,
                                extra_body=parse_json(args.extra_body),
                                allow_local_http=args.allow_local_http)
        result = run_repair_benchmark(config)
    else:
        result = self_check()
    payload = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload)
    if not args.endpoint and not result["success"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
