"""Serial multi-agent debugging, patch review, and regression control."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time

from src.patching.java.patch_validation import rollback_validated_patch, snapshot_diff, validate_patch
from src.debug.semantic_agent.agent import Agent
from src.debug.semantic_agent.diagnosis_agent import DebugDiagnosisAgent
from src.debug.semantic_agent.patch_generation_agent import PatchGenerationAgent
from src.debug.semantic_agent.patch_review_agent import PatchReviewAgent
from src.debug.semantic_agent.prompt import orchestrator_system_prompt
from src.preprocess.context import load_preprocess_context
from src.preprocess.runner import PreprocessStageRunner
from src.fault_graph.java_refresh import has_repair_index, refresh_java_files, update_java_failure_context


FEEDBACK_LIMIT = 2000
HISTORY_LIMIT = 6000
TOTAL_CONTEXT_LIMIT = 12000


def _truncate(text: object, limit: int) -> str:
    value = str(text or "")
    return value if len(value) <= limit else value[:limit].rstrip() + "\n...[truncated]"


class RepairOrchestrator:
    """Coordinate FL, patch generation, patch review, and repair rounds."""

    def __init__(self, *, project, project_spec, paths, preprocess_dir, llm_settings,
                 test_case_id=None, baseline_sources=None, budget_started_at=None):
        self.project = project
        self.project_spec = project_spec
        self.paths = paths
        self.preprocess_dir = Path(preprocess_dir)
        self.llm_settings = llm_settings
        self.test_case_id = test_case_id
        self.max_repairs = max(1, int(os.getenv("PROODOS_MAX_REPAIR_ROUNDS", os.getenv("PROODOS_MAX_REPAIRS", "5"))))
        self.max_patch_attempts = max(1, int(os.getenv("PROODOS_MAX_PATCH_ATTEMPTS", "5")))
        self.max_diagnosis_rounds = max(1, int(os.getenv("PROODOS_MAX_DIAGNOSIS_ROUNDS", "3")))
        self.max_review_attempts = max(1, int(os.getenv("PROODOS_MAX_REVIEW_ATTEMPTS", "2")))
        self.time_budget_seconds = max(1.0, float(os.getenv("PROODOS_TIME_BUDGET_SECONDS", "3600")))
        self.started_at = budget_started_at if budget_started_at is not None else time.monotonic()
        self.baseline_sources = dict(baseline_sources or {})
        self.accepted_patches: list[dict[str, object]] = []
        self.accepted_files: set[str] = set()
        self.current_failing_tests = []
        self.last_regression = None

    def _check_budget(self):
        if time.monotonic() - self.started_at >= self.time_budget_seconds:
            raise TimeoutError(f"Case time budget exhausted ({self.time_budget_seconds:g} seconds)")

    @staticmethod
    def _failure_ids(failures):
        return [str(test.test_id) for test in failures]

    def _failure_text(self, failures):
        if not failures:
            return "No failing tests remain."
        chunks = []
        for test in failures:
            report = test.stack_trace or test.failure_message or "No failure report available"
            chunks.append(f"Test ID: {test.test_id}\nFailure:\n{_truncate(report, FEEDBACK_LIMIT)}")
        return _truncate("\n\n".join(chunks), TOTAL_CONTEXT_LIMIT)

    def _select_test(self, failures, round_index):
        if self.test_case_id:
            wanted = str(self.test_case_id)
            if any(test.test_id == wanted for test in failures):
                return wanted, "Explicit test selector"
            return None, f"Explicit test {wanted} is no longer failing"
        if not failures:
            return None, "All regression tests pass"
        agent = Agent(
            tools=[], system_prompt=orchestrator_system_prompt, settings=self.llm_settings,
            name="orchestrator_agent", output_dir=self.paths.output_dir,
            test_id=f"round-{round_index}", max_steps=3,
            validate=self._validate_test_selection,
        )
        task = (
            f"<regression_round>{round_index}</regression_round>\n"
            f"<current_failing_tests>{self._failure_ids(failures)}</current_failing_tests>\n"
            f"<accepted_patches>{json.dumps(self.accepted_patches, default=str)}</accepted_patches>\n"
            f"<failures>\n{self._failure_text(failures)}\n</failures>"
        )
        report = agent.run(task)
        selected = str(report.get("test_id") or "").strip()
        if any(test.test_id == selected for test in failures):
            return selected, str(report.get("explanation") or "")
        return failures[0].test_id, "Fallback: first current failing test"

    @staticmethod
    def _validate_test_selection(payload):
        if not isinstance(payload, dict) or not str(payload.get("test_id") or "").strip():
            raise ValueError("Return test_id and explanation")
        if not str(payload.get("explanation") or "").strip():
            raise ValueError("Return test_id and explanation")

    def _diagnose(self, test_id, failures, repair_round, diagnosis_round, feedback=""):
        self._check_budget()
        preprocess_paths = self.paths.__class__(
            project_root=self.paths.project_root, project_path=self.paths.project_path,
            output_dir=self.preprocess_dir,
        )
        graph_path = self.preprocess_dir / "fault_context.sqlite"
        if not has_repair_index(graph_path):
            result = PreprocessStageRunner(
                project=self.project, project_spec=self.project_spec,
                paths=preprocess_paths,
            ).run()
            if result.status.value != "success":
                raise RuntimeError(result.message)
        update_java_failure_context(graph_path, failures)
        context = load_preprocess_context(self.preprocess_dir)
        agent = DebugDiagnosisAgent(
            self.llm_settings, context, self.paths.output_dir, test_id,
            project=self.project,
        )
        task = (
            f"<current_failure>\n{self._failure_text(failures)}\n</current_failure>\n"
            f"<diagnosis_round>{diagnosis_round}</diagnosis_round>\n"
            f"<previous_patch_feedback>{_truncate(feedback, HISTORY_LIMIT)}</previous_patch_feedback>\n"
            "Diagnose the production defect on this current version."
        )
        report = agent.run(task).get("report") or {}
        method_id = agent.resolve_method(report.get("method_id"))
        if not method_id:
            raise RuntimeError("Debug diagnosis returned no indexed method")
        return agent, report, method_id

    def _generate_patch(self, *, test_id, method_id, source, debug_report,
                        failures, attempt, feedback, history, preprocess_data):
        self._check_budget()
        agent = PatchGenerationAgent(
            self.llm_settings, self.paths.output_dir, test_id, method_id,
            preprocess_data=preprocess_data,
        )
        task = (
            f"<failure>\n{self._failure_text(failures)}\n</failure>\n"
            f"<diagnosed_method_id>{method_id}</diagnosed_method_id>\n"
            f"<diagnosed_source>\n{source}\n</diagnosed_source>\n"
            f"<debug_report>\n{_truncate(json.dumps(debug_report, ensure_ascii=False, default=str), FEEDBACK_LIMIT)}\n</debug_report>\n"
            f"<patch_attempt>{attempt}</patch_attempt>\n"
            f"<previous_validation_feedback>{_truncate(feedback, FEEDBACK_LIMIT)}</previous_validation_feedback>\n"
            f"<previous_attempts>{_truncate(history, HISTORY_LIMIT)}</previous_attempts>\n"
            "Generate one minimal production repair, or request further diagnosis if this suspect is insufficient."
        )
        return agent.run(task)

    def _review(self, *, test_id, method_id, patch, failures, attempt, diagnosis_round, validation):
        """Run review attempts; malformed review decisions get one retry."""
        before_ids = set(self._failure_ids(failures))
        last = None
        for review_attempt in range(1, self.max_review_attempts + 1):
            self._check_budget()
            agent = PatchReviewAgent(
                self.llm_settings, self.paths.output_dir, test_id, self.project,
            )
            try:
                report = agent.run(
                    f"<target_test>{test_id}</target_test>\n"
                    f"<method_id>{method_id}</method_id>\n"
                    f"<diagnosis_round>{diagnosis_round}</diagnosis_round>\n"
                    f"<patch_attempt>{attempt}</patch_attempt>\n"
                    f"<review_attempt>{review_attempt}</review_attempt>\n"
                    "<selected_test_passed>true</selected_test_passed>\n"
                    f"<candidate_patch>\n{patch.get('replacement_function', '')}\n</candidate_patch>\n"
                    f"<validated_diff>\n{validation.get('diff', '')}\n</validated_diff>\n"
                    f"<expected_behavior>{_truncate(patch.get('expected_behavior'), FEEDBACK_LIMIT)}</expected_behavior>\n"
                    f"<patch_explanation>{_truncate(patch.get('explanation'), FEEDBACK_LIMIT)}</patch_explanation>\n"
                    f"<baseline_failing_tests>{sorted(before_ids)}</baseline_failing_tests>\n"
                    f"<accepted_patches>{_truncate(json.dumps(self.accepted_patches, ensure_ascii=False, default=str), HISTORY_LIMIT)}</accepted_patches>\n"
                    "Review the candidate after the full regression result."
                )
            except Exception as exc:
                report = {
                    "decision": "reject",
                    "reason": f"Patch review regression failed: {type(exc).__name__}: {exc}",
                    "current_failing_tests": sorted(before_ids),
                    "execution_error": True,
                }
            after_ids = set(report.get("current_failing_tests") or [])
            report.update({
                "review_attempt": review_attempt,
                "fixed_tests": sorted(before_ids - after_ids),
                "introduced_tests": sorted(after_ids - before_ids),
                "remaining_tests": sorted(before_ids & after_ids),
                "review_agent": agent,
            })
            last = report
            if str(report.get("decision") or "").lower() in {"accept_finish", "accept_continue", "reject"}:
                return report
        return last or {
            "decision": "reject",
            "reason": "Patch review did not return a valid decision",
            "current_failing_tests": sorted(before_ids),
            "execution_error": True,
        }

    def run(self):
        history = []
        try:
            self._check_budget()
            regression = self.project.run_tests()
            self.last_regression = regression
            self.current_failing_tests = list(regression.failing_tests)
            if not self.current_failing_tests:
                return self._success(history, 0)

            for repair_round in range(1, self.max_repairs + 1):
                self._check_budget()
                failures = list(self.current_failing_tests)
                if not failures:
                    return self._success(history, repair_round)
                test_id, selection_reason = self._select_test(failures, repair_round)
                if not test_id:
                    return self._failure(history, selection_reason)
                accepted_this_round = False
                diagnosis_feedback = ""

                for diagnosis_round in range(1, self.max_diagnosis_rounds + 1):
                    try:
                        diagnoser, debug_report, method_id = self._diagnose(
                            test_id, failures, repair_round, diagnosis_round, diagnosis_feedback
                        )
                        source = diagnoser.method_source(method_id)
                    except Exception as exc:
                        diagnosis_feedback = str(exc)
                        history.append({"round": repair_round, "diagnosis_round": diagnosis_round,
                                        "test_id": test_id, "status": "diagnosis_error",
                                        "error": str(exc)})
                        continue

                    feedback = diagnosis_feedback
                    attempt_history = []
                    requested_diagnosis = False
                    for attempt in range(1, self.max_patch_attempts + 1):
                        try:
                            patch = self._generate_patch(
                                test_id=test_id, method_id=method_id, source=source,
                                debug_report=debug_report, failures=failures,
                                attempt=attempt, feedback=feedback,
                                history=json.dumps(attempt_history, ensure_ascii=False, default=str),
                                preprocess_data=diagnoser.preprocess_data,
                            )
                        except Exception as exc:
                            patch = {"type": "request_diagnosis", "reason": str(exc),
                                     "explanation": str(exc)}
                        record = {"round": repair_round, "diagnosis_round": diagnosis_round,
                                  "attempt": attempt, "test_id": test_id,
                                  "selection": selection_reason, "method_id": method_id,
                                  "patch": patch}
                        if str(patch.get("type") or "patch").lower() == "request_diagnosis":
                            diagnosis_feedback = _truncate(
                                patch.get("reason") or patch.get("explanation"), FEEDBACK_LIMIT
                            )
                            record["status"] = "request_diagnosis"
                            history.append(record)
                            requested_diagnosis = True
                            break

                        try:
                            validation = validate_patch(
                                project=self.project, test_id=test_id, method_id=method_id,
                                replacement_function=str(patch.get("replacement_function") or ""),
                            )
                        except Exception as exc:
                            validation = {"status": "error", "accepted": False,
                                          "error": f"{type(exc).__name__}: {exc}"}
                        record["validation"] = validation
                        if not validation.get("accepted"):
                            feedback = _truncate(
                                validation.get("error") or validation.get("temporary_validation"),
                                FEEDBACK_LIMIT,
                            )
                            attempt_history.append({"attempt": attempt, "feedback": feedback})
                            history.append(record)
                            if validation.get("fatal"):
                                return self._failure(history, "Patch validation rollback failed")
                            continue

                        review = self._review(
                            test_id=test_id, method_id=method_id, patch=patch,
                            failures=failures, attempt=attempt,
                            diagnosis_round=diagnosis_round,
                            validation=validation,
                        )
                        record["review"] = {k: v for k, v in review.items() if k != "review_agent"}
                        decision = str(review.get("decision") or "reject").lower()
                        current_failures = list(review.get("current_failing_tests") or [])
                        if review.get("execution_error", False):
                            decision = "reject"
                            review["reason"] = (
                                review.get("reason")
                                or "Regression compilation or test execution failed"
                            )
                        if decision == "accept_finish" and current_failures:
                            decision = "reject"
                            review["reason"] = "accept_finish requires an empty current failure list"
                        if decision in {"accept_finish", "accept_continue"}:
                            try:
                                refresh_java_files(
                                    self.preprocess_dir / "fault_context.sqlite", self.project,
                                    [str(validation["file_path"])],
                                )
                            except Exception as exc:
                                rollback_validated_patch(self.project, validation)
                                record["index_refresh_error"] = str(exc)
                                history.append(record)
                                return self._failure(history, f"Accepted candidate index refresh failed: {exc}")
                            self.accepted_files.add(str(validation.get("file_path") or ""))
                            self.accepted_patches.append({
                                "method_id": method_id, "test_id": test_id,
                                "file_path": validation.get("file_path"),
                                "review": review.get("reason", ""),
                            })
                            self.last_regression = review["review_agent"].last_regression
                            self.current_failing_tests = list(self.last_regression.failing_tests)
                            history.append(record)
                            accepted_this_round = True
                            if decision == "accept_finish" or not self.current_failing_tests:
                                return self._success(history, repair_round)
                            break

                        try:
                            rollback_validated_patch(self.project, validation)
                        except Exception as exc:
                            record["rollback_error"] = str(exc)
                            history.append(record)
                            return self._failure(history, "Candidate rollback failed")
                        feedback = _truncate(review.get("reason"), FEEDBACK_LIMIT)
                        attempt_history.append({"attempt": attempt, "feedback": feedback,
                                                "review": record["review"]})
                        history.append(record)
                    if accepted_this_round:
                        break
                    if requested_diagnosis:
                        continue
                if accepted_this_round:
                    continue
                return self._failure(
                    history, f"No accepted patch after {self.max_diagnosis_rounds} diagnosis round(s)"
                )
            return self._failure(history, f"Repair budget exhausted after {self.max_repairs} round(s)")
        except TimeoutError as exc:
            return self._failure(history, str(exc))
        except Exception as exc:
            return self._failure(history, f"Repair error: {type(exc).__name__}: {exc}")

    def _success(self, history, rounds):
        regression = self.last_regression
        if (regression is None or not regression.success or regression.failed
                or regression.errors or regression.failing_tests):
            return self._failure(history, "Full regression did not pass or its result is unavailable")
        return {
            "repaired_methods": [p["method_id"] for p in self.accepted_patches],
            "explanation": f"All regression tests pass after {len(self.accepted_patches)} accepted patch(es).",
            "repair_status": "success", "rounds": rounds,
            "accepted_patches": self.accepted_patches, "history": history,
            "final_diff": snapshot_diff(self.baseline_sources, self.project, self.accepted_files),
        }

    def _failure(self, history, reason):
        return {
            "repaired_methods": [p["method_id"] for p in self.accepted_patches],
            "explanation": reason, "repair_status": "incomplete",
            "accepted_patches": self.accepted_patches, "history": history,
            "final_diff": "",
        }
