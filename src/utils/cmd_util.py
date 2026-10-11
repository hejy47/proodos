from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


DEFAULT_TEST_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class CommandResult:
    command: list[str]
    cwd: Path
    return_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    started_at: float
    finished_at: float

    @property
    def succeeded(self) -> bool:
        return self.return_code == 0


def get_test_timeout_seconds(env: Mapping[str, str] | None = None) -> int:
    value = (env or {}).get(
        "PROODOS_TEST_TIMEOUT_SECONDS",
        os.environ.get("PROODOS_TEST_TIMEOUT_SECONDS", str(DEFAULT_TEST_TIMEOUT_SECONDS)),
    )
    try:
        timeout = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("PROODOS_TEST_TIMEOUT_SECONDS must be a positive integer") from exc
    if timeout <= 0:
        raise ValueError("PROODOS_TEST_TIMEOUT_SECONDS must be a positive integer")
    return timeout


def run_test_command(
    command: Sequence[str],
    cwd: Path,
    timeout_seconds: int | None = None,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    """Bound each test command, including all tests in a batch or regression."""
    timeout = get_test_timeout_seconds(env) if timeout_seconds is None else timeout_seconds
    if timeout <= 0:
        raise ValueError("Test command timeout must be positive")
    return run_command(command, cwd=cwd, timeout_seconds=timeout, env=env)


def run_command(
    command: Sequence[str],
    cwd: Path,
    timeout_seconds: int | None = None,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    started_at = time.time()
    resolved_env = {**os.environ, **dict(env)} if env is not None else None
    try:
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            env=resolved_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        finished_at = time.time()
        return CommandResult(
            command=list(command),
            cwd=cwd,
            return_code=127,
            stdout="",
            stderr=str(exc),
            duration_seconds=finished_at - started_at,
            started_at=started_at,
            finished_at=finished_at,
        )

    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
        finished_at = time.time()
        return CommandResult(
            command=list(command),
            cwd=cwd,
            return_code=process.returncode,
            stdout=_decode_output(stdout),
            stderr=_decode_output(stderr),
            duration_seconds=finished_at - started_at,
            started_at=started_at,
            finished_at=finished_at,
        )
    except subprocess.TimeoutExpired:
        _terminate_process_group(process)
        stdout, stderr = process.communicate()
        finished_at = time.time()
        return CommandResult(
            command=list(command),
            cwd=cwd,
            return_code=124,
            stdout=_decode_output(stdout),
            stderr=(f"Command timed out after {timeout_seconds} seconds"
                    + ("\n" + _decode_output(stderr) if stderr else "")),
            duration_seconds=finished_at - started_at,
            started_at=started_at,
            finished_at=finished_at,
        )


def _decode_output(payload: str | bytes | None) -> str:
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    return payload or ""


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    # The session/process group created by Popen keeps this ID even if the
    # leader exits before its children close the captured output pipes.
    process_group_id = process.pid
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        return

    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass

    # A terminated wrapper does not imply its Java/test subprocesses exited.
    # Kill remaining group members before waiting for stdout/stderr to close.
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        return

    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
