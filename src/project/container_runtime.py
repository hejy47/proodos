"""Run project commands either through Docker or in the current container.

The host launcher normally invokes ``docker exec test1``.  Once the Python
process is already running in ``test1``, invoking Docker again is both
unnecessary and unavailable.  These small helpers keep that detail out of
the intervention and preprocessing code paths.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil


def in_target_container(container: str | None) -> bool:
    """Return whether *container* is the process's current container."""
    if not container:
        return False
    marker = os.environ.get("CAUSALFL_IN_CONTAINER", "").strip().lower()
    if marker in {"1", "true", "yes"}:
        return True
    linux_dir = os.environ.get("COHIKER_LINUX_DIR", "").strip()
    # The test1 image has the target tree but no Docker CLI.  Requiring both
    # properties avoids changing the normal host -> docker execution path.
    candidate = Path(linux_dir) if linux_dir else Path("/root/linux")
    return candidate.is_dir() and shutil.which("docker") is None


def exec_argv(container: str | None, *args: str) -> list[str]:
    """Build argv for a command in the target environment."""
    if in_target_container(container):
        return list(args)
    if not container:
        return list(args)
    return ["docker", "exec", container, *args]


def shell_argv(container: str | None, script: str) -> list[str]:
    return exec_argv(container, "bash", "-lc", script)


def copy_to(container: str | None, source: str | Path, destination: str) -> None:
    """Copy a host path into the target environment."""
    if in_target_container(container):
        dest = Path(destination)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        return
    if not container:
        raise RuntimeError("container is required for a remote copy")
    import subprocess

    subprocess.run(["docker", "cp", str(source), f"{container}:{destination}"], check=True)
