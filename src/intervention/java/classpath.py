from __future__ import annotations

from pathlib import Path

from src.java_runtime.runner_cli import (
    missing_test_runner_classpath_jars,
    test_runner_classpath,
)


def intervention_classpath(project_classpath: str | None) -> str:
    """Classpath for Java source intervention and observation via TestRunner."""
    missing = missing_intervention_classpath_jars()
    if missing:
        raise FileNotFoundError(
            "Missing test runner jars: " + ", ".join(str(path) for path in missing)
        )
    return test_runner_classpath(project_classpath)


def missing_intervention_classpath_jars() -> list[Path]:
    return missing_test_runner_classpath_jars()
