from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.patching.java.api import run_intervention
from src.patching.java.models import InterventionRequest, InterventionStatus


def parse_method_id(method_id: str) -> tuple[str, str, str | None]:
    if "#" not in method_id:
        raise ValueError(f"Invalid method_id (missing #): {method_id}")
    class_name, rest = method_id.split("#", 1)
    paren = rest.find("(")
    if paren < 0:
        return class_name, rest, parse_jvm_return_type(method_id)
    method_name = rest[:paren]
    return class_name, method_name, parse_jvm_return_type(method_id)


def parse_test_id(test_id: str) -> tuple[str, str]:
    if "::" in test_id:
        test_class, test_method = test_id.split("::", 1)
        return test_class, test_method
    if "#" in test_id:
        test_class, test_method = test_id.split("#", 1)
        return test_class, test_method
    raise ValueError(f"Invalid test_id: {test_id}")


def jvm_return_type_to_java(return_type: str | None) -> str:
    if return_type is None:
        return "java.lang.Object"
    mapping = {
        "Z": "boolean",
        "B": "byte",
        "C": "char",
        "S": "short",
        "I": "int",
        "J": "long",
        "F": "float",
        "D": "double",
        "V": "void",
        "Ljava/lang/String;": "java.lang.String",
    }
    if return_type in mapping:
        return mapping[return_type]
    if return_type.startswith("L") and return_type.endswith(";"):
        return return_type[1:-1].replace("/", ".")
    return return_type


def parse_jvm_parameter_types(method_id: str) -> tuple[str, ...]:
    """Parse Java parameter type names from a method_id JVM descriptor."""
    if "#" not in method_id:
        return ()
    descriptor = method_id.split("#", 1)[1]
    paren_start = descriptor.find("(")
    paren_end = descriptor.find(")", paren_start + 1)
    if paren_start < 0 or paren_end < 0:
        return ()
    inner = descriptor[paren_start + 1 : paren_end]
    if not inner:
        return ()
    types: list[str] = []
    index = 0
    while index < len(inner):
        ch = inner[index]
        if ch == "L":
            end = inner.find(";", index)
            if end < 0:
                break
            types.append(jvm_return_type_to_java(inner[index : end + 1]))
            index = end + 1
            continue
        if ch == "[":
            # Keep array types as JVM form converted loosely to Java.
            start = index
            while index < len(inner) and inner[index] == "[":
                index += 1
            if index >= len(inner):
                break
            if inner[index] == "L":
                end = inner.find(";", index)
                if end < 0:
                    break
                base = jvm_return_type_to_java(inner[index : end + 1])
                dims = index - start
                types.append(base + "[]" * dims)
                index = end + 1
            else:
                base = jvm_return_type_to_java(inner[index])
                dims = index - start
                types.append(base + "[]" * dims)
                index += 1
            continue
        types.append(jvm_return_type_to_java(ch))
        index += 1
    return tuple(types)


def apply_intervention(
    *,
    project_root: Path,
    test_id: str,
    method_id: str,
    replacement_function: str,
) -> dict[str, Any]:
    """Compile a complete replacement method and re-run the failing test.

    Production and test source files in the working tree are never modified.
    """
    try:
        target_class, target_method, jvm_ret = parse_method_id(method_id)
        test_class, test_method = parse_test_id(test_id)
        return_type = jvm_return_type_to_java(jvm_ret)
        parameter_types = parse_jvm_parameter_types(method_id)
    except ValueError as exc:
        return {
            "status": "validation_error",
            "method_id": method_id,
            "test_id": test_id,
            "replacement_function": replacement_function,
            "validation_error": str(exc),
            "error": str(exc),
            "test_passed": False,
            "outcome": None,
        }

    if not replacement_function or not replacement_function.strip():
        return {
            "status": "validation_error",
            "method_id": method_id,
            "test_id": test_id,
            "replacement_function": replacement_function,
            "validation_error": "replacement_function is required (complete Java method definition)",
            "error": "replacement_function is required",
            "test_passed": False,
            "outcome": None,
        }

    body_err = _validate_replacement_budget(replacement_function)
    if body_err:
        return {
            "status": "validation_error",
            "method_id": method_id,
            "test_id": test_id,
            "replacement_function": replacement_function,
            "validation_error": body_err,
            "error": body_err,
            "test_passed": False,
            "outcome": None,
        }

    result = run_intervention(
        InterventionRequest(
            test_class=test_class,
            test_method=test_method,
            target_class=target_class,
            target_method=target_method,
            replacement_function=replacement_function,
            return_type=return_type,
            parameter_types=parameter_types,
        ),
        project_root=Path(project_root),
    )

    generated = result.generated_source_code

    if result.status == InterventionStatus.UNSUPPORTED:
        return {
            "status": "validation_error",
            "method_id": method_id,
            "test_id": test_id,
            "replacement_function": replacement_function,
            "return_type": return_type,
            "validation_error": result.error_message,
            "error": result.error_message,
            "test_passed": False,
            "outcome": None,
            "generated_source_code": generated,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "original_passed": bool(
                result.original_result and result.original_result.passed
            ),
        }

    if result.status == InterventionStatus.ERROR:
        return {
            "status": "execution_error",
            "method_id": method_id,
            "test_id": test_id,
            "replacement_function": replacement_function,
            "return_type": return_type,
            "error": result.error_message,
            "test_passed": False,
            "outcome": None,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "generated_source_code": generated,
            "original_passed": bool(
                result.original_result and result.original_result.passed
            ),
        }

    passed = bool(result.intervention_result and result.intervention_result.passed)
    return {
        "status": "success",
        "method_id": method_id,
        "test_id": test_id,
        "replacement_function": replacement_function,
        "return_type": return_type,
        "validation_error": None,
        "test_passed": passed,
        "outcome": "crash_disappeared" if passed else "still_failing",
        "error": None,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "generated_source_code": generated,
        "original_passed": bool(result.original_result and result.original_result.passed),
    }


MAX_REPLACEMENT_CHARS = 50000
MAX_REPLACEMENT_LINES = 1000


def _validate_replacement_budget(replacement_function: str) -> str | None:
    text = replacement_function.strip()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(text) > MAX_REPLACEMENT_CHARS:
        return (
            f"replacement_function too long ({len(text)} chars > {MAX_REPLACEMENT_CHARS}); "
            "provide one complete method definition"
        )
    if len(lines) > MAX_REPLACEMENT_LINES:
        return (
            f"replacement_function too long ({len(lines)} lines > {MAX_REPLACEMENT_LINES}); "
            "provide one complete method definition"
        )
    return None


def parse_jvm_return_type(method_id: str) -> str | None:
    hash_index = method_id.find("#")
    if hash_index == -1:
        return None

    descriptor = method_id[hash_index + 1 :]
    paren_start = descriptor.find("(")
    if paren_start == -1:
        return None

    depth = 0
    for index in range(paren_start, len(descriptor)):
        char = descriptor[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return_type = descriptor[index + 1 :]
                return return_type or None
    return None
