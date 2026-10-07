from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class InterventionStatus(str, Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    UNSUPPORTED = "UNSUPPORTED"
    ERROR = "ERROR"


@dataclass(frozen=True)
class TestOutcome:
    passed: bool
    failing_tests: int = 0
    failure_message: str = ""


@dataclass(frozen=True)
class InterventionRequest:
    test_class: str
    test_method: str
    target_class: str
    target_method: str
    # Complete Java method definition compiled as a temporary source override.
    replacement_function: str | None = None
    return_type: str = "java.lang.Object"
    parameter_types: tuple[str, ...] = ()
    source_roots: tuple[str, ...] = ()


@dataclass(frozen=True)
class InterventionResult:
    status: InterventionStatus
    original_result: TestOutcome | None = None
    intervention_result: TestOutcome | None = None
    generated_source_path: str | None = None
    generated_source_code: str | None = None
    stdout: str = ""
    stderr: str = ""
    error_message: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "original_result": None
            if self.original_result is None
            else {
                "passed": self.original_result.passed,
                "failing_tests": self.original_result.failing_tests,
                "failure_message": self.original_result.failure_message,
            },
            "intervention_result": None
            if self.intervention_result is None
            else {
                "passed": self.intervention_result.passed,
                "failing_tests": self.intervention_result.failing_tests,
                "failure_message": self.intervention_result.failure_message,
            },
            "generated_source_path": self.generated_source_path,
            "generated_source_code": self.generated_source_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "error_message": self.error_message,
            "extras": dict(self.extras),
        }
