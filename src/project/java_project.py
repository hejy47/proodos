from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Mapping

from src.models import CompilationResult, ProjectSpec, TestCase, TestRunResult
from src.project.base_project import Project
from src.utils.cmd_util import run_command, run_test_command
from src.utils.java_helper import discover_compiled_test_cases
from src.utils.java_util import (
    GRADLE,
    MAVEN,
    build_runner_available,
    detect_build_tool,
    discover_compiled_test_artifact_roots,
    discover_java_source_roots,
    discover_java_test_roots,
    gradle_test_selector,
    maven_test_selector,
    parse_junit_report_directory,
    resolve_build_runner,
)


class JavaProject(Project):
    def __init__(self, spec: ProjectSpec, build_tool_hint: str | None = None):
        super().__init__(spec)
        self.build_tool = build_tool_hint or detect_build_tool(self.project_path)

    def compile(self, env: Mapping[str, str] | None = None) -> CompilationResult:
        if self.build_tool is None:
            return CompilationResult(
                success=False,
                errors=["No supported Java build tool found. Expected pom.xml or build.gradle."],
            )

        command = self._build_compile_command()
        result = run_command(command, cwd=self.project_path, env=env)
        return CompilationResult(
            success=result.succeeded,
            errors=[] if result.succeeded else [result.stderr.strip() or result.stdout.strip() or "Compilation failed"],
            command=result.command,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    def run_tests(self, env: Mapping[str, str] | None = None) -> TestRunResult:
        if self.build_tool is None:
            return self._unsupported_result("No supported Java build tool found. Expected pom.xml or build.gradle.")

        command = self._build_test_command()
        result = run_test_command(command, cwd=self.project_path, env=env)
        summary = self._parse_test_reports(result.started_at - 2.0)
        return self._to_test_run_result(result, summary)

    def run_test_case(self, test_case_id: str, env: Mapping[str, str] | None = None) -> TestRunResult:
        if self.build_tool is None:
            return self._unsupported_result("No supported Java build tool found. Expected pom.xml or build.gradle.")

        command = self._build_single_test_command(test_case_id)
        result = run_test_command(command, cwd=self.project_path, env=env)
        summary = self._parse_test_reports(result.started_at - 2.0)
        return self._to_test_run_result(result, summary)

    def discover_tests(self) -> list[TestCase]:
        artifact_roots = self._compiled_test_artifact_roots()
        # Static preprocessing and debugging run on a clean Vul4J checkout,
        # before Maven has created target/test-classes. No compiled tests are
        # discoverable yet; let the later debug tools build what they
        # need instead of failing while describing the project.
        if not artifact_roots:
            return []
        runtime_classpath = self.test_runtime_classpath()
        if not runtime_classpath or not runtime_classpath.strip():
            # Some older Maven installations cannot run the newest exec plugin.
            # Test discovery is optional metadata during preprocessing and debugging.
            return []
        return discover_compiled_test_cases(
            self.project_path,
            artifact_roots,
            runtime_classpath=runtime_classpath,
            source_roots=self.discover_test_roots(),
        )

    def discover_source_roots(self) -> list[Path]:
        return discover_java_source_roots(self.project_path)

    def discover_test_roots(self) -> list[Path]:
        return discover_java_test_roots(self.project_path)

    def test_runtime_classpath(self) -> str | None:
        return self._resolve_test_runtime_classpath_from_build()

    def validate_environment(self) -> bool:
        return (
            self.project_path.exists()
            and self.build_tool is not None
            and build_runner_available(self.project_path, self.build_tool)
        )

    def describe(self) -> dict[str, object]:
        metadata = super().describe()
        metadata.update(
            {
                "build_tool": self.build_tool,
                "build_files": [
                    str(path)
                    for path in (
                        self.project_path / "pom.xml",
                        self.project_path / "build.gradle",
                        self.project_path / "build.gradle.kts",
                    )
                    if path.exists()
                ],
            }
        )
        return metadata

    def _compiled_test_artifact_roots(self) -> list[Path]:
        preferred_candidates: list[Path] = []
        if self.build_tool == MAVEN:
            preferred_candidates = [
                self.project_path / "target/test-classes",
            ]
        elif self.build_tool == GRADLE:
            preferred_candidates = [
                self.project_path / "build/classes/java/test",
                self.project_path / "build/classes/kotlin/test",
                self.project_path / "build/classes/groovy/test",
                self.project_path / "build/classes/test",
            ]
        return discover_compiled_test_artifact_roots(
            self.project_path,
            preferred_candidates=preferred_candidates,
        )

    def _resolve_test_runtime_classpath_from_build(self) -> str | None:
        if self.build_tool == MAVEN:
            return self._resolve_maven_test_runtime_classpath()
        if self.build_tool == GRADLE:
            return self._resolve_gradle_test_runtime_classpath()
        return None

    def _resolve_maven_test_runtime_classpath(self) -> str | None:
        runner = resolve_build_runner(self.project_path, self.build_tool)
        for plugin_version in ("3.5.0", "1.6.0"):
            result = run_command(
                [
                    *runner,
                    "-q",
                    "-Dexec.classpathScope=test",
                    "-Dexec.executable=echo",
                    "-Dexec.args=%classpath",
                    f"org.codehaus.mojo:exec-maven-plugin:{plugin_version}:exec",
                ],
                cwd=self.project_path,
            )
            if result.succeeded:
                classpath = _last_non_empty_line(result.stdout)
                if classpath:
                    return classpath
        return None

    def _resolve_gradle_test_runtime_classpath(self) -> str | None:
        runner = resolve_build_runner(self.project_path, self.build_tool)
        with tempfile.TemporaryDirectory(prefix="proodos-gradle-classpath-") as temp_dir:
            init_script = Path(temp_dir) / "init.gradle"
            init_script.write_text(
                """
gradle.projectsEvaluated {
    gradle.rootProject.tasks.create(name: "proodosPrintTestRuntimeClasspath") {
        doLast {
            def testProjects = rootProject.allprojects.findAll { candidate ->
                def sourceSets = candidate.hasProperty("sourceSets") ? candidate.sourceSets : null
                sourceSets != null && sourceSets.findByName("test") != null
            }
            if (testProjects.isEmpty()) {
                throw new GradleException("No test source set available")
            }
            testProjects.each { candidate ->
                def sourceSets = candidate.sourceSets
                println "PROODOS_TEST_CP=" + sourceSets.getByName("test").runtimeClasspath.asPath
            }
        }
    }
}
""".strip()
                + "\n",
                encoding="utf-8",
            )
            result = run_command(
                [
                    *runner,
                    "--quiet",
                    "-I",
                    str(init_script),
                    "proodosPrintTestRuntimeClasspath",
                ],
                cwd=self.project_path,
                timeout_seconds=300,
            )
        if not result.succeeded:
            return None
        marker = "PROODOS_TEST_CP="
        entries: list[str] = []
        seen: set[str] = set()
        for line in result.stdout.splitlines():
            text = line.strip()
            if not text.startswith(marker):
                continue
            for entry in text[len(marker):].split(os.pathsep):
                entry = entry.strip()
                if entry and entry not in seen:
                    seen.add(entry)
                    entries.append(entry)
        if entries:
            return os.pathsep.join(entries)
        return _last_non_empty_line(result.stdout)

    def _build_compile_command(self) -> list[str]:
        runner = resolve_build_runner(self.project_path, self.build_tool)
        if self.build_tool == MAVEN:
            return [*runner, "-q", "-DskipTests", "compile", "test-compile"]
        if self.build_tool == GRADLE:
            return [*runner, "--quiet", "testClasses"]
        raise ValueError(f"Unsupported build tool: {self.build_tool}")

    def _build_test_command(self) -> list[str]:
        runner = resolve_build_runner(self.project_path, self.build_tool)
        if self.build_tool == MAVEN:
            return [*runner, "-q", "test"]
        if self.build_tool == GRADLE:
            return [*runner, "--quiet", "test"]
        raise ValueError(f"Unsupported build tool: {self.build_tool}")

    def _build_single_test_command(self, test_case_id: str) -> list[str]:
        runner = resolve_build_runner(self.project_path, self.build_tool)
        if self.build_tool == MAVEN:
            selector = maven_test_selector(test_case_id)
            return [*runner, "-q", f"-Dtest={selector}", "test"]
        if self.build_tool == GRADLE:
            selector = gradle_test_selector(test_case_id)
            return [*runner, "--quiet", "test", "--tests", selector]
        raise ValueError(f"Unsupported build tool: {self.build_tool}")

    def _parse_test_reports(self, modified_after: float | None = None):
        report_dirs = []
        if self.build_tool == MAVEN:
            report_dirs.append(self.project_path / "target/surefire-reports")
        elif self.build_tool == GRADLE:
            report_dirs.append(self.project_path / "build/test-results/test")

        summary = None
        for report_dir in report_dirs:
            parsed = parse_junit_report_directory(report_dir, modified_after=modified_after)
            if summary is None:
                summary = parsed
            else:
                summary = type(parsed)(
                    total=summary.total + parsed.total,
                    failures=summary.failures + parsed.failures,
                    errors=summary.errors + parsed.errors,
                    skipped=summary.skipped + parsed.skipped,
                    failing_tests=[*summary.failing_tests, *parsed.failing_tests],
                )
        return summary

    def _to_test_run_result(self, command_result, junit_summary) -> TestRunResult:
        total = junit_summary.total if junit_summary is not None else 0
        failed = junit_summary.failures if junit_summary is not None else 0
        errors = junit_summary.errors if junit_summary is not None else 0
        if command_result.return_code not in (0, 1):
            errors = max(errors, 1)
        failing_tests = junit_summary.failing_tests if junit_summary is not None else []
        passed = max(total - failed - errors, 0)
        success = command_result.succeeded and failed == 0 and errors == 0
        return TestRunResult(
            success=success,
            passed=passed,
            failed=failed,
            errors=errors,
            failing_tests=failing_tests,
            execution_time=command_result.duration_seconds,
            stdout=command_result.stdout,
            stderr=command_result.stderr,
            command=command_result.command,
        )

    def _unsupported_result(self, message: str) -> TestRunResult:
        return TestRunResult(
            success=False,
            passed=0,
            failed=0,
            errors=1,
            stderr=message,
        )


def _last_non_empty_line(output: str) -> str | None:
    for line in reversed(output.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped
    return None
