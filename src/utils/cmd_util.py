from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


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
            preexec_fn=os.setsid,
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
            stderr=_decode_output(stderr) or f"Command timed out after {timeout_seconds} seconds",
            duration_seconds=finished_at - started_at,
            started_at=started_at,
            finished_at=finished_at,
        )


def _decode_output(payload: str | bytes | None) -> str:
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    return payload or ""


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    try:
        process_group_id = os.getpgid(process.pid)
    except ProcessLookupError:
        return

    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        return

    try:
        process.wait(timeout=1)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        return

    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
