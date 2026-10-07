from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
import json
import subprocess
import sys
from typing import Callable

from src.models import ProjectSpec
from src.preprocess.context import FAULT_CONTEXT_FILENAME
from src.utils.json_utils import json_dumps, write_jsonl
from src.utils.output_paths import project_case_id

PREPROCESS_SUMMARY_FILENAME = "preprocess_summary.json"
PREPROCESS_CASE_RESULTS_FILENAME = "preprocess_case_results.jsonl"


@dataclass(frozen=True)
class BatchPreprocessCase:
    case_id: str
    dataset: str
    project_path: Path
    preprocess_dir: Path
    project_id: str | None = None
    bug_id: str | None = None


@dataclass(frozen=True)
class BatchPreprocessCaseResult:
    case_id: str
    dataset: str
    project_path: Path
    preprocess_dir: Path
    stage: str
    success: bool
    return_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class BatchPreprocessSummary:
    case_results: tuple[BatchPreprocessCaseResult, ...]
    output_dir: Path
    summary_path: Path
    case_results_path: Path

    @property
    def succeeded(self) -> bool:
        return all(result.success for result in self.case_results)


class BatchPreprocessRunner:
    def __init__(
        self,
        *,
        repo_root: Path,
        preprocess_root: Path,
        max_workers: int = 1,
        stop_on_failure: bool = False,
        force_reprocess: bool = False,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ):
        self.repo_root = repo_root
        self.preprocess_root = preprocess_root
        self.max_workers = max(1, max_workers)
        self.stop_on_failure = stop_on_failure
        self.force_reprocess = force_reprocess
        self._command_runner = command_runner or subprocess.run

    def run(self, cases: list[BatchPreprocessCase]) -> BatchPreprocessSummary:
        self.preprocess_root.mkdir(parents=True, exist_ok=True)
        case_results_path = self.preprocess_root / PREPROCESS_CASE_RESULTS_FILENAME
        if self.max_workers == 1:
            results = self._run_sequential(cases, case_results_path=case_results_path)
        else:
            results = self._run_concurrent(cases, case_results_path=case_results_path)
        summary_path = self.preprocess_root / PREPROCESS_SUMMARY_FILENAME
        summary_payload = {
            "case_count": len(results),
            "successful_case_count": sum(1 for result in results if result.success),
            "failed_case_count": sum(1 for result in results if not result.success),
            "max_workers": self.max_workers,
            "stop_on_failure": self.stop_on_failure,
            "collection_strategy": "static_fault_context",
        }
        summary_path.write_text(json_dumps(summary_payload), encoding="utf-8")
        write_jsonl(case_results_path, [_case_result_payload(result) for result in results])
        return BatchPreprocessSummary(
            case_results=tuple(results),
            output_dir=self.preprocess_root,
            summary_path=summary_path,
            case_results_path=case_results_path,
        )

    def _run_sequential(
        self,
        cases: list[BatchPreprocessCase],
        *,
        case_results_path: Path,
    ) -> list[BatchPreprocessCaseResult]:
        results: list[BatchPreprocessCaseResult] = []
        for case in cases:
            result = self._run_case(case)
            results.append(result)
            self._write_case_results(case_results_path, results)
            if not result.success and self.stop_on_failure:
                break
        return results

    def _run_concurrent(
        self,
        cases: list[BatchPreprocessCase],
        *,
        case_results_path: Path,
    ) -> list[BatchPreprocessCaseResult]:
        results_by_index: dict[int, BatchPreprocessCaseResult] = {}
        pending_by_future: dict[Future[BatchPreprocessCaseResult], int] = {}
        next_index = 0
        stop_submitting = False

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            while next_index < len(cases) and len(pending_by_future) < self.max_workers:
                future = executor.submit(self._run_case, cases[next_index])
                pending_by_future[future] = next_index
                next_index += 1

            while pending_by_future:
                completed, _ = wait(tuple(pending_by_future.keys()), return_when=FIRST_COMPLETED)
                for future in completed:
                    index = pending_by_future.pop(future)
                    result = future.result()
                    results_by_index[index] = result
                    self._write_case_results(
                        case_results_path,
                        [results_by_index[idx] for idx in sorted(results_by_index)],
                    )
                    if not result.success and self.stop_on_failure:
                        stop_submitting = True
                    if not stop_submitting and next_index < len(cases):
                        next_future = executor.submit(self._run_case, cases[next_index])
                        pending_by_future[next_future] = next_index
                        next_index += 1

        return [results_by_index[index] for index in sorted(results_by_index)]

    def _write_case_results(
        self,
        case_results_path: Path,
        results: list[BatchPreprocessCaseResult],
    ) -> None:
        write_jsonl(case_results_path, [_case_result_payload(result) for result in results])

    def _run_case(self, case: BatchPreprocessCase) -> BatchPreprocessCaseResult:
        graph_path = case.preprocess_dir / FAULT_CONTEXT_FILENAME
        if not self.force_reprocess and graph_path.is_file():
            return BatchPreprocessCaseResult(
                case_id=case.case_id, dataset=case.dataset, project_path=case.project_path,
                preprocess_dir=case.preprocess_dir, stage="preprocess", success=True,
                return_code=0, stdout=f"Static fault context already exists at {graph_path}, skipping preprocess.",
                stderr="",
            )
        case.preprocess_dir.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            "main.py",
            "--project_path",
            str(case.project_path),
            "--dataset",
            case.dataset,
            "--preprocess_dir",
            str(case.preprocess_dir),
            "--stage",
            "preprocess",
        ]
        if case.project_id:
            command.extend(["--project_id", case.project_id])
        if case.bug_id:
            command.extend(["--bug_id", case.bug_id])

        completed = self._command_runner(
            command,
            cwd=self.repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return BatchPreprocessCaseResult(
            case_id=case.case_id,
            dataset=case.dataset,
            project_path=case.project_path,
            preprocess_dir=case.preprocess_dir,
            stage="preprocess",
            success=completed.returncode == 0,
            return_code=int(completed.returncode),
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


def load_batch_cases(cases_path: Path, *, preprocess_root: Path) -> list[BatchPreprocessCase]:
    cases: list[BatchPreprocessCase] = []
    for raw_line in cases_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        payload = _parse_case_line(line)
        if payload is None:
            continue
        case_id = str(
            payload.get("case_id")
            or f"{payload.get('project_id', 'unknown')}-{payload.get('bug_id', 'unknown')}"
        )
        spec = ProjectSpec(
            dataset=str(payload.get("dataset") or "defects4j").lower(),
            project_path=Path(str(payload["project_path"])),
            project_id=str(payload["project_id"]) if payload.get("project_id") is not None else None,
            bug_id=str(payload["bug_id"]) if payload.get("bug_id") is not None else case_id,
        )
        cases.append(
            BatchPreprocessCase(
                case_id=case_id,
                dataset=spec.dataset,
                project_path=spec.project_path,
                preprocess_dir=preprocess_root / project_case_id(spec) / "preprocess",
                project_id=spec.project_id,
                bug_id=spec.bug_id,
            )
        )
    return cases


def _parse_case_line(line: str) -> dict[str, object] | None:
    payload = json.loads(line)
    return payload if isinstance(payload, dict) else None


def _case_result_payload(result: BatchPreprocessCaseResult) -> dict[str, object]:
    return {
        "case_id": result.case_id,
        "dataset": result.dataset,
        "project_path": str(result.project_path),
        "preprocess_dir": str(result.preprocess_dir),
        "pipeline_stage": result.stage,
        "pipeline_status": "success" if result.success else "failed",
        "return_code": result.return_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
