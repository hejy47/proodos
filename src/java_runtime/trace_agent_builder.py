from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from config import PROJECT_ROOT
from src.utils.cmd_util import run_command


TRACE_AGENT_PROJECT_DIR = PROJECT_ROOT / "trace_agent"
TRACE_AGENT_POM_PATH = TRACE_AGENT_PROJECT_DIR / "pom.xml"
TRACE_AGENT_JAR_PATH = TRACE_AGENT_PROJECT_DIR / "target" / "causalfl-trace-agent.jar"


@dataclass(frozen=True)
class TraceAgentBuildResult:
    success: bool
    agent_jar_path: Path
    command: list[str]
    stdout: str
    stderr: str
    reused_existing: bool


def build_trace_agent(*, force: bool = False) -> TraceAgentBuildResult:
    if not force and TRACE_AGENT_JAR_PATH.is_file():
        jar_mtime = TRACE_AGENT_JAR_PATH.stat().st_mtime
        src_root = TRACE_AGENT_PROJECT_DIR / "src"
        newest_src = max(
            (path.stat().st_mtime for path in src_root.rglob("*.java")),
            default=0.0,
        )
        if newest_src <= jar_mtime:
            return TraceAgentBuildResult(
                success=True,
                agent_jar_path=TRACE_AGENT_JAR_PATH,
                command=[],
                stdout="",
                stderr="",
                reused_existing=True,
            )

    command = [
        "mvn",
        "-q",
        "-f",
        str(TRACE_AGENT_POM_PATH),
        "-DskipTests",
        "package",
    ]
    result = run_command(command, cwd=PROJECT_ROOT)
    success = result.succeeded and TRACE_AGENT_JAR_PATH.is_file()
    return TraceAgentBuildResult(
        success=success,
        agent_jar_path=TRACE_AGENT_JAR_PATH,
        command=result.command,
        stdout=result.stdout,
        stderr=result.stderr,
        reused_existing=False,
    )
