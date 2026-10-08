"""Serial debugging controller for fault diagnosis, repair, and regression."""
from __future__ import annotations

import json
import os
from pathlib import Path

from src.patching.java.patch_validation import validate_patch
from src.debug.semantic_agent.diagnosis_agent import DebugDiagnosisAgent
from src.debug.semantic_agent.patch_generation_agent import PatchGenerationAgent
from src.debug.semantic_agent.agent import Agent
from src.debug.semantic_agent.prompt import orchestrator_system_prompt
from src.preprocess.runner import PreprocessStageRunner
from src.preprocess.context import load_preprocess_context


class RepairOrchestrator:
    """Choose one failing test, diagnose it, generate a patch, then regress.

    All state is deliberately serial: a patch is accepted only after the
    selected test passes, and the next iteration starts from a full regression
    on the checkout containing every previously accepted patch.
    """

    def __init__(self, *, project, project_spec, paths, preprocess_dir, llm_settings,
                 test_case_id=None):
        self.project = project
        self.project_spec = project_spec
        self.paths = paths
        self.preprocess_dir = Path(preprocess_dir)
        self.llm_settings = llm_settings
        self.test_case_id = test_case_id
        self.max_repairs = max(1, int(os.getenv("PROODOS_MAX_REPAIRS", "5")))
        self.max_patch_attempts = max(1, int(os.getenv("PROODOS_MAX_PATCH_ATTEMPTS", "3")))
        self.accepted_patches = []

    def _failure_text(self, failures) -> str:
        if not failures:
            return "No failing tests remain."
        chunks = []
        for test in failures:
            report = test.stack_trace or test.failure_message or "No failure report available"
            chunks.append(f"Test ID: {test.test_id}\nFailure:\n{report[-12000:]}")
        return "\n\n".join(chunks)

    def _select_test(self, failures, round_index: int) -> tuple[str | None, str]:
        if self.test_case_id:
            wanted = str(self.test_case_id)
            for failure in failures:
                if failure.test_id == wanted:
                    return wanted, "Explicit test selector"
            return None, f"Explicit test {wanted} is no longer failing"
        if not failures:
            return None, "All regression tests pass"
        agent = Agent(
            tools=[], system_prompt=orchestrator_system_prompt,
            settings=self.llm_settings, name="orchestrator_agent",
            output_dir=self.paths.output_dir, test_id=f"round-{round_index}",
            max_steps=2, validate=self._validate_test_selection,
        )
        task = (
            f"<regression_round>{round_index}</regression_round>\n"
            f"<accepted_patches>{json.dumps(self.accepted_patches, default=str)}</accepted_patches>\n"
            f"<failures>\n{self._failure_text(failures)}\n</failures>"
        )
        report = agent.run(task)
        selected = str(report.get("test_id") or "").strip()
        if any(test.test_id == selected for test in failures):
            return selected, str(report.get("explanation") or "")
        # A deterministic fallback keeps a malformed model answer from
        # discarding a runnable repair round.
        return failures[0].test_id, "Fallback: first current failing test"

    @staticmethod
    def _validate_test_selection(payload):
        if not isinstance(payload, dict) or not str(payload.get("test_id") or "").strip():
            raise ValueError("Return test_id and explanation")
        if not str(payload.get("explanation") or "").strip():
            raise ValueError("Return explanation")

    def _diagnose(self, test_id: str, failures, round_index: int):
        preprocess_dir = self.preprocess_dir
        preprocess_paths = self.paths.__class__(
            project_root=self.paths.project_root, project_path=self.paths.project_path,
            output_dir=preprocess_dir,
        )
        graph_path = preprocess_dir / "fault_context.sqlite"
        # ``--stage all`` has already indexed the checkout immediately before
        # debugging. Reuse that graph for the first round; after an
        # accepted patch the next round deliberately rebuilds it.
        if not (round_index == 1 and graph_path.is_file()):
            result = PreprocessStageRunner(
                project=self.project, project_spec=self.project_spec,
                paths=preprocess_paths, test_case_id=test_id,
            ).run()
            if result.status.value != "success":
                raise RuntimeError(result.message)
        context = load_preprocess_context(preprocess_dir)
        agent = DebugDiagnosisAgent(
            self.llm_settings, context, self.paths.output_dir, test_id,
            project=self.project,
        )
        report = agent.run(
            f"<current_failure>\n{self._failure_text([t for t in failures if t.test_id == test_id])}\n</current_failure>\n"
            "Diagnose the production defect on this current version."
        ).get("report") or {}
        method_id = agent.resolve_method(report.get("method_id"))
        if not method_id:
            raise RuntimeError("Debug diagnosis returned no indexed method")
        return agent, report, method_id

    def _generate_patch(self, *, test_id, method_id, source, debug_report, failures, attempt):
        agent = PatchGenerationAgent(
            self.llm_settings, self.paths.output_dir, test_id,
            method_id,
        )
        task = (
            f"<failure>\n{self._failure_text(failures)}\n</failure>\n"
            f"<diagnosed_method_id>{method_id}</diagnosed_method_id>\n"
            f"<diagnosed_source>\n{source}\n</diagnosed_source>\n"
            f"<debug_report>\n{json.dumps(debug_report, ensure_ascii=False, default=str)}\n</debug_report>\n"
            f"<patch_attempt>{attempt}</patch_attempt>\n"
            "Generate one minimal production repair."
        )
        return agent.run(task)

    def run(self):
        all_history = []
        for round_index in range(1, self.max_repairs + 1):
            regression = self.project.run_tests()
            failures = list(regression.failing_tests)
            if not failures:
                return self._success(all_history, round_index, regression)
            test_id, selection_reason = self._select_test(failures, round_index)
            if not test_id:
                return self._failure(
                    all_history,
                    f"Selected test is no longer failing, but {len(failures)} regression failure(s) remain",
                )
            try:
                diagnoser, debug_report, method_id = self._diagnose(
                    test_id, failures, round_index
                )
                source = diagnoser.method_source(method_id)
            except Exception as exc:
                all_history.append({"round": round_index, "test_id": test_id,
                                    "status": "debug_error", "error": str(exc)})
                continue
            accepted = None
            for attempt in range(1, self.max_patch_attempts + 1):
                patch = self._generate_patch(
                    test_id=test_id, method_id=method_id, source=source,
                    debug_report=debug_report, failures=failures, attempt=attempt,
                )
                validation = validate_patch(
                    project=self.project, test_id=test_id, method_id=method_id,
                    replacement_function=str(patch.get("replacement_function") or ""),
                )
                record = {"round": round_index, "test_id": test_id,
                          "selection": selection_reason, "method_id": method_id,
                          "debug_report": debug_report, "patch": patch,
                          "validation": validation}
                all_history.append(record)
                if validation.get("accepted"):
                    accepted = validation
                    self.accepted_patches.append({
                        "method_id": method_id, "file_path": validation.get("file_path"),
                        "diff": validation.get("diff", ""),
                    })
                    break
                if validation.get("fatal"):
                    return self._failure(all_history, "Patch rollback failed")
            if accepted is None:
                continue
            # The next loop iteration runs the complete regression on the
            # accepted checkout and either finishes or selects the next failure.
        return self._failure(all_history, f"Repair budget exhausted after {self.max_repairs} rounds")

    def _success(self, history, rounds, regression):
        if regression.errors or regression.failed:
            details = []
            if regression.failed:
                details.append(f"{regression.failed} failing test(s)")
            if regression.errors:
                details.append(f"{regression.errors} execution error(s)")
            return self._failure(
                history,
                f"Regression reported {', '.join(details)}; no clean all-tests result",
            )
        return {"repaired_methods": [p["method_id"] for p in self.accepted_patches],
                "explanation": f"All regression tests pass after {len(self.accepted_patches)} accepted patch(es).",
                "repair_status": "success", "rounds": rounds,
                "accepted_patches": self.accepted_patches, "history": history,
                "regression": {"failed": regression.failed, "errors": regression.errors}}

    def _failure(self, history, reason):
        return {"repaired_methods": [p["method_id"] for p in self.accepted_patches],
                "explanation": reason, "repair_status": "incomplete",
                "accepted_patches": self.accepted_patches, "history": history}
