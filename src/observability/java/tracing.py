from __future__ import annotations

import csv
from pathlib import Path
import tempfile
from typing import Any, Iterable

from src.java_runtime.tracing import JavaTraceCollector
from src.java_runtime.test_runner_builder import build_test_runner
from src.java_runtime.trace_agent_builder import build_trace_agent
from src.models import TestCase
from src.utils.java_util import split_test_id


DEFAULT_MAX_EVENTS = 1000
MAX_EVENTS_LIMIT = 10000


def collect_java_trace(
    *,
    project: Any,
    test_id: str,
    method_ids: Iterable[str],
    max_events: int = DEFAULT_MAX_EVENTS,
) -> dict[str, Any]:
    """Run one Java test with the trace agent and return candidate method events.

    This is an on-demand runtime experiment. It does not read or write the
    preprocess dataset; temporary reports are discarded when the call returns.
    """
    requested = list(dict.fromkeys(str(method_id).strip() for method_id in method_ids))
    if not requested or any(not method_id for method_id in requested):
        return _error("validation_error", "method_ids must contain at least one non-empty method ID")
    if not isinstance(max_events, int) or max_events <= 0:
        return _error("validation_error", "max_events must be a positive integer")
    max_events = min(max_events, MAX_EVENTS_LIMIT)
    if project is None or getattr(project, "spec", None) is None:
        return _error("execution_error", "Java tracing requires a project with a project spec")

    try:
        test_class, test_method = split_test_id(str(test_id))
        if test_method == "*" or not test_class.strip() or not test_method.strip():
            return _error("validation_error", f"trace_functions requires a single test method: {test_id}")

        runner_build = build_test_runner()
        if not runner_build.success:
            detail = runner_build.stderr.strip() or runner_build.stdout.strip() or "test runner build failed"
            return _error("execution_error", f"Could not build Java test runner: {detail}")
        agent_build = build_trace_agent()
        if not agent_build.success:
            detail = agent_build.stderr.strip() or agent_build.stdout.strip() or "trace agent build failed"
            return _error("execution_error", f"Could not build Java trace agent: {detail}")

        testcase = TestCase(
            test_id=str(test_id),
            class_name=test_class,
            method_name=test_method,
            metadata={"framework": "JUNIT"},
        )
        class_prefixes = list(dict.fromkeys(method_id.split("#", 1)[0] for method_id in requested))

        with tempfile.TemporaryDirectory(prefix="proodos-java-trace-") as temp_dir:
            stage_dir = Path(temp_dir)
            trace_runner = JavaTraceCollector(project)
            spectra_path = stage_dir / "spectra.csv"
            tests_path = stage_dir / "tests.csv"
            trace_path = stage_dir / "trace.txt"
            coverage = trace_runner.collect_reports(
                selected_tests=[testcase],
                agent_jar_path=agent_build.agent_jar_path,
                include_prefixes=class_prefixes,
                stage_dir=stage_dir,
                spectra_path=spectra_path,
                tests_report_path=tests_path,
                trace_report_path=trace_path,
            )
            if coverage:
                error = next((record.execution_error for record in coverage if record.execution_error), "Java trace collection failed")
                return _error("execution_error", error, test_id=str(test_id), requested=requested)

            method_names = trace_runner._read_trace_spectra(spectra_path)
            test_ids_by_index = trace_runner._read_trace_test_ids_by_index(tests_path)
            matching_index = next(
                (
                    index
                    for index, candidate in test_ids_by_index.items()
                    if _normalize_test_id(candidate) == _normalize_test_id(str(test_id))
                ),
                None,
            )
            if matching_index is None:
                return _error(
                    "execution_error",
                    f"Java trace report did not contain the selected test {test_id}",
                    test_id=str(test_id),
                    requested=requested,
                )

            outcome = _read_outcome(tests_path, matching_index)
            raw_events = _read_call_events(trace_path, matching_index, method_names)
            return _summarize_events(
                test_id=str(test_id),
                outcome=outcome,
                requested=requested,
                raw_events=raw_events,
                max_events=max_events,
            )
    except Exception as exc:
        return _error("execution_error", f"{type(exc).__name__}: {exc}", test_id=str(test_id), requested=requested)


def _read_outcome(tests_path: Path, target_index: int) -> str | None:
    for row in _read_csv(tests_path):
        try:
            index = int(str(row.get("id", "")).strip())
        except ValueError:
            continue
        if index == target_index:
            return str(row.get("outcome", "")).strip().upper() or None
    return None


def _read_call_events(trace_path: Path, target_index: int, method_names: dict[int, str]) -> list[dict[str, str]]:
    if not trace_path.is_file():
        return []
    for line in trace_path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        try:
            index = int(parts[0].strip())
        except ValueError:
            continue
        if index != target_index:
            continue
        events: list[dict[str, str]] = []
        for payload in parts[2].split(","):
            payload = payload.strip()
            if len(payload) < 2 or payload[0] not in {"e", "x"}:
                continue
            try:
                method_number = int(payload[1:])
            except ValueError:
                continue
            method_id = method_names.get(method_number)
            if method_id:
                events.append({"event": "enter" if payload[0] == "e" else "exit", "method_id": method_id})
        return events
    return []


def _summarize_events(
    *,
    test_id: str,
    outcome: str | None,
    requested: list[str],
    raw_events: list[dict[str, str]],
    max_events: int,
) -> dict[str, Any]:
    requested_set = set(requested)
    matched_events = [event for event in raw_events if _matches_requested(event["method_id"], requested_set)]
    visible_events = matched_events[:max_events]
    counts: dict[str, int] = {}
    observed: list[str] = []
    for event in matched_events:
        if event["event"] != "enter":
            continue
        method_id = event["method_id"]
        canonical = _canonical_requested(method_id, requested)
        counts[canonical] = counts.get(canonical, 0) + 1
        if canonical not in observed:
            observed.append(canonical)
    observed_set = set(observed)
    return {
        "status": "success",
        "test_id": test_id,
        "test_outcome": outcome,
        "trace_source": "on-demand Java agent",
        "requested": requested,
        "observed": observed,
        "not_observed": [method_id for method_id in requested if method_id not in observed_set],
        "counts": counts,
        "events": visible_events,
        "event_count": len(matched_events),
        "truncated": len(matched_events) > max_events,
    }


def _matches_requested(observed_method: str, requested: set[str]) -> bool:
    if observed_method in requested:
        return True
    return any(_method_without_descriptor(observed_method) == _method_without_descriptor(item) for item in requested)


def _canonical_requested(observed_method: str, requested: list[str]) -> str:
    return next(
        (item for item in requested if item == observed_method or _method_without_descriptor(item) == _method_without_descriptor(observed_method)),
        observed_method,
    )


def _method_without_descriptor(method_id: str) -> str:
    class_name, separator, method = method_id.partition("#")
    if not separator:
        return method_id
    method_name = method.split("(", 1)[0]
    return f"{class_name}#{method_name}"


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _normalize_test_id(test_id: str) -> str:
    return test_id.strip().replace("::", "#")


def _error(status: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"status": status, "error": message, **extra}
