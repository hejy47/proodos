from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Mapping

from src.models import CompilationResult, ProjectSpec, TestCase, TestRunResult


class Project(ABC):
    def __init__(self, spec: ProjectSpec):
        self.spec = spec
        self.project_path = spec.project_path

    @abstractmethod
    def compile(self, env: Mapping[str, str] | None = None) -> CompilationResult:
        """Compile the target project."""

    @abstractmethod
    def run_tests(self, env: Mapping[str, str] | None = None) -> TestRunResult:
        """Run the complete test suite."""

    @abstractmethod
    def run_test_case(self, test_case_id: str, env: Mapping[str, str] | None = None) -> TestRunResult:
        """Run a single test case."""

    @abstractmethod
    def discover_tests(self) -> list[TestCase]:
        """Return discovered test cases when available."""

    @abstractmethod
    def discover_source_roots(self) -> list[Path]:
        """Return the source roots that contain production code."""

    @abstractmethod
    def discover_test_roots(self) -> list[Path]:
        """Return the source roots that contain tests."""

    def test_runtime_classpath(self) -> str | None:
        """Return a classpath suitable for direct JUnit execution, when available."""
        return None

    def validate_environment(self) -> bool:
        return self.project_path.exists()

    def describe(self) -> dict[str, object]:
        return {
            "dataset": self.spec.dataset,
            "project_path": str(self.project_path),
            "project_id": self.spec.project_id,
            "bug_id": self.spec.bug_id,
            "exists": self.project_path.exists(),
            "environment_valid": self.validate_environment(),
            "source_roots": [str(path) for path in self.discover_source_roots()],
            "test_roots": [str(path) for path in self.discover_test_roots()],
            "discovered_tests": len(self.discover_tests()),
        }
