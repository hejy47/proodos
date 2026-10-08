"""Bounded Java runtime evidence used during debugging."""

from __future__ import annotations

import json
from typing import Any

from src.observability.java.bridge import apply_observation
from src.observability.java.tracing import collect_java_trace
from src.debug.semantic_agent.tools.tool_response import format_observation_result


MAX_OBSERVE_ATTEMPTS = 2
MAX_TOTAL_RUNTIME_CALLS = 6


class RuntimeExperiments:
    """Case-scoped Java observation and tracing budgets."""

    def __init__(self, project, preprocess_data, test_id):
        self.project = project
        self.project_root = project.spec.project_path if project is not None else None
        self.preprocess_data = preprocess_data
        self.test_id = test_id
        self._observe_attempts: dict[str, int] = {}
        self._total_runtime_calls = 0
        self._last_observe_results: dict[str, dict[str, Any]] = {}
        self.history: list[dict[str, Any]] = []
        self._cache: dict[str, Any] = {}

    @property
    def can_run_runtime_experiment(self) -> bool:
        return self._total_runtime_calls < MAX_TOTAL_RUNTIME_CALLS

    @property
    def runtime_unavailable_reason(self) -> str | None:
        if self._total_runtime_calls >= MAX_TOTAL_RUNTIME_CALLS:
            return (
                f"Case runtime experiment budget ({MAX_TOTAL_RUNTIME_CALLS} calls) is "
                "exhausted; continue with collected and static evidence."
            )
        return None

    def _blocked_runtime_result(self, method_id: str | None = None) -> str:
        reason = self.runtime_unavailable_reason or "Runtime experiments are unavailable."
        method = f"\nmethod_id: {method_id}" if method_id else ""
        return f"status: execution_error\nerror: {reason}{method}"

    def _backend_language(self) -> str:
        metadata = getattr(self.preprocess_data, "metadata", None) or {}
        return str(metadata.get("language") or "java").strip().lower()

    def _unsupported_backend(self) -> str | None:
        language = self._backend_language()
        if language in {"", "java"}:
            return None
        return (
            f"Observation/intervention is not supported for this {language} project "
            "(the available runtime backend supports Java only). Do not call "
            "probe_function/trace_functions for this case — classify from source "
            "and propagation evidence instead."
        )

    def _probe_function(
        self,
        method_id: str,
        probe_spec: str | dict[str, Any] = "",
    ) -> str:
        attempts = self._observe_attempts.get(method_id, 0)
        if not self.can_run_runtime_experiment:
            return self._blocked_runtime_result(method_id)
        if self.project is None or self.project_root is None:
            return format_observation_result(
                {
                    "status": "execution_error",
                    "error": "Observation requires a project (with project_path).",
                    "method_id": method_id,
                    "probe_spec": probe_spec,
                    "attempts": attempts,
                    "samples": [],
                    "call_count": 0,
                    "truncated": False,
                }
            )
        unsupported = self._unsupported_backend()
        if unsupported:
            return format_observation_result(
                {
                    "status": "unsupported",
                    "error": unsupported,
                    "method_id": method_id,
                    "probe_spec": probe_spec,
                    "attempts": attempts,
                    "samples": [],
                    "call_count": 0,
                    "truncated": False,
                }
            )
        if attempts >= MAX_OBSERVE_ATTEMPTS:
            last = self._last_observe_results.get(method_id) or {}
            return format_observation_result(
                {
                    "status": "execution_error",
                    "error": (
                        f"Max observe attempts ({MAX_OBSERVE_ATTEMPTS}) reached for "
                        f"{method_id}; stop retrying and use the last result."
                    ),
                    "method_id": method_id,
                    "probe_spec": probe_spec,
                    "attempts": attempts,
                    "samples": last.get("samples") or [],
                    "call_count": last.get("call_count") or 0,
                    "truncated": bool(last.get("truncated")),
                    "stderr": last.get("stderr"),
                }
            )

        attempts += 1
        self._observe_attempts[method_id] = attempts
        self._total_runtime_calls += 1
        result = apply_observation(
            project_root=self.project_root,
            test_id=self.test_id,
            method_id=method_id,
            probe_spec=probe_spec,
        )
        result["attempts"] = attempts
        if result.get("status") in {"execution_error", "validation_error", "unsupported"}:
            self._observe_attempts[method_id] = MAX_OBSERVE_ATTEMPTS
        self._last_observe_results[method_id] = result
        return format_observation_result(result)

    def _run(self, name: str, args: Any, operation) -> Any:
        key = json.dumps([name, args], sort_keys=True, default=str)
        if key in self._cache:
            return self._cache[key]
        try:
            result = operation()
        except Exception as exc:
            result = f"status: execution_error\nerror: {type(exc).__name__}: {exc}"
        self._cache[key] = result
        self.history.append({"tool": name, "arguments": args, "result": result})
        return result

    def probe_function(self, method_id, probe_spec=""):
        return self._run(
            "probe_function",
            [method_id, probe_spec],
            lambda: self._probe_function(method_id, probe_spec),
        )

    def trace_functions(self, method_ids, max_events=1000):
        def run():
            if not self.can_run_runtime_experiment:
                return self._blocked_runtime_result()
            unsupported = self._unsupported_backend()
            if unsupported:
                return f"status: unsupported\nerror: {unsupported}"
            if self.project is None:
                return "status: execution_error\nerror: Java tracing requires a project."

            self._total_runtime_calls += 1
            result = collect_java_trace(
                project=self.project,
                test_id=str(self.test_id),
                method_ids=method_ids,
                max_events=max_events,
            )
            if result.get("status") != "success":
                return (
                    f"status: {result.get('status', 'execution_error')}\n"
                    f"error: {result.get('error', 'Java trace collection failed')}"
                )

            observed = result["observed"]
            missing = result["not_observed"]
            fields = [
                f"test_id: {result['test_id']}",
                f"test_outcome: {result.get('test_outcome') or 'unknown'}",
                "trace_source: on-demand Java agent",
                f"event_count: {result['event_count']}",
                "observed_method_ids:",
                *(
                    [
                        f"- {method_id}: {result['counts'].get(method_id, 0)} call(s)"
                        for method_id in observed
                    ]
                    or ["- (none)"]
                ),
                "requested_but_not_observed:",
                *([f"- {method_id}" for method_id in missing] or ["- (none)"]),
            ]
            if result.get("events"):
                fields.extend(
                    [
                        "events:",
                        *[
                            f"- {event['event']}: {event['method_id']}"
                            for event in result["events"]
                        ],
                    ]
                )
            if result.get("truncated"):
                fields.append(f"note: event output was capped at {max_events} entries.")
            return (
                "## Java Trace Result\n"
                "status: success\n"
                "summary: reran the selected test with Java method tracing\n\n"
                + "\n".join(fields)
            )

        return self._run("trace_functions", [method_ids, max_events], run)
