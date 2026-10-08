from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from src.models import ProjectSpec
from src.patching.java.models import TestOutcome
from src.java_runtime.runner_cli import TEST_RUNNER_MAIN_CLASS, runner_command_env
from src.project.java_project import JavaProject
from src.utils.cmd_util import run_command


RUNNER_OUTCOME_RE = re.compile(
    r"\[causalfl-runner\]\s+(\S+)\s+(PASS|FAIL|ERROR)\s+"
)


@dataclass(frozen=True)
class CompiledLayout:
    project_root: Path
    main_sources: list[Path]
    test_sources: list[Path]
    classes_dir: Path
    test_classes_dir: Path
    # Full project runtime CP (optional). When set, used instead of classes+test-classes only.
    project_classpath: str | None = None
    # Defects4J-style: production and test classes are already built.
    skip_main_compile: bool = False


def discover_java_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(root.rglob("*.java"))


def default_layout(project_root: Path) -> CompiledLayout:
    main_root = project_root / "src" / "main" / "java"
    test_root = project_root / "src" / "test" / "java"
    build = project_root / "build"
    return CompiledLayout(
        project_root=project_root,
        main_sources=discover_java_files(main_root),
        test_sources=discover_java_files(test_root),
        classes_dir=build / "classes",
        test_classes_dir=build / "test-classes",
    )


@lru_cache(maxsize=64)
def _maven_test_runtime_classpath(project_root: Path, pom_mtime_ns: int) -> str | None:
    """Resolve Maven's test classpath, including target output and dependencies."""
    del pom_mtime_ns  # Cache invalidates when the POM changes.
    project = JavaProject(
        ProjectSpec(dataset="maven", project_path=project_root),
        build_tool_hint="maven",
    )
    return project.test_runtime_classpath()


@lru_cache(maxsize=64)
def _gradle_test_runtime_classpath(project_root: Path, build_file_mtime_ns: int) -> str | None:
    """Resolve test classpaths and compiled outputs across Gradle subprojects."""
    del build_file_mtime_ns  # Cache invalidates when the root build file changes.
    project = JavaProject(
        ProjectSpec(dataset="gradle", project_path=project_root),
        build_tool_hint="gradle",
    )
    return project.test_runtime_classpath()


def resolve_layout(project_root: Path) -> CompiledLayout:
    """Detect Maven-style fixtures or Defects4J checkout layouts."""
    # Absolute root so compile -d / classpath never resolve against a nested cwd.
    project_root = Path(project_root).resolve()
    d4j_props = project_root / "defects4j.build.properties"
    if d4j_props.is_file():
        return _defects4j_layout(project_root, d4j_props)
    pom_path = project_root / "pom.xml"
    if pom_path.is_file():
        return CompiledLayout(
            project_root=project_root,
            main_sources=discover_java_files(project_root / "src" / "main" / "java"),
            test_sources=discover_java_files(project_root / "src" / "test" / "java"),
            classes_dir=project_root / "target" / "classes",
            test_classes_dir=project_root / "target" / "test-classes",
            project_classpath=_maven_test_runtime_classpath(
                project_root, pom_path.stat().st_mtime_ns
            ),
            # The debug patching stage owns compilation. Reusing its output avoids
            # recompiling third-party test trees with javac's default encoding.
            skip_main_compile=True,
        )
    gradle_path = next(
        (
            candidate
            for candidate in (project_root / "build.gradle", project_root / "build.gradle.kts")
            if candidate.is_file()
        ),
        None,
    )
    if gradle_path is not None:
        project_classpath = _gradle_test_runtime_classpath(
            project_root, gradle_path.stat().st_mtime_ns
        )
        main_roots = _discover_gradle_java_source_roots(project_root, "main")
        test_roots = _discover_gradle_java_source_roots(project_root, "test")
        return CompiledLayout(
            project_root=project_root,
            main_sources=sorted(
                source
                for source_root in main_roots
                for source in discover_java_files(source_root)
            ),
            test_sources=sorted(
                source
                for source_root in test_roots
                for source in discover_java_files(source_root)
            ),
            classes_dir=project_root / "build" / "classes" / "java" / "main",
            test_classes_dir=project_root / "build" / "classes" / "java" / "test",
            project_classpath=project_classpath,
            skip_main_compile=bool(project_classpath),
        )
    # Common non-Maven Java layouts (source/ + tests/)
    if (project_root / "source").is_dir() and (project_root / "tests").is_dir():
        lib_jars = (
            sorted((project_root / "lib").glob("*.jar"))
            if (project_root / "lib").is_dir()
            else []
        )
        return CompiledLayout(
            project_root=project_root,
            main_sources=discover_java_files(project_root / "source"),
            test_sources=discover_java_files(project_root / "tests"),
            classes_dir=project_root / "build",
            test_classes_dir=project_root / "build-tests",
            project_classpath=_join_existing(
                project_root / "build",
                project_root / "build-tests",
                *lib_jars,
            ),
            skip_main_compile=True,
        )
    return default_layout(project_root)


def _discover_gradle_java_source_roots(project_root: Path, source_set: str) -> list[Path]:
    """Find Java source sets in Gradle subprojects without walking build outputs."""
    excluded_dirs = {".git", ".gradle", ".idea", "build", "node_modules", "out", "target"}
    roots: list[Path] = []
    for current, dirs, _files in os.walk(project_root):
        current_path = Path(current)
        dirs[:] = sorted(name for name in dirs if name not in excluded_dirs)
        if current_path.name != "src":
            continue
        source_root = current_path / source_set / "java"
        if source_root.is_dir():
            roots.append(source_root)
        # Source trees cannot contain project build descriptors; stop at each src.
        dirs[:] = []
    return sorted(roots)


def _defects4j_layout(project_root: Path, props_path: Path) -> CompiledLayout:
    props = _parse_properties(props_path)
    src_main = project_root / props.get("d4j.dir.src.classes", "source")
    src_tests = project_root / props.get("d4j.dir.src.tests", "tests")
    bin_main = project_root / props.get("d4j.dir.bin.classes", "build")
    bin_tests = project_root / props.get("d4j.dir.bin.tests", "build-tests")
    # Chart-1 properties omit bin dirs; defaults match Defects4J Chart layout.
    if not bin_main.exists():
        for candidate in ("build", "target/classes", "bin"):
            path = project_root / candidate
            if path.is_dir():
                bin_main = path
                break
    if not bin_tests.exists():
        for candidate in ("build-tests", "target/test-classes", "build/test-classes"):
            path = project_root / candidate
            if path.is_dir():
                bin_tests = path
                break

    cp_test = _defects4j_cp_test(project_root)
    if not cp_test:
        lib_jars = (
            sorted((project_root / "lib").glob("*.jar"))
            if (project_root / "lib").is_dir()
            else []
        )
        cp_test = _join_existing(bin_main, bin_tests, *lib_jars)

    return CompiledLayout(
        project_root=project_root,
        main_sources=discover_java_files(src_main),
        test_sources=discover_java_files(src_tests),
        classes_dir=bin_main,
        test_classes_dir=bin_tests,
        project_classpath=cp_test,
        skip_main_compile=True,
    )


_CP_TEST_CACHE: dict[Path, str] = {}


def _defects4j_cp_test(project_root: Path) -> str | None:
    """Export Defects4J ``cp.test``, caching the result.

    Some projects (e.g. Mockito) declare ``compile`` depends on ``clean``.
    ``defects4j export -p cp.test`` depends on ``compile``, which wipes
    ``test-classes`` without rebuilding them. After export we restore tests
    when the test output directory no longer contains class files.
    """
    project_root = Path(project_root).resolve()
    cached = _CP_TEST_CACHE.get(project_root)
    if cached is not None:
        return cached

    result = run_command(
        ["defects4j", "export", "-p", "cp.test"],
        cwd=project_root,
        timeout_seconds=60,
        env=runner_command_env(project_path=project_root),
    )
    if not result.succeeded:
        return None
    exported: str | None = None
    for line in result.stdout.splitlines():
        text = line.strip()
        if text and not text.startswith("#") and (
            ":" in text or text.endswith(".jar") or "/" in text
        ):
            exported = text
            break
    if exported is None:
        return None

    _ensure_defects4j_test_classes(project_root)
    _CP_TEST_CACHE[project_root] = exported
    return exported


def _ensure_defects4j_test_classes(project_root: Path) -> None:
    """Rebuild developer tests if export/compile left ``test-classes`` empty."""
    props_path = project_root / "defects4j.build.properties"
    props = _parse_properties(props_path) if props_path.is_file() else {}
    bin_tests = project_root / props.get("d4j.dir.bin.tests", "target/test-classes")
    if not bin_tests.is_dir():
        for candidate in ("target/test-classes", "build-tests", "build/test-classes"):
            path = project_root / candidate
            if path.is_dir():
                bin_tests = path
                break
    if bin_tests.is_dir() and any(bin_tests.rglob("*.class")):
        return
    run_command(
        ["defects4j", "compile"],
        cwd=project_root,
        timeout_seconds=300,
        env=runner_command_env(project_path=project_root),
    )


def _parse_properties(path: Path) -> dict[str, str]:
    props: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        props[key.strip()] = value.strip()
    return props


def _join_existing(*entries: Path) -> str:
    parts: list[str] = []
    for entry in entries:
        if entry.exists():
            parts.append(str(entry))
    return os.pathsep.join(parts)


def compile_sources(
    *,
    source_files: list[Path],
    output_dir: Path,
    classpath: str,
    cwd: Path,
) -> tuple[bool, str, str]:
    if not source_files:
        return True, "", ""
    cwd = Path(cwd).resolve()
    # javac resolves -d relative to cwd. Always pass an absolute -d so a layout path
    # like "<repo>/data/.../build-tests" is never nested under project_root cwd.
    output_dir = Path(output_dir)
    if not output_dir.is_absolute():
        # Layout paths are normally created from an absolute project_root. If a caller
        # still passes a relative path, resolve against the process CWD (same as
        # Path.resolve), not javac's cwd — matching how resolve_layout builds paths.
        output_dir = output_dir.resolve()
    else:
        output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    command = [
        "javac",
        "-encoding",
        "UTF-8",
        "-cp",
        _absolutize_classpath(classpath, cwd),
        "-d",
        str(output_dir),
        *[str(path.resolve()) for path in source_files],
    ]
    result = run_command(
        command,
        cwd=cwd,
        timeout_seconds=120,
        env=runner_command_env(project_path=cwd),
    )
    return result.succeeded, result.stdout, result.stderr


def build_fixture_classpath(layout: CompiledLayout) -> str:
    """Project CP for fixture/D4J: prefer full project_classpath when available."""
    if layout.project_classpath:
        return _absolutize_classpath(layout.project_classpath, layout.project_root)
    entries = [str(layout.classes_dir.resolve()), str(layout.test_classes_dir.resolve())]
    return os.pathsep.join(entries)


def _absolutize_classpath(classpath: str, base: Path) -> str:
    """Resolve relative classpath entries against base (usually project_root)."""
    base = Path(base).resolve()
    parts: list[str] = []
    for entry in classpath.split(os.pathsep):
        text = entry.strip()
        if not text:
            continue
        path = Path(text)
        if path.is_absolute():
            parts.append(str(path))
        else:
            parts.append(str((base / path).resolve()))
    return os.pathsep.join(parts)


def run_single_test_with_runner(
    *,
    classpath: str,
    test_class: str,
    test_method: str,
    cwd: Path,
    framework: str = "JUNIT",
    per_test_timeout: int = 60,
) -> tuple[TestOutcome, str, str]:
    cwd = Path(cwd).resolve()
    classpath = _absolutize_classpath(classpath, cwd)
    with tempfile.NamedTemporaryFile(
        "w",
        prefix="causalfl-java-tests-",
        suffix=".txt",
        delete=False,
        encoding="utf-8",
    ) as handle:
        handle.write(f"{framework},{test_class}#{test_method}\n")
        tests_file = Path(handle.name)

    try:
        command = [
            "java",
            "-Xmx2g",
            "-XX:+ExitOnOutOfMemoryError",
            "-cp",
            classpath,
            TEST_RUNNER_MAIN_CLASS,
            "runTests",
            "--testMethods",
            str(tests_file),
            "--perTestTimeout",
            str(per_test_timeout),
        ]
        result = run_command(
            command,
            cwd=cwd,
            timeout_seconds=per_test_timeout + 30,
            env=runner_command_env(project_path=cwd),
        )
        outcome = parse_runner_outcome(
            result.stdout + "\n" + result.stderr,
            test_class=test_class,
            test_method=test_method,
        )
        if outcome is None:
            return (
                TestOutcome(
                    passed=False,
                    failing_tests=1,
                    failure_message=result.stderr or result.stdout or "No runner outcome line",
                ),
                result.stdout,
                result.stderr,
            )
        return outcome, result.stdout, result.stderr
    finally:
        tests_file.unlink(missing_ok=True)


def parse_runner_outcome(text: str, *, test_class: str, test_method: str) -> TestOutcome | None:
    wanted_suffixes = (
        f"{test_class}::{test_method}",
        f"{test_class}#{test_method}",
        f"{test_class}.{test_method}",
    )
    last: TestOutcome | None = None
    for match in RUNNER_OUTCOME_RE.finditer(text):
        test_id = match.group(1)
        label = match.group(2)
        if not any(test_id.endswith(suffix) or test_id == suffix for suffix in wanted_suffixes):
            if test_method not in test_id:
                continue
        passed = label == "PASS"
        last = TestOutcome(
            passed=passed,
            failing_tests=0 if passed else 1,
            failure_message="" if passed else f"runner outcome {label}",
        )
    return last
