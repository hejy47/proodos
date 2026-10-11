from __future__ import annotations

import shutil
from pathlib import Path
from typing import Mapping

from src.models import CompilationResult, ProjectSpec, TestCase, TestRunResult
from src.project.base_project import Project
from src.java_runtime.runner_cli import runner_command_env
from src.utils.cmd_util import run_command, run_test_command
from src.utils.java_source import discover_package_prefixes
from src.utils.java_helper import discover_compiled_test_cases
from src.utils.java_util import (
    discover_compiled_test_artifact_roots,
    discover_java_source_roots,
    discover_java_test_roots,
    parse_defects4j_test_listing,
    parse_defects4j_failing_tests,
    parse_junitcore_output,
    parse_properties_file,
    split_test_id,
)


class Defects4JProject(Project):
    def __init__(self, spec: ProjectSpec):
        super().__init__(spec)
        self.properties = parse_properties_file(self.project_path / "defects4j.build.properties")
        self._cp_test: str | None = None

    def _command_env(self, env: Mapping[str, str] | None = None) -> dict[str, str] | None:
        return runner_command_env(
            project_path=self.project_path,
            dataset=self.spec.dataset,
            project_id=self.spec.project_id,
            base_env=env,
        )

    def compile(self, env: Mapping[str, str] | None = None) -> CompilationResult:
        result = run_command(["defects4j", "compile"], cwd=self.project_path, env=self._command_env(env))
        return CompilationResult(
            success=result.succeeded,
            errors=[] if result.succeeded else [result.stderr.strip() or result.stdout.strip() or "Compilation failed"],
            command=result.command,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    def run_tests(self, env: Mapping[str, str] | None = None) -> TestRunResult:
        result = run_test_command(["defects4j", "test"], cwd=self.project_path, env=self._command_env(env))
        failing_tests = parse_defects4j_failing_tests(self.project_path, result.stdout, result.stderr)
        return TestRunResult(
            success=result.succeeded and not failing_tests,
            passed=None,
            failed=len(failing_tests),
            errors=0 if result.return_code in (0, 1) else 1,
            failing_tests=failing_tests,
            execution_time=result.duration_seconds,
            stdout=result.stdout,
            stderr=result.stderr,
            command=result.command,
        )

    def run_test_case(self, test_case_id: str, env: Mapping[str, str] | None = None) -> TestRunResult:
        class_name, method_name = split_test_id(test_case_id)
        if method_name == "*" and ("::" not in test_case_id and "#" not in test_case_id):
            return self._run_test_class(class_name, env=env)
        result = run_test_command(["defects4j", "test", "-t", test_case_id], cwd=self.project_path, env=env)
        failing_tests = parse_defects4j_failing_tests(self.project_path, result.stdout, result.stderr)
        return TestRunResult(
            success=result.succeeded and not failing_tests,
            passed=None,
            failed=len(failing_tests),
            errors=0 if result.return_code in (0, 1) else 1,
            failing_tests=failing_tests,
            execution_time=result.duration_seconds,
            stdout=result.stdout,
            stderr=result.stderr,
            command=result.command,
        )

    def discover_source_roots(self) -> list[Path]:
        if "d4j.dir.src.classes" in self.properties:
            candidate = self.project_path / self.properties["d4j.dir.src.classes"]
            return [candidate] if candidate.exists() else []
        return discover_java_source_roots(self.project_path)

    def discover_test_roots(self) -> list[Path]:
        if "d4j.dir.src.tests" in self.properties:
            candidate = self.project_path / self.properties["d4j.dir.src.tests"]
            return [candidate] if candidate.exists() else []
        return discover_java_test_roots(self.project_path)

    def discover_tests(self) -> list[TestCase]:
        runtime_classpath = self.test_runtime_classpath()
        source_roots = self.discover_test_roots()
        test_includes = _includes_pattern(discover_package_prefixes(source_roots))
        self.compile()
        # A fresh checkout has no test output directories until compilation.
        artifact_roots = self._compiled_test_artifact_roots()
        return discover_compiled_test_cases(
            self.project_path,
            artifact_roots,
            runtime_classpath=runtime_classpath,
            source_roots=source_roots,
            includes=test_includes,
        )

    def test_runtime_classpath(self) -> str | None:
        return self._test_classpath()

    def discover_execution_tests(self) -> list[TestCase]:
        discovered_tests = self.discover_tests()
        if not discovered_tests:
            return discovered_tests

        trigger_test_ids = set(self._export_test_listing("tests.trigger"))
        execution_tests: list[TestCase] = []
        for discovered in discovered_tests:
            metadata = dict(discovered.metadata)
            if discovered.test_id in trigger_test_ids:
                metadata["trigger_test"] = True
                metadata["trigger_source"] = "defects4j export -p tests.trigger"
            execution_tests.append(
                TestCase(
                    test_id=discovered.test_id,
                    class_name=discovered.class_name,
                    method_name=discovered.method_name,
                    file_path=discovered.file_path,
                    failure_message=discovered.failure_message,
                    stack_trace=discovered.stack_trace,
                    metadata=metadata,
                )
            )
        return execution_tests

    def validate_environment(self) -> bool:
        return self.project_path.exists() and shutil.which("defects4j") is not None

    def describe(self) -> dict[str, object]:
        metadata = super().describe()
        metadata.update(
            {
                "defects4j_properties_file": str(self.project_path / "defects4j.build.properties"),
                "properties_loaded": bool(self.properties),
            }
        )
        return metadata

    def _run_test_class(self, class_name: str, env: Mapping[str, str] | None = None) -> TestRunResult:
        classpath = self._test_classpath()
        if not classpath:
            return TestRunResult(
                success=False,
                passed=0,
                failed=0,
                errors=1,
                stderr="Defects4J test classpath could not be resolved",
            )

        command = ["java", "-Djava.awt.headless=true", "-cp", classpath, "org.junit.runner.JUnitCore", class_name]
        result = run_test_command(command, cwd=self.project_path, env=env)
        summary = parse_junitcore_output(result.stdout, result.stderr)
        passed = max(summary.total - summary.failures - summary.errors, 0)
        return TestRunResult(
            success=result.return_code == 0 and summary.failures == 0 and summary.errors == 0,
            passed=passed,
            failed=summary.failures,
            errors=summary.errors if result.return_code in (0, 1) else max(summary.errors, 1),
            failing_tests=summary.failing_tests,
            execution_time=result.duration_seconds,
            stdout=result.stdout,
            stderr=result.stderr,
            command=result.command,
        )

    def _test_classpath(self) -> str | None:
        if self._cp_test is not None:
            return self._cp_test
        export_result = run_command(["defects4j", "export", "-p", "cp.test"], cwd=self.project_path)
        candidates = parse_defects4j_test_listing(export_result.stdout, export_result.stderr)
        self._cp_test = candidates[0] if candidates else None
        self.compile()
        return self._cp_test

    def _compiled_test_artifact_roots(self) -> list[Path]:
        preferred_candidates: list[Path] = []
        candidate = self.properties.get("d4j.dir.bin.tests")
        if candidate:
            resolved = self.project_path / candidate
            preferred_candidates.append(resolved)
        else:
            for test_dir in self._export_test_listing("dir.bin.tests"):
                resolved = self.project_path / test_dir
                preferred_candidates.append(resolved)

        preferred_candidates.extend(
            [
                self.project_path / "target/test-classes",
                self.project_path / "build/test-classes",
                self.project_path / "build-tests",
                self.project_path / "build/classes/java/test",
                self.project_path / "build/classes/kotlin/test",
                self.project_path / "build/classes/groovy/test",
                self.project_path / "build/classes/test",
            ]
        )
        return discover_compiled_test_artifact_roots(
            self.project_path,
            preferred_candidates=preferred_candidates,
        )

    def _compiled_source_artifact_roots(self) -> list[Path]:
        preferred_candidates: list[Path] = []
        candidate = self.properties.get("d4j.dir.bin.classes")
        if candidate:
            preferred_candidates.append(self.project_path / candidate)
        else:
            for source_dir in self._export_test_listing("dir.bin.classes"):
                preferred_candidates.append(self.project_path / source_dir)

        preferred_candidates.extend(
            [
                self.project_path / "target/classes",
                self.project_path / "build/classes",
                self.project_path / "build/classes/java/main",
                self.project_path / "build/classes/kotlin/main",
                self.project_path / "build/classes/groovy/main",
                self.project_path / "bin",
                self.project_path / "classes",
            ]
        )
        return _existing_unique(preferred_candidates)

    def _first_available_test_listing(self, property_names: tuple[str, ...]) -> tuple[list[str], str | None]:
        for property_name in property_names:
            listing = self._export_test_listing(property_name)
            if listing:
                return listing, property_name
        return [], None

    def _export_test_listing(self, property_name: str) -> list[str]:
        export_result = run_command(["defects4j", "export", "-p", property_name], cwd=self.project_path)
        return parse_defects4j_test_listing(export_result.stdout, export_result.stderr)


def _includes_pattern(prefixes: list[str]) -> str:
    cleaned = [prefix.strip().rstrip(".*") for prefix in prefixes if prefix.strip()]
    return ":".join(f"{prefix}.*" for prefix in cleaned) if cleaned else "*"


def _existing_unique(candidates: list[Path]) -> list[Path]:
    roots: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if not candidate.exists():
            continue
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        roots.append(resolved)
    return roots
