from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

from src.models import CompilationResult, ProjectSpec, TestCase, TestOutcome, TestRunResult
from src.project.base_project import Project
from src.project.kernel_tree import (
    KernelTree,
    resolve_kernel_report_path,
    resolve_kernel_syz_path,
    seed_relpaths_for_case,
)


class CoHikerProject(Project):
    """CoHiker Linux-kernel case adapter.

    ``project_path`` is the CoHiker dataset root (contains ``datasets/``).
    ``bug_id`` is the syzkaller case id. Kernel sources are read from
    ``COHIKER_LINUX_DIR`` or the ``cohiker`` docker container.
    """

    def __init__(self, spec: ProjectSpec, *, linux_dir: Path | None = None, docker_container: str | None = "cohiker"):
        super().__init__(spec)
        self.dataset_root = spec.project_path
        self.case_id = spec.bug_id or spec.project_id
        if linux_dir is None and os.environ.get("COHIKER_LINUX_DIR"):
            linux_dir = Path(os.environ["COHIKER_LINUX_DIR"])
        if "COHIKER_DOCKER" in os.environ:
            docker_container = os.environ["COHIKER_DOCKER"] or None
        self.linux_dir = linux_dir
        self.docker_container = docker_container
        self.kernel = KernelTree(linux_dir=linux_dir, docker_container=docker_container)
        self._source_snapshot: Path | None = None

    def validate_environment(self) -> bool:
        if self.case_id is None:
            return False
        if resolve_kernel_syz_path(self.case_id, self.dataset_root) is None:
            return False
        return self.kernel.available()

    def compile(self, env: Mapping[str, str] | None = None) -> CompilationResult:
        return CompilationResult(success=True, command=[], stdout="CoHiker kernel is checked out and compiled before preprocessing")

    def run_tests(self, env: Mapping[str, str] | None = None) -> TestRunResult:
        tests = self.discover_tests()
        return TestRunResult(
            success=False,
            passed=0,
            failed=len(tests),
            errors=0,
            failing_tests=tests,
            stdout="",
            stderr="CoHiker reproduction is an external case artifact; preprocessing builds a static graph",
        )

    def run_test_case(self, test_case_id: str, env: Mapping[str, str] | None = None) -> TestRunResult:
        return self.run_tests(env)

    def discover_tests(self) -> list[TestCase]:
        if not self.case_id:
            return []
        syz_path = resolve_kernel_syz_path(self.case_id, self.dataset_root)
        report_path = resolve_kernel_report_path(self.case_id, self.dataset_root)
        stack = (
            report_path.read_text(encoding="utf-8", errors="replace")
            if report_path is not None and report_path.is_file()
            else ""
        )
        return [
            TestCase(
                test_id=self.case_id,
                class_name="syzkaller",
                method_name=self.case_id,
                file_path=syz_path if syz_path is not None and syz_path.is_file() else None,
                failure_message="kernel oops",
                stack_trace=stack,
                metadata={
                    "syz_path": str(syz_path) if syz_path is not None else "",
                    "outcome": TestOutcome.FAIL.value,
                },
            )
        ]

    def discover_source_roots(self) -> list[Path]:
        if self._source_snapshot is not None and self._source_snapshot.is_dir():
            return [self._source_snapshot]
        return []

    def discover_test_roots(self) -> list[Path]:
        testcases = self.dataset_root / "datasets" / "testcases"
        return [testcases] if testcases.is_dir() else []

    def materialize_source_snapshot(self, dest_root: Path) -> list[Path]:
        if not self.case_id:
            return []
        rel_paths = seed_relpaths_for_case(
            case_id=self.case_id,
            dataset_root=self.dataset_root,
            kernel=self.kernel,
        )
        written = self.kernel.materialize(rel_paths, dest_root)
        self._source_snapshot = dest_root
        return written

    def describe(self) -> dict[str, object]:
        payload = super().describe()
        payload.update(
            {
                "case_id": self.case_id,
                "dataset_root": str(self.dataset_root),
                "linux_dir": str(self.linux_dir) if self.linux_dir else None,
                "docker_container": self.docker_container,
                "kernel_available": self.kernel.available(),
            }
        )
        return payload
