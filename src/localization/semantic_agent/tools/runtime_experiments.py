from __future__ import annotations

import re
from typing import Any
from src.intervention.c_kernel import apply_c_intervention, apply_c_observation
from src.intervention.java.bridge import apply_intervention, apply_observation
from src.intervention.java.tracing import collect_java_trace
from src.intervention.c_kernel.tracing import collect_ftrace, format_ftrace_result
from src.localization.semantic_agent.tools.tool_response import format_intervention_result, format_observation_result

MAX_EXECUTE_ATTEMPTS = 2
MAX_OBSERVE_ATTEMPTS = 2
MAX_TOTAL_INTERVENTION_CALLS = 6


class RuntimeExperiments:
    """Case-scoped experiment budgets and raw results shared by the three stages."""
    def __init__(self, project, preprocess_data, test_id):
        self.project = project
        self.project_root = project.spec.project_path if project is not None else None
        self.preprocess_data = preprocess_data
        self.test_id = test_id
        self._execute_attempts = {}
        self._observe_attempts = {}
        self._total_intervention_calls = 0
        self._guest_unavailable_reason: str | None = None
        self._last_execute_results = {}
        self._last_observe_results = {}
        self.history = []
        self._cache = {}
        self.allowed_method_id = None

    @property
    def can_run_runtime_experiment(self) -> bool:
        return (
            self._guest_unavailable_reason is None
            and self._total_intervention_calls < MAX_TOTAL_INTERVENTION_CALLS
        )

    @property
    def runtime_unavailable_reason(self) -> str | None:
        if self._guest_unavailable_reason:
            return self._guest_unavailable_reason
        if self._total_intervention_calls >= MAX_TOTAL_INTERVENTION_CALLS:
            return (
                f"Case runtime experiment budget ({MAX_TOTAL_INTERVENTION_CALLS} calls) is exhausted; "
                "continue with collected and static evidence."
            )
        return None

    def _blocked_runtime_result(self, method_id: str | None = None) -> str:
        reason = self.runtime_unavailable_reason or "Runtime experiments are unavailable."
        method = f"\nmethod_id: {method_id}" if method_id else ""
        return f"status: execution_error\nerror: {reason}{method}"

    def _remember_runtime_failure(self, result: Any) -> None:
        """Stop retrying QEMU runtime tools after a case-wide guest boot failure."""
        if isinstance(result, dict):
            message = str(result.get("error") or "")
            status = str(result.get("status") or "")
        else:
            text = str(result or "")
            status_match = re.search(r"(?m)^status:\s*([A-Za-z_]+)", text)
            error_match = re.search(r"(?m)^error:\s*(.*)$", text)
            status = status_match.group(1) if status_match else ""
            message = error_match.group(1) if error_match else text
        if status not in {"execution_error", ""}:
            return
        normalized = message.casefold()
        boot_failure_markers = (
            "guest kernel crashed before ssh",
            "guest crashed before the intervention test could run",
            "did not become reachable over ssh",
            "could not boot a reachable guest",
            "qemu boot/ssh timeout",
            "guest did not become reachable over ssh",
        )
        if any(marker in normalized for marker in boot_failure_markers):
            self._guest_unavailable_reason = message.strip() or "Guest failed before SSH became available."

    def _probe_function(self, method_id: str, probe_spec: str | dict[str, Any] = "") -> str:
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
        language = self._backend_language()
        if language not in {"", "java", "c"}:
            return format_observation_result(
                {
                    "status": "unsupported",
                    "error": self._non_jvm_reason(),
                    "method_id": method_id,
                    "probe_spec": probe_spec,
                    "attempts": attempts,
                    "samples": [],
                    "call_count": 0,
                    "truncated": False,
                }
            )
        if self._total_intervention_calls >= MAX_TOTAL_INTERVENTION_CALLS:
            return format_observation_result(
                {
                    "status": "execution_error",
                    "error": (
                        f"Global intervention budget ({MAX_TOTAL_INTERVENTION_CALLS} "
                        "observe+execute calls) exhausted for this case; classify from "
                        "existing evidence instead."
                    ),
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
                        f"{method_id}; stop retrying and use last result."
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
        self._total_intervention_calls += 1
        if language == "c":
            result = apply_c_observation(
                project=self.project,
                preprocess_data=self.preprocess_data,
                test_id=self.test_id,
                method_id=method_id,
                probe_spec=probe_spec,
                fetch_args=probe_spec,
            )
        else:
            result = apply_observation(
                project_root=self.project_root,
                test_id=self.test_id,
                method_id=method_id,
                probe_spec=probe_spec,
            )
        result["attempts"] = attempts
        # A runtime/setup error is already a terminal result for this exact
        # method. Do not spend the remaining experiment budget repeating a
        # probe that the kernel cannot register or a guest that has failed;
        # the model should continue with static evidence and finish the report.
        if result.get("status") in {"execution_error", "validation_error", "unsupported"}:
            self._observe_attempts[method_id] = MAX_OBSERVE_ATTEMPTS
        self._last_observe_results[method_id] = result
        return format_observation_result(result)

    def _execute_intervention(self, method_id: str, replacement_function: str) -> str:
        attempts = self._execute_attempts.get(method_id, 0)
        if not self.can_run_runtime_experiment:
            return self._blocked_runtime_result(method_id)
        if self.project is None or self.project_root is None:
            return format_intervention_result(
                {
                    "status": "execution_error",
                    "error": "Intervention requires a project (with project_path).",
                    "method_id": method_id,
                    "replacement_function": replacement_function,
                    "attempts": attempts,
                }
            )
        language = self._backend_language()
        if language not in {"", "java", "c"}:
            return format_intervention_result(
                {
                    "status": "unsupported",
                    "error": self._non_jvm_reason(),
                    "method_id": method_id,
                    "replacement_function": replacement_function,
                    "attempts": attempts,
                }
            )
        if self._total_intervention_calls >= MAX_TOTAL_INTERVENTION_CALLS:
            return format_intervention_result(
                {
                    "status": "execution_error",
                    "error": (
                        f"Global intervention budget ({MAX_TOTAL_INTERVENTION_CALLS} "
                        "observe+execute calls) exhausted for this case; classify from "
                        "existing evidence instead."
                    ),
                    "method_id": method_id,
                    "replacement_function": replacement_function,
                    "attempts": attempts,
                }
            )
        if attempts >= MAX_EXECUTE_ATTEMPTS:
            last = self._last_execute_results.get(method_id) or {}
            return format_intervention_result(
                {
                    "status": "execution_error",
                    "error": (
                        f"Max execute attempts ({MAX_EXECUTE_ATTEMPTS}) reached for "
                        f"{method_id}; stop further execute_intervention (single-method budget "
                        "exhausted). Still write the full Causal Report for the Counterfactual Agent "
                        "(unsupported/inert; do not stub other methods)."
                    ),
                    "method_id": method_id,
                    "replacement_function": replacement_function,
                    "attempts": attempts,
                    "outcome": last.get("outcome"),
                    "test_passed": last.get("test_passed"),
                    "selected_mode": last.get("selected_mode"),
                    "generated_source_code": last.get("generated_source_code"),
                    "stdout": last.get("stdout"),
                    "stderr": last.get("stderr"),
                }
            )

        attempts += 1
        self._execute_attempts[method_id] = attempts
        self._total_intervention_calls += 1
        if language == "c":
            result = apply_c_intervention(
                project=self.project,
                preprocess_data=self.preprocess_data,
                test_id=self.test_id,
                method_id=method_id,
                replacement_function=replacement_function,
            )
        else:
            result = apply_intervention(
                project_root=self.project_root,
                test_id=self.test_id,
                method_id=method_id,
                replacement_function=replacement_function,
            )
        result["attempts"] = attempts
        result.setdefault("method_id", method_id)
        if result.get("status") in {"execution_error", "validation_error", "unsupported"}:
            self._execute_attempts[method_id] = MAX_EXECUTE_ATTEMPTS
        self._last_execute_results[method_id] = result
        return format_intervention_result(result)

    def _backend_language(self) -> str:
        metadata = getattr(self.preprocess_data, "metadata", None) or {}
        return str(metadata.get("language", "java")).strip().lower()

    def _non_jvm_reason(self) -> str | None:
        """Reason intervention/observation is unavailable, or None when supported."""
        language = self._backend_language()
        if language in {"", "java", "c"}:
            return None
        return (
            f"Observation/intervention is not supported for this {language} "
            f"project (no Java test runner, no C kernel backend). Do not call "
            f"probe_function/execute_intervention for this case — classify from "
            f"source and propagation evidence instead."
        )

    def _run(self, name, args, operation):
        import json
        key = json.dumps([name, args], sort_keys=True)
        if key in self._cache:
            return self._cache[key]
        try:
            result = operation()
        except Exception as exc:
            result = f"status: execution_error\nerror: {type(exc).__name__}: {exc}"
        self._remember_runtime_failure(result)
        self._cache[key] = result
        self.history.append({"tool": name, "arguments": args, "result": result})
        return result

    def probe_function(self, method_id, probe_spec=""):
        return self._run("probe_function", [method_id, probe_spec],
                         lambda: self._probe_function(method_id, probe_spec))

    def execute_intervention(self, method_id, replacement_function):
        if method_id != self.allowed_method_id:
            return "status: validation_error\nerror: Intervention must target the current indexed suspect."
        return self._run("execute_intervention", [method_id, replacement_function],
                         lambda: self._execute_intervention(method_id, replacement_function))

    def trace_functions(self, method_ids, max_events=1000):
        def run():
            language = self._backend_language()
            if language == "java":
                if not self.can_run_runtime_experiment:
                    return self._blocked_runtime_result()
                if self.project is None:
                    return "status: execution_error\nerror: Java tracing requires a project."
                self._total_intervention_calls += 1
                result = collect_java_trace(
                    project=self.project,
                    test_id=str(self.test_id),
                    method_ids=method_ids,
                    max_events=max_events,
                )
                if result.get("status") != "success":
                    return f"status: {result.get('status', 'execution_error')}\nerror: {result.get('error', 'Java trace collection failed')}"
                observed = result["observed"]
                missing = result["not_observed"]
                fields = [
                    f"test_id: {result['test_id']}",
                    f"test_outcome: {result.get('test_outcome') or 'unknown'}",
                    "trace_source: on-demand Java agent",
                    f"event_count: {result['event_count']}",
                    "observed_method_ids:",
                    *([
                        f"- {method_id}: {result['counts'].get(method_id, 0)} call(s)"
                        for method_id in observed
                    ] or ["- (none)" ]),
                    "requested_but_not_observed:",
                    *([f"- {method_id}" for method_id in missing] or ["- (none)" ]),
                ]
                if result.get("events"):
                    fields.extend([
                        "events:",
                        *[
                            f"- {event['event']}: {event['method_id']}"
                            for event in result["events"]
                        ],
                    ])
                if result.get("truncated"):
                    fields.append(f"note: event output was capped at {max_events} entries.")
                return "## Java Trace Result\nstatus: success\nsummary: reran the selected test with Java method tracing\n\n" + "\n".join(fields)
            if language != "c":
                return f"status: unsupported\nerror: function tracing is not supported for the {language or 'unknown'} backend."
            if not self.can_run_runtime_experiment:
                return self._blocked_runtime_result()
            self._total_intervention_calls += 1
            return format_ftrace_result(collect_ftrace(
                project=self.project, test_id=self.test_id,
                method_ids=method_ids, max_events=max_events))
        return self._run("trace_functions", [method_ids, max_events], run)
