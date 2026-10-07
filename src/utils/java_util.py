
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

from src.models import TestCase
from src.utils.java_source import discover_test_cases_in_file


MAVEN = "maven"
GRADLE = "gradle"

COMMON_COMPILED_TEST_ARTIFACT_PATHS = (
    "target/test-classes",
    "target/tests",
    "build/test-classes",
    "build-tests",
    "build/classes/java/test",
    "build/classes/kotlin/test",
    "build/classes/groovy/test",
    "build/classes/test",
    "out/test/classes",
    "out/test",
    "test-classes",
    "test-bin",
    "bin-tests",
)


@dataclass(frozen=True)
class JUnitReportSummary:
    total: int = 0
    failures: int = 0
    errors: int = 0
    skipped: int = 0
    failing_tests: list[TestCase] = field(default_factory=list)


def detect_build_tool(project_path: Path) -> str | None:
    if (project_path / "pom.xml").exists():
        return MAVEN
    if (project_path / "build.gradle").exists() or (project_path / "build.gradle.kts").exists():
        return GRADLE
    return None


def resolve_build_runner(project_path: Path, build_tool: str) -> list[str]:
    if build_tool == MAVEN:
        mvnw = project_path / "mvnw"
        return [str(mvnw)] if mvnw.exists() else ["mvn"]
    if build_tool == GRADLE:
        gradlew = project_path / "gradlew"
        return [str(gradlew)] if gradlew.exists() else ["gradle"]
    raise ValueError(f"Unsupported build tool: {build_tool}")


def build_runner_available(project_path: Path, build_tool: str) -> bool:
    runner = resolve_build_runner(project_path, build_tool)
    runner_path = Path(runner[0])
    if runner_path.is_absolute():
        return runner_path.exists()
    return shutil.which(runner[0]) is not None


def discover_java_source_roots(project_path: Path) -> list[Path]:
    candidates = (
        project_path / "src/main/java",
        project_path / "src/java",
        project_path / "source",
        project_path / "src",
    )
    return _reduce_nested_roots([path for path in candidates if path.exists()])


def discover_java_test_roots(project_path: Path) -> list[Path]:
    candidates = (
        project_path / "src/test/java",
        project_path / "src/test",
        project_path / "tests",
    )
    return _reduce_nested_roots([path for path in candidates if path.exists()])


def discover_compiled_test_artifact_roots(
    project_path: Path,
    *,
    preferred_candidates: list[Path] | None = None,
) -> list[Path]:
    discovered: list[Path] = []
    seen: set[Path] = set()
    candidates = [
        *(preferred_candidates or []),
        *(project_path / relative_path for relative_path in COMMON_COMPILED_TEST_ARTIFACT_PATHS),
    ]
    for candidate in candidates:
        if not candidate.exists():
            continue
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        discovered.append(candidate)
    return discovered


def discover_test_cases(project_path: Path, test_roots: list[Path]) -> list[TestCase]:
    test_cases: list[TestCase] = []
    for root in test_roots:
        for file_path in sorted(root.rglob("*.java")):
            test_cases.extend(discover_test_cases_in_file(file_path))
    return test_cases


def split_test_id(test_id: str) -> tuple[str, str]:
    if "::" in test_id:
        class_name, method_name = test_id.split("::", 1)
        return class_name, method_name
    if "#" in test_id:
        class_name, method_name = test_id.split("#", 1)
        return class_name, method_name
    return test_id, "*"


def maven_test_selector(test_id: str) -> str:
    class_name, method_name = split_test_id(test_id)
    if method_name == "*" or not method_name:
        return class_name
    return f"{class_name}#{method_name}"


def gradle_test_selector(test_id: str) -> str:
    class_name, method_name = split_test_id(test_id)
    if method_name == "*" or not method_name:
        return class_name
    return f"{class_name}.{method_name}"


def parse_junit_report_directory(report_dir: Path, modified_after: float | None = None) -> JUnitReportSummary:
    if not report_dir.exists():
        return JUnitReportSummary()

    summary = JUnitReportSummary()
    for report_file in sorted(report_dir.rglob("*.xml")):
        if modified_after is not None and report_file.stat().st_mtime < modified_after:
            continue
        summary = merge_junit_summaries(summary, parse_junit_xml_file(report_file))
    return summary


def parse_junit_xml_file(report_file: Path) -> JUnitReportSummary:
    try:
        root = ElementTree.parse(report_file).getroot()
    except ElementTree.ParseError:
        return JUnitReportSummary()

    if root.tag == "testsuite":
        testsuite_nodes = [root]
    elif root.tag == "testsuites":
        testsuite_nodes = list(root.findall("testsuite"))
    else:
        testsuite_nodes = []

    summary = JUnitReportSummary(
        total=sum(int(node.attrib.get("tests", "0")) for node in testsuite_nodes) or int(root.attrib.get("tests", "0")),
        failures=sum(int(node.attrib.get("failures", "0")) for node in testsuite_nodes) or int(root.attrib.get("failures", "0")),
        errors=sum(int(node.attrib.get("errors", "0")) for node in testsuite_nodes) or int(root.attrib.get("errors", "0")),
        skipped=sum(int(node.attrib.get("skipped", "0")) for node in testsuite_nodes) or int(root.attrib.get("skipped", "0")),
        failing_tests=[],
    )

    for testcase in root.iter("testcase"):
        failure_node = None
        failure_type = ""
        for child in testcase:
            if child.tag in {"failure", "error"}:
                failure_node = child
                failure_type = child.tag
                break

        if failure_node is None:
            continue

        class_name = testcase.attrib.get("classname", root.attrib.get("name", "unknown"))
        method_name = testcase.attrib.get("name", "unknown")
        message = failure_node.attrib.get("message", "").strip()
        stack_trace = (failure_node.text or "").strip()
        if not message and stack_trace:
            message = stack_trace.splitlines()[0].strip()
        summary.failing_tests.append(
            TestCase(
                test_id=f"{class_name}::{method_name}",
                class_name=class_name,
                method_name=method_name,
                failure_message=message,
                stack_trace=stack_trace,
                metadata={
                    "failure_type": failure_type,
                    "report_file": str(report_file),
                },
            )
        )
    return summary


def merge_junit_summaries(left: JUnitReportSummary, right: JUnitReportSummary) -> JUnitReportSummary:
    return JUnitReportSummary(
        total=left.total + right.total,
        failures=left.failures + right.failures,
        errors=left.errors + right.errors,
        skipped=left.skipped + right.skipped,
        failing_tests=[*left.failing_tests, *right.failing_tests],
    )


def parse_properties_file(properties_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not properties_path.exists():
        return values

    for raw_line in properties_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _reduce_nested_roots(paths: list[Path]) -> list[Path]:
    reduced: list[Path] = []
    for candidate in sorted({path.resolve() for path in paths}, key=lambda path: (len(path.parts), str(path))):
        if any(candidate.is_relative_to(existing) or existing.is_relative_to(candidate) for existing in reduced):
            if any(existing.is_relative_to(candidate) for existing in reduced):
                continue
            reduced = [existing for existing in reduced if not candidate.is_relative_to(existing)]
        reduced.append(candidate)
    return reduced


def parse_defects4j_failing_tests(project_path: Path, stdout: str = "", stderr: str = "") -> list[TestCase]:
    failing_tests_file = project_path / "failing_tests"
    if failing_tests_file.exists():
        return _parse_defects4j_failing_tests_file(failing_tests_file)
    return _parse_defects4j_failing_tests_from_output(stdout, stderr)


def parse_defects4j_test_listing(stdout: str, stderr: str = "") -> list[str]:
    combined = "\n".join(part for part in (stdout, stderr) if part)
    tests: list[str] = []
    for raw_line in combined.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("OpenJDK ") or line.startswith("Running ant ") or line == "OK":
            continue
        if any(ch.isspace() for ch in line):
            continue
        tests.append(line)
    return tests


_JUNITCORE_FAILURE_HEADER = re.compile(r"^\d+\)\s+(?P<method>[^()\s]+)\((?P<class_name>[^()]+)\)$")
_JUNITCORE_OK_SUMMARY = re.compile(r"^OK \((?P<total>\d+) tests?\)$")
_JUNITCORE_FAILURE_SUMMARY = re.compile(r"^Tests run:\s*(?P<total>\d+),\s*Failures:\s*(?P<failures>\d+)\s*$")


def parse_junitcore_output(stdout: str, stderr: str = "") -> JUnitReportSummary:
    summary = JUnitReportSummary()
    lines = stdout.splitlines()
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        ok_match = _JUNITCORE_OK_SUMMARY.match(line)
        if ok_match:
            summary = JUnitReportSummary(
                total=int(ok_match.group("total")),
                failures=0,
                errors=0,
                skipped=0,
                failing_tests=summary.failing_tests,
            )
            continue

        failure_match = _JUNITCORE_FAILURE_HEADER.match(line)
        if failure_match:
            class_name = failure_match.group("class_name").strip()
            method_name = failure_match.group("method").strip()
            message = ""
            stack_lines: list[str] = []
            for nested_line in lines[index + 1 :]:
                stripped = nested_line.rstrip()
                if not stripped:
                    if stack_lines:
                        break
                    continue
                if _JUNITCORE_FAILURE_HEADER.match(stripped.strip()) or _JUNITCORE_FAILURE_SUMMARY.match(stripped.strip()):
                    break
                if not message:
                    message = stripped.strip()
                stack_lines.append(stripped)
            summary.failing_tests.append(
                TestCase(
                    test_id=f"{class_name}::{method_name}",
                    class_name=class_name,
                    method_name=method_name,
                    failure_message=message,
                    stack_trace="\n".join(stack_lines).strip(),
                    metadata={"source": "junitcore"},
                )
            )
            continue

        failure_summary_match = _JUNITCORE_FAILURE_SUMMARY.match(line)
        if failure_summary_match:
            summary = JUnitReportSummary(
                total=int(failure_summary_match.group("total")),
                failures=int(failure_summary_match.group("failures")),
                errors=0,
                skipped=0,
                failing_tests=summary.failing_tests,
            )
    return summary


def _parse_defects4j_failing_tests_file(failing_tests_file: Path) -> list[TestCase]:
    entries: list[TestCase] = []
    current_test_id = ""
    current_lines: list[str] = []

    def flush_current() -> None:
        nonlocal current_test_id, current_lines
        if not current_test_id:
            return
        class_name, method_name = split_test_id(current_test_id)
        stack_trace = "\n".join(current_lines).strip()
        message = stack_trace.splitlines()[0].strip() if stack_trace else ""
        entries.append(
            TestCase(
                test_id=current_test_id,
                class_name=class_name,
                method_name=method_name,
                failure_message=message,
                stack_trace=stack_trace,
                metadata={"source": str(failing_tests_file)},
            )
        )
        current_test_id = ""
        current_lines = []

    for raw_line in failing_tests_file.read_text(encoding="utf-8").splitlines():
        if raw_line.startswith("--- "):
            flush_current()
            current_test_id = raw_line.removeprefix("--- ").strip()
            continue
        current_lines.append(raw_line)

    flush_current()
    return entries


def _parse_defects4j_failing_tests_from_output(stdout: str, stderr: str) -> list[TestCase]:
    combined = "\n".join(part for part in (stdout, stderr) if part)
    entries: list[TestCase] = []
    for raw_line in combined.splitlines():
        line = raw_line.strip()
        if not line or not line.startswith("- "):
            continue
        test_id = line.removeprefix("- ").strip()
        class_name, method_name = split_test_id(test_id)
        entries.append(
            TestCase(
                test_id=test_id,
                class_name=class_name,
                method_name=method_name,
            )
        )
    return entries
