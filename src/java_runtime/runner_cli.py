from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Mapping

from config import PROJECT_ROOT


JUNIT_JAR = PROJECT_ROOT / "lib" / "junit.jar"
HAMCREST_CORE_JAR = PROJECT_ROOT / "lib" / "hamcrest-core.jar"
TEST_RUNNER_JAR = PROJECT_ROOT / "test_runner" / "target" / "proodos-test-runner.jar"
RUNNER_REQUIRED_RUNTIME_JARS = (JUNIT_JAR, HAMCREST_CORE_JAR)
TEST_RUNNER_MAIN_CLASS = "proodos.runner.TestRunnerMain"
DEFECTS4J_TIMEZONE = "America/Los_Angeles"


def test_runner_classpath(*entries: str | Path | None) -> str:
    """Build the runner classpath: pinned JUnit first, project classpath, runner jar last."""
    classpath_entries: list[str] = [str(path) for path in RUNNER_REQUIRED_RUNTIME_JARS]
    for entry in entries:
        if entry is None:
            continue
        value = str(entry).strip()
        if value:
            classpath_entries.append(value)
    classpath_entries.append(str(TEST_RUNNER_JAR))
    return os.pathsep.join(_deduplicate_classpath_entries(classpath_entries))


def missing_test_runner_classpath_jars() -> list[Path]:
    required_jars = [*RUNNER_REQUIRED_RUNTIME_JARS, TEST_RUNNER_JAR]
    return [path for path in required_jars if not path.is_file()]


def runner_command_env(
    *,
    project_path: Path | None = None,
    dataset: str | None = None,
    project_id: str | None = None,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str] | None:
    env = dict(base_env or os.environ)
    if _is_defects4j_context(project_path=project_path, dataset=dataset):
        env["TZ"] = DEFECTS4J_TIMEZONE
    vul4j_java_home = _vul4j_java_home(project_path, dataset)
    if vul4j_java_home is not None and vul4j_java_home.is_dir():
        env["JAVA_HOME"] = str(vul4j_java_home)
        env["PATH"] = f"{vul4j_java_home / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    elif _needs_java8(project_id=project_id, project_path=project_path):
        java8_home = Path("/usr/lib/jvm/java-8-openjdk-amd64")
        if java8_home.is_dir():
            env["JAVA_HOME"] = str(java8_home)
            env["PATH"] = f"{java8_home / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    return env or None


def _vul4j_java_home(project_path: Path | None, dataset: str | None) -> Path | None:
    if project_path is None:
        return None
    info_path = project_path / "VUL4J" / "vulnerability_info.json"
    if not info_path.is_file() and (dataset or "").strip().lower() != "vul4j":
        return None
    if not info_path.is_file():
        return None
    try:
        compliance = int(json.loads(info_path.read_text(encoding="utf-8")).get("compliance_level", 8))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if compliance <= 8:
        # The bundled Proodos test runner targets Java 8 bytecode; JDK 8 can
        # execute older Vul4J projects compiled for Java 7.
        return Path("/usr/lib/jvm/java-8-openjdk-amd64")
    if compliance == 11:
        return Path("/usr/lib/jvm/java-11-openjdk-amd64")
    return Path("/usr/lib/jvm/zulu-16")


def _deduplicate_classpath_entries(entries: list[str]) -> list[str]:
    deduplicated: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if entry in seen:
            continue
        seen.add(entry)
        deduplicated.append(entry)
    return deduplicated


def _is_defects4j_context(*, project_path: Path | None, dataset: str | None) -> bool:
    if dataset is not None and dataset.lower() == "defects4j":
        return True
    return project_path is not None and (project_path / "defects4j.build.properties").is_file()


def _needs_java8(*, project_id: str | None, project_path: Path | None) -> bool:
    if project_id is not None and project_id.lower() == "time":
        return True
    if project_path is not None:
        properties_path = project_path / "defects4j.build.properties"
        if properties_path.is_file():
            for raw_line in properties_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if raw_line.startswith("pid="):
                    if raw_line.split("=", 1)[1].strip().lower() == "time":
                        return True
                    break
        if _declares_legacy_javac_source(project_path):
            return True
    return False


_LEGACY_JAVAC_SOURCE_RE = re.compile(
    r"""(?:source\s*=\s*['"]1\.[56]['"])"""
    r"""|(?:<maven\.compiler\.source>\s*1\.[56]\s*</maven\.compiler\.source>)""",
    re.IGNORECASE,
)


def _declares_legacy_javac_source(project_path: Path) -> bool:
    """True when the project build asks for javac source/target 1.5 or 1.6.

    JDK 11+ rejects those options; intervention/preprocess must use Java 8.
    """
    for name in ("build.xml", "pom.xml"):
        path = project_path / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _LEGACY_JAVAC_SOURCE_RE.search(text):
            return True
    return False
