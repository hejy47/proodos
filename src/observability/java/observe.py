from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any

from src.patching.java.api import _compile_and_run, _default_source_roots
from src.patching.java.classpath import missing_java_runtime_classpath_jars
from src.patching.java.gates import (
    check_observation_supported,
    find_target_method_descriptor,
)
from src.patching.java.models import InterventionRequest, InterventionStatus
from src.patching.java.runner import resolve_layout
from src.observability.java.source_override import (
    OBS_MARKER,
    OBS_MAX_CALLS_DEFAULT,
    insert_observation_prelude,
    read_java_source,
    validate_observation_expressions,
)


_OBS_LINE_RE = re.compile(
    rf"{re.escape(OBS_MARKER)}\s+(?P<method>\S+)\s+call=(?P<call>-?\d+)\s+(?P<key>\S+)=(?P<value>.*)$"
)


def run_observation(
    *,
    project_root: Path,
    test_class: str,
    test_method: str,
    target_class: str,
    target_method: str,
    expressions: list[str] | tuple[str, ...],
    parameter_types: tuple[str, ...] = (),
    max_calls: int = OBS_MAX_CALLS_DEFAULT,
) -> dict[str, Any]:
    """Compile a temp observation prelude for the target method and collect stderr samples."""
    project_root = Path(project_root)
    try:
        expressions = validate_observation_expressions(expressions)
        if type(max_calls) is not int or not 1 <= max_calls <= 100:
            raise ValueError("max_calls must be an integer between 1 and 100")
    except ValueError as exc:
        return {"status": "validation_error", "error": str(exc), "validation_error": str(exc),
                "samples": [], "call_count": 0, "truncated": False}
    missing = missing_java_runtime_classpath_jars()
    if missing:
        return {
            "status": "execution_error",
            "error": "Missing jars: " + ", ".join(str(path) for path in missing),
            "samples": [],
            "call_count": 0,
            "truncated": False,
        }

    layout = resolve_layout(project_root)
    source_roots = _default_source_roots(layout)
    target_descriptor = find_target_method_descriptor(
        source_roots,
        target_class,
        target_method,
        parameter_types,
    )
    if target_descriptor is None:
        return {
            "status": "validation_error",
            "error": "unable to resolve target method source for observation",
            "validation_error": "unable to resolve target method source for observation",
            "samples": [],
            "call_count": 0,
            "truncated": False,
        }

    gate = check_observation_supported(target_descriptor)
    if not gate.ok:
        return {
            "status": "validation_error",
            "error": gate.reason,
            "validation_error": gate.reason,
            "samples": [],
            "call_count": 0,
            "truncated": False,
        }

    request = InterventionRequest(
        test_class=test_class,
        test_method=test_method,
        target_class=target_class,
        target_method=target_method,
        return_type=target_descriptor.return_type or "void",
        parameter_types=parameter_types,
    )

    target_file = Path(target_descriptor.file_path)
    original = read_java_source(target_file)
    try:
        patched = insert_observation_prelude(
            original,
            target_descriptor,
            expressions,
            max_calls=max_calls,
        )
    except Exception as exc:
        return {
            "status": "execution_error",
            "error": str(exc),
            "samples": [],
            "call_count": 0,
            "truncated": False,
        }

    with tempfile.TemporaryDirectory(prefix="proodos-observe-") as tmp:
        tmp_root = Path(tmp)
        override_dir = tmp_root / "classes"
        temp_java = tmp_root / target_file.name
        temp_java.write_bytes(patched.encode("utf-8"))
        result = _compile_and_run(
            layout,
            request,
            compile_main_files=[temp_java],
            override_classes_dir=override_dir,
        )

    if result.status == InterventionStatus.ERROR and result.error_message:
        err = result.error_message or ""
        status = "execution_error"
        if "failed to compile" in err.lower() or "cannot find symbol" in (result.stderr or "").lower():
            status = "execution_error"
        return {
            "status": status,
            "error": err,
            "stderr": result.stderr,
            "stdout": result.stdout,
            "samples": [],
            "call_count": 0,
            "truncated": False,
            "generated_source_snippet": patched[
                max(0, target_descriptor.start_byte - 80) : target_descriptor.end_byte + 400
            ],
        }

    combined = "\n".join(
        part for part in (result.stdout or "", result.stderr or "") if part
    )
    samples, call_count, truncated = parse_observation_output(
        combined,
        max_calls=max_calls,
    )
    for sample in samples:
        sample["values"] = {
            expression: sample["values"][f"expr_{index}"]
            for index, expression in enumerate(expressions)
            if f"expr_{index}" in sample["values"]
        }
    test_passed = bool(result.intervention_result and result.intervention_result.passed)
    return {
        "status": "success",
        "error": None,
        "samples": samples,
        "call_count": call_count,
        "truncated": truncated,
        "call_count_is_lower_bound": truncated,
        "test_passed": test_passed,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "working_tree_unchanged": read_java_source(target_file) == original,
    }


def parse_observation_output(
    text: str,
    *,
    max_calls: int = OBS_MAX_CALLS_DEFAULT,
) -> tuple[list[dict[str, Any]], int, bool]:
    """Parse ``__PROODOS_OBS__`` lines into per-call sample dicts."""
    by_call: dict[int, dict[str, Any]] = {}
    for line in (text or "").splitlines():
        match = _OBS_LINE_RE.search(line.strip())
        if not match:
            continue
        call = int(match.group("call"))
        entry = by_call.setdefault(
            call,
            {"call": call, "method": match.group("method"), "values": {}},
        )
        entry["values"][match.group("key")] = match.group("value")

    ordered_calls = sorted(by_call)
    truncated = len(ordered_calls) > max_calls
    kept = ordered_calls[:max_calls]
    samples = [by_call[c] for c in kept]
    return samples, len(ordered_calls), truncated
