from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any
import re


Metadata = dict[str, Any]


class TestOutcome(str, Enum):
    PASS = "pass"
    FAIL = "fail"


class PipelineStageStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    NOT_IMPLEMENTED = "not_implemented"


@dataclass(frozen=True)
class ProjectSpec:
    dataset: str
    project_path: Path
    project_id: str | None = None
    bug_id: str | None = None


@dataclass(frozen=True)
class MethodRef:
    class_name: str
    method_name: str
    package_name: str | None = None
    signature: str | None = None
    file_path: Path | None = None

    @property
    def qualified_name(self) -> str:
        parts = []
        if self.package_name:
            parts.append(self.package_name)
        parts.append(self.class_name)
        base = ".".join(parts)
        suffix = self.method_name
        if self.signature:
            suffix = f"{suffix}{self.signature}"
        return f"{base}#{suffix}"

    @staticmethod
    def from_qualified_name(identifier: str) -> "MethodRef":
        class_part, method_part = identifier.split("#", 1)
        package_name: str | None = None
        class_name = class_part
        if "." in class_part:
            package_name, class_name = class_part.rsplit(".", 1)

        match = re.match(r"(?P<name>[^(]+)(?P<signature>\(.*\).*)?$", method_part)
        if match is None:
            raise ValueError(f"Invalid method identifier: {identifier}")

        return MethodRef(
            package_name=package_name,
            class_name=class_name,
            method_name=match.group("name"),
            signature=match.group("signature"),
        )


@dataclass(frozen=True)
class CompilationResult:
    success: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    command: list[str] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class TestCase:
    test_id: str
    class_name: str
    method_name: str
    file_path: Path | None = None
    failure_message: str = ""
    stack_trace: str = ""
    metadata: Metadata = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return f"{self.class_name}::{self.method_name}"


@dataclass(frozen=True)
class TestRunResult:
    success: bool
    passed: int
    failed: int
    errors: int
    failing_tests: list[TestCase] = field(default_factory=list)
    execution_time: float = 0.0
    stdout: str = ""
    stderr: str = ""
    command: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PipelineStageResult:
    project: str
    stage: str
    status: PipelineStageStatus
    message: str
    metadata: Metadata = field(default_factory=dict)


@dataclass(frozen=True)
class PipelineRunSummary:
    project: ProjectSpec
    stage_results: list[PipelineStageResult]
    preprocess_dir: Path | None = None
    localization_dir: Path | None = None
    result_dir: Path | None = None

    @property
    def output_dir(self) -> Path | None:
        return self.localization_dir or self.preprocess_dir

    @property
    def succeeded(self) -> bool:
        return all(result.status != PipelineStageStatus.FAILED for result in self.stage_results)

    def to_console(self) -> str:
        lines = [
            "CausalFL pipeline summary",
            f"project: {self.project.project_path}",
            f"dataset: {self.project.dataset}",
        ]
        if self.preprocess_dir is not None:
            lines.append(f"preprocess_dir: {self.preprocess_dir}")
        if self.localization_dir is not None:
            lines.append(f"localization_dir: {self.localization_dir}")
        if self.result_dir is not None:
            lines.append(f"result_dir: {self.result_dir}")
        if self.project.project_id:
            lines.append(f"project_id: {self.project.project_id}")
        if self.project.bug_id:
            lines.append(f"bug_id: {self.project.bug_id}")
        lines.append("stages:")
        for result in self.stage_results:
            lines.append(f"- {result.stage}: {result.status.value} ({result.message})")
        console_summaries = [
            str(result.metadata.get("console_summary", "")).strip()
            for result in self.stage_results
            if isinstance(result.metadata.get("console_summary"), str)
            and str(result.metadata.get("console_summary")).strip()
        ]
        for console_summary in console_summaries:
            lines.append("")
            lines.extend(console_summary.splitlines())
        return "\n".join(lines)
