"""Regression-based review of a selected-test-passing candidate patch."""
from __future__ import annotations

from src.debug.semantic_agent.agent import Agent
from src.debug.semantic_agent.prompt import patch_review_system_prompt


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "\n...[truncated]"


class PatchReviewAgent:
    """Run the full regression suite and ask an LLM to review its result."""

    name = "patch_review_agent"
    max_steps = 3

    def __init__(self, llm_settings, output_dir, test_id, project):
        self.project = project
        self.test_id = str(test_id)
        self.last_regression = None
        self.agent = Agent(
            tools=[], system_prompt=patch_review_system_prompt,
            settings=llm_settings, name=self.name, output_dir=output_dir,
            test_id=test_id, max_steps=self.max_steps, validate=self.validate,
        )

    def run(self, task: str) -> dict:
        self.last_regression = self.project.run_tests()
        regression = self.last_regression
        failures = [
            {
                "test_id": test.test_id,
                "failure": (test.failure_message or test.stack_trace or "")[-2000:],
            }
            for test in regression.failing_tests
        ]
        failure_text = _truncate(str(failures), 6000)
        current_tests = _truncate(str([test.test_id for test in regression.failing_tests]), 2000)
        execution_error = bool(
            regression.errors or (not regression.success and not regression.failing_tests and regression.failed == 0)
        )
        passed = regression.passed if regression.passed is not None else "unknown (runner does not report this count)"
        report = self.agent.run(
            f"{task}\n<regression_result>\n"
            f"execution_success={regression.success}\n"
            f"command={regression.command}\n"
            f"execution_seconds={regression.execution_time}\n"
            f"passed={passed}\nfailed={regression.failed}\n"
            f"errors={regression.errors}\n"
            f"execution_error={execution_error}\n"
            f"current_failing_tests={current_tests}\n"
            f"failure_details={failure_text}\n"
            f"runner_stdout={_truncate(regression.stdout, 2000)}\n"
            f"runner_stderr={_truncate(regression.stderr, 2000)}\n"
            f"</regression_result>"
        )
        report["current_failing_tests"] = [test.test_id for test in regression.failing_tests]
        report["regression_success"] = regression.success
        report["regression_failed"] = regression.failed
        report["regression_errors"] = regression.errors
        report["execution_error"] = execution_error
        return report

    @staticmethod
    def validate(payload):
        if not isinstance(payload, dict) or not str(payload.get("reason") or "").strip():
            raise ValueError("Return an evidence-based reason")
        decision = str(payload.get("decision") or "").strip().lower()
        if decision not in {"accept_finish", "accept_continue", "reject"}:
            raise ValueError("decision must be accept_finish, accept_continue, or reject")
