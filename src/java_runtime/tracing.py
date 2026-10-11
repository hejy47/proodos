"""Execute selected tests with a Java agent, only for runtime tools."""
from __future__ import annotations
import csv
from dataclasses import dataclass
from pathlib import Path
import shutil
import tempfile

from src.java_runtime.runner_cli import TEST_RUNNER_MAIN_CLASS, runner_command_env, test_runner_classpath
from src.models import TestCase
from src.utils.cmd_util import get_test_timeout_seconds, run_command, run_test_command

JAVA_AGENT_CLI_CLASS = TEST_RUNNER_MAIN_CLASS

@dataclass(frozen=True)
class TraceCollectionError:
    test_case: TestCase
    execution_error: str

class JavaTraceCollector:
    def __init__(self, project):
        self.project = project
        self.project_spec = project.spec

    def collect_reports(
        self,
        *,
        selected_tests: list[TestCase],
        agent_jar_path: Path,
        include_prefixes: list[str],
        stage_dir: Path,
        spectra_path: Path,
        tests_report_path: Path,
        trace_report_path: Path,
    ) -> list[TraceCollectionError] | None:
        classpath = self.project.test_runtime_classpath()
        if not classpath or not classpath.strip():
            return [
                TraceCollectionError(
                    test_case=test_case,
                    execution_error="No Java test runtime classpath is available for compact dynamic summary collection",
                )
                for test_case in selected_tests
            ]

        with tempfile.TemporaryDirectory(prefix="proodos-instrumentation-", dir=stage_dir) as temp_dir:
            temp_root = Path(temp_dir)
            tests_file = temp_root / "tests.txt"
            trace_report_dir = temp_root / "trace-report"
            self._write_test_methods_file(tests_file, selected_tests)

            timeout = get_test_timeout_seconds()
            command = self._batch_command(
                agent_jar_path=agent_jar_path,
                classpath=classpath,
                tests_file=tests_file,
                trace_report_dir=trace_report_dir,
                include_prefixes=include_prefixes,
                per_test_timeout=timeout,
            )
            result = run_test_command(
                command,
                cwd=self.project.project_path,
                timeout_seconds=timeout,
                env=runner_command_env(
                    project_path=self.project.project_path,
                    dataset=self.project_spec.dataset,
                    project_id=self.project_spec.project_id,
                ),
            )
            if not result.succeeded:
                execution_error = self._command_error(result.stdout, result.stderr)
                return [
                    TraceCollectionError(test_case=test_case, execution_error=execution_error)
                    for test_case in selected_tests
                ]

            # The agent writes trace.ser on JVM shutdown; convert to CSV
            # reports consumed by the on-demand trace tool.
            report_error = self._materialize_trace_report(
                agent_jar_path=agent_jar_path,
                trace_report_dir=trace_report_dir,
            )
            if report_error is not None:
                return [
                    TraceCollectionError(test_case=test_case, execution_error=report_error)
                    for test_case in selected_tests
                ]

            if not self._trace_report_files_exist(trace_report_dir):
                execution_error = self._command_error(result.stdout, result.stderr)
                return [
                    TraceCollectionError(test_case=test_case, execution_error=execution_error)
                    for test_case in selected_tests
                ]

            self._copy_trace_report_files(
                report_dir=trace_report_dir,
                spectra_path=spectra_path,
                tests_report_path=tests_report_path,
                trace_report_path=trace_report_path,
            )
        return None


    def _materialize_trace_report(
        self,
        *,
        agent_jar_path: Path,
        trace_report_dir: Path,
    ) -> str | None:
        """Convert agent ``trace.ser`` into spectra/tests/trace CSV artifacts."""
        ser_file = trace_report_dir / "trace.ser"
        if not ser_file.is_file():
            return f"Trace agent did not produce {ser_file.name} after test_runner execution"
        trace_report_dir.mkdir(parents=True, exist_ok=True)
        report_result = run_command(
            [
                "java",
                "-cp",
                str(agent_jar_path),
                "proodos.trace.TraceReportMain",
                "--dataFile",
                str(ser_file),
                "--outputDirectory",
                str(trace_report_dir),
            ],
            cwd=self.project.project_path,
            timeout_seconds=600,
        )
        if not report_result.succeeded:
            return self._command_error(report_result.stdout, report_result.stderr)
        return None


    def _batch_command(
        self,
        *,
        agent_jar_path: Path,
        classpath: str,
        tests_file: Path,
        trace_report_dir: Path,
        include_prefixes: list[str],
        per_test_timeout: int | None = None,
    ) -> list[str]:
        command = [
            "java",
            "-Xmx16g",
            "-Djava.awt.headless=true",
            self._trace_agent_option(
                agent_jar_path=agent_jar_path,
                trace_report_dir=trace_report_dir,
                include_prefixes=include_prefixes,
            ),
            "-cp",
            test_runner_classpath(classpath),
            JAVA_AGENT_CLI_CLASS,
            "runTests",
            "--testMethods",
            str(tests_file),
            "--perTestTimeout",
            str(get_test_timeout_seconds() if per_test_timeout is None else per_test_timeout),
        ]

        return command

    def _trace_agent_option(
        self,
        *,
        agent_jar_path: Path,
        trace_report_dir: Path,
        include_prefixes: list[str],
    ) -> str:
        agent_args = [
            f"outputDir={trace_report_dir}",
        ]
        if include_prefixes:
            agent_args.append(f"includes={':'.join(include_prefixes)}")
        return f"-javaagent:{agent_jar_path}={','.join(agent_args)}"


    def _write_test_methods_file(self, path: Path, selected_tests: list[TestCase]) -> None:
        lines = [self._serialize_test_method(test_case) for test_case in selected_tests]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


    def _serialize_test_method(self, test_case: TestCase) -> str:
        framework = str(test_case.metadata.get("framework", "JUNIT")).strip().upper() or "JUNIT"
        if framework not in {"JUNIT", "JUNIT5", "TESTNG"}:
            framework = "JUNIT"
        return f"{framework},{test_case.class_name}#{test_case.method_name}"


    @staticmethod
    def _trace_report_files_exist(report_dir: Path) -> bool:
        return (
            (report_dir / "spectra.csv").is_file()
            and (report_dir / "tests.csv").is_file()
            and (report_dir / "trace.txt").is_file()
        )


    @staticmethod
    def _copy_trace_report_files(
        *,
        report_dir: Path,
        spectra_path: Path,
        tests_report_path: Path,
        trace_report_path: Path,
    ) -> None:
        shutil.copyfile(report_dir / "spectra.csv", spectra_path)
        shutil.copyfile(report_dir / "tests.csv", tests_report_path)
        shutil.copyfile(report_dir / "trace.txt", trace_report_path)


    def _read_trace_spectra(self, spectra_path: Path) -> dict[int, str]:
        method_names_by_id: dict[int, str] = {}
        for row in self._read_csv_dicts(spectra_path):
            raw_id = str(row.get("id", "")).strip()
            method_name = str(row.get("name", "")).strip()
            if not raw_id or not method_name:
                continue
            try:
                method_names_by_id[int(raw_id)] = method_name
            except ValueError:
                continue
        return method_names_by_id


    def _read_trace_test_ids_by_index(self, tests_path: Path) -> dict[int, str]:
        tests_by_index: dict[int, str] = {}
        for row in self._read_csv_dicts(tests_path):
            raw_id = str(row.get("id", "")).strip()
            test_id = str(row.get("name", "")).strip()
            if not raw_id or not test_id:
                continue
            try:
                tests_by_index[int(raw_id)] = test_id
            except ValueError:
                continue
        return tests_by_index


    @staticmethod
    def _read_csv_dicts(path: Path) -> list[dict[str, str]]:
        if not path.exists():
            return []
        csv.field_size_limit(16 * 1024 * 1024)
        with path.open("r", encoding="utf-8", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]


    @staticmethod
    def _command_error(stdout: str, stderr: str) -> str:
        return stderr.strip() or stdout.strip() or "Compact dynamic summary collection command failed"
