from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from config import PROJECT_ROOT
from src.utils.cmd_util import run_command


TEST_RUNNER_PROJECT_DIR = PROJECT_ROOT / "test_runner"
TEST_RUNNER_POM_PATH = TEST_RUNNER_PROJECT_DIR / "pom.xml"
TEST_RUNNER_JAR_PATH = TEST_RUNNER_PROJECT_DIR / "target" / "proodos-test-runner.jar"


@dataclass(frozen=True)
class TestRunnerBuildResult:
    success: bool
    runner_jar_path: Path
    command: list[str]
    stdout: str
    stderr: str
    reused_existing: bool


def build_test_runner() -> TestRunnerBuildResult:
    if TEST_RUNNER_JAR_PATH.is_file():
        return TestRunnerBuildResult(
            success=True,
            runner_jar_path=TEST_RUNNER_JAR_PATH,
            command=[],
            stdout="",
            stderr="",
            reused_existing=True,
        )

    command = [
        "mvn",
        "-q",
        "-f",
        str(TEST_RUNNER_POM_PATH),
        "-DskipTests",
        "package",
    ]
    result = run_command(command, cwd=PROJECT_ROOT)
    success = result.succeeded and TEST_RUNNER_JAR_PATH.is_file()
    return TestRunnerBuildResult(
        success=success,
        runner_jar_path=TEST_RUNNER_JAR_PATH,
        command=result.command,
        stdout=result.stdout,
        stderr=result.stderr,
        reused_existing=False,
    )
