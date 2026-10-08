from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.patching.java.bridge import (
    parse_jvm_parameter_types,
    parse_method_id,
    parse_test_id,
)

def apply_observation(
    *,
    project_root: Path,
    test_id: str,
    method_id: str,
    probe_spec: str | dict[str, Any],
) -> dict[str, Any]:
    """Observe explicitly selected entry expressions in the selected test."""
    from src.observability.java.observe import run_observation

    try:
        options = parse_java_probe_spec(probe_spec)
        target_class, target_method, _jvm_ret = parse_method_id(method_id)
        test_class, test_method = parse_test_id(test_id)
        parameter_types = parse_jvm_parameter_types(method_id)
    except ValueError as exc:
        return {
            "status": "validation_error",
            "method_id": method_id,
            "test_id": test_id,
            "probe_spec": probe_spec,
            "validation_error": str(exc),
            "error": str(exc),
            "samples": [],
            "call_count": 0,
            "truncated": False,
        }

    result = run_observation(
        project_root=Path(project_root),
        test_class=test_class,
        test_method=test_method,
        target_class=target_class,
        target_method=target_method,
        expressions=options["expressions"],
        parameter_types=parameter_types,
        max_calls=options["max_calls"],
    )
    result["method_id"] = method_id
    result["test_id"] = test_id
    result["expressions"] = options["expressions"]
    result["probe_spec"] = options
    result["probe_type"] = options["type"]
    return result


def parse_java_probe_spec(spec: str | dict[str, Any]) -> dict[str, Any]:
    from src.observability.java.source_override import OBS_MAX_CALLS_DEFAULT, validate_observation_expressions

    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except (TypeError, ValueError) as exc:
            raise ValueError('Java probe_spec must be a JSON object, e.g. {"type":"entry","expressions":["this.field"]}') from exc
    if not isinstance(spec, dict):
        raise ValueError("Java probe_spec must be a JSON object")
    unknown = set(spec) - {"type", "expressions", "max_calls"}
    if unknown:
        raise ValueError("unsupported Java probe_spec fields: " + ", ".join(sorted(unknown)))
    if spec.get("type", "entry") != "entry":
        raise ValueError("Java probe currently supports only type=entry; return and line probes are not implemented")
    expressions = validate_observation_expressions(spec.get("expressions"))
    max_calls = spec.get("max_calls", OBS_MAX_CALLS_DEFAULT)
    if type(max_calls) is not int or not 1 <= max_calls <= 100:
        raise ValueError("max_calls must be an integer between 1 and 100")
    return {"type": "entry", "expressions": expressions, "max_calls": max_calls}
