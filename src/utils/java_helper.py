from __future__ import annotations

import tempfile
from pathlib import Path

from src.java_runtime.runner_cli import (
    TEST_RUNNER_MAIN_CLASS,
    missing_test_runner_classpath_jars,
    runner_command_env,
    test_runner_classpath,
)
from src.java_runtime.test_runner_builder import build_test_runner
from src.models import TestCase
from src.utils.cmd_util import run_command


def discover_related_test_cases(
    project_path: Path,
    artifact_roots: list[Path],
    *,
    runtime_classpath: str | None,
    source_roots: list[Path] | None = None,
    includes: str = "*",
    production_artifact_roots: list[Path] | None = None,
    production_source_roots: list[Path] | None = None,
    source_includes: str | None = None,
) -> list[TestCase]:
    return discover_compiled_test_cases(
        project_path,
        artifact_roots,
        runtime_classpath=runtime_classpath,
        source_roots=source_roots,
        includes=includes,
    )


def discover_compiled_test_cases(
    project_path: Path,
    artifact_roots: list[Path],
    *,
    runtime_classpath: str | None,
    source_roots: list[Path] | None = None,
    includes: str = "*",
) -> list[TestCase]:
    resolved_artifact_roots = [root.resolve() for root in artifact_roots if root.exists()]
    if not resolved_artifact_roots:
        raise RuntimeError("No compiled test artifacts are available for test discovery")
    if not runtime_classpath or not runtime_classpath.strip():
        raise RuntimeError("No Java test runtime classpath is available for test discovery")
    runner_build = build_test_runner()
    if not runner_build.success:
        raise RuntimeError("Building the CausalFL test runner failed")
    missing_jars = missing_test_runner_classpath_jars()
    if missing_jars:
        raise RuntimeError(
            f"Test runner classpath jars are not available: {', '.join(str(path) for path in missing_jars)}"
        )

    resolved_source_roots = [root.resolve() for root in source_roots or [] if root.exists()]
    test_includes = includes if includes.strip() else "*"
    command_env = runner_command_env(project_path=project_path)
    with tempfile.TemporaryDirectory(prefix="causalfl-test-discovery-") as temp_dir:
        discovered: list[TestCase] = []
        for index, root in enumerate(resolved_artifact_roots):
            output_file = Path(temp_dir) / f"tests-{index}.txt"
            command = [
                "java",
                "-cp",
                test_runner_classpath(runtime_classpath, root),
                TEST_RUNNER_MAIN_CLASS,
                "discoverTests",
                str(root),
                "--outputFile",
                str(output_file),
                "--includes",
                test_includes,
            ]
            result = run_command(command, cwd=project_path, env=command_env)
            if not result.succeeded:
                details = result.stderr.strip() or result.stdout.strip() or "test discovery failed"
                raise RuntimeError(f"Failed to discover Java tests with the CausalFL runner: {details}")
            if not output_file.exists():
                raise RuntimeError("Test discovery did not produce an output file")
            discovered.extend(_parse_discovered_tests(output_file, resolved_source_roots))
    return _deduplicate_tests(discovered)


def discover_test_methods_with_helper(
    *,
    project_path: Path,
    artifact_roots: list[Path],
    runtime_classpath: str | None,
    source_roots: list[Path] | None = None,
    includes: str = "*",
    production_artifact_roots: list[Path] | None = None,
    production_source_roots: list[Path] | None = None,
    source_includes: str | None = None,
) -> list[TestCase]:
    return discover_related_test_cases(
        project_path,
        artifact_roots,
        runtime_classpath=runtime_classpath,
        source_roots=source_roots,
        includes=includes,
        production_artifact_roots=production_artifact_roots,
        production_source_roots=production_source_roots,
        source_includes=source_includes,
    )


def _parse_discovered_tests(output_file: Path, source_roots: list[Path]) -> list[TestCase]:
    tests: list[TestCase] = []
    for raw_line in output_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        framework, identifier = line.split(",", 1)
        class_name, method_name = identifier.split("#", 1)
        tests.append(
            TestCase(
                test_id=f"{class_name}::{method_name}",
                class_name=class_name,
                method_name=method_name,
                file_path=_resolve_source_file(class_name, source_roots),
                metadata={
                    "framework": framework,
                    "discovery": "causalfl_runner",
                    "discovery_source": "compiled_artifacts",
                },
            )
        )
    return tests


def _deduplicate_tests(tests: list[TestCase]) -> list[TestCase]:
    deduplicated: list[TestCase] = []
    seen: set[str] = set()
    for test_case in tests:
        if test_case.test_id in seen:
            continue
        seen.add(test_case.test_id)
        deduplicated.append(test_case)
    return deduplicated


def _resolve_source_file(class_name: str, source_roots: list[Path]) -> Path | None:
    if not source_roots:
        return None

    top_level_name = class_name.split("$", 1)[0]
    relative_path = Path(*top_level_name.split(".")).with_suffix(".java")
    for source_root in source_roots:
        candidate = source_root / relative_path
        if candidate.exists():
            return candidate
    return None
