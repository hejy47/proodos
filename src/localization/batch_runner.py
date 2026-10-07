from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
import json
import subprocess
import sys
from typing import Callable

from src.utils.json_utils import json_dumps, write_jsonl
from src.models import ProjectSpec
from src.utils.output_paths import CaseOutputPaths, default_output_root, project_case_id

LOCALIZATION_SUMMARY_FILENAME = "localization_summary.json"
LOCALIZATION_CASE_RESULTS_FILENAME = "localization_case_results.jsonl"


@dataclass(frozen=True)
class BatchLocalizationCase:
    case_id: str
    dataset: str
    project_path: Path
    preprocess_dir: Path
    localization_dir: Path
    result_dir: Path | None = None
    project_id: str | None = None
    bug_id: str | None = None


@dataclass(frozen=True)
class BatchLocalizationCaseResult:
    case_id: str
    dataset: str
    project_path: Path
    preprocess_dir: Path
    localization_dir: Path
    stage: str
    success: bool
    return_code: int
    stdout: str
    stderr: str
    result_dir: Path | None = None
    result_path: Path | None = None


@dataclass(frozen=True)
class BatchLocalizationSummary:
    case_results: tuple[BatchLocalizationCaseResult, ...]
    output_dir: Path
    summary_path: Path
    case_results_path: Path

    @property
    def succeeded(self) -> bool:
        return all(result.success for result in self.case_results)


class BatchLocalizationRunner:
    def __init__(
        self,
        *,
        repo_root: Path,
        localization_root: Path,
        max_workers: int = 1,
        stop_on_failure: bool = True,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ):
        self.repo_root = repo_root
        self.localization_root = localization_root
        self.max_workers = max(1, max_workers)
        self.stop_on_failure = stop_on_failure
        self._command_runner = command_runner or subprocess.run

    def run(self, cases: list[BatchLocalizationCase]) -> BatchLocalizationSummary:
        self.localization_root.mkdir(parents=True, exist_ok=True)
        case_results_path = self.localization_root / LOCALIZATION_CASE_RESULTS_FILENAME
        if self.max_workers == 1:
            results = self._run_sequential(cases, case_results_path=case_results_path)
        else:
            results = self._run_concurrent(cases, case_results_path=case_results_path)
        summary_path = self.localization_root / LOCALIZATION_SUMMARY_FILENAME
        summary_payload = {
            "case_count": len(results),
            "successful_case_count": sum(1 for result in results if result.success),
            "failed_case_count": sum(1 for result in results if not result.success),
            "max_workers": self.max_workers,
            "stop_on_failure": self.stop_on_failure,
            "pipeline_stage": "localization",
        }
        summary_path.write_text(json_dumps(summary_payload), encoding="utf-8")
        write_jsonl(case_results_path, [_case_result_payload(result) for result in results])
        return BatchLocalizationSummary(
            case_results=tuple(results),
            output_dir=self.localization_root,
            summary_path=summary_path,
            case_results_path=case_results_path,
        )

    def _run_sequential(
        self,
        cases: list[BatchLocalizationCase],
        *,
        case_results_path: Path,
    ) -> list[BatchLocalizationCaseResult]:
        results: list[BatchLocalizationCaseResult] = []
        for case in cases:
            result = self._run_case(case)
            results.append(result)
            self._write_case_results(case_results_path, results)
            if not result.success and self.stop_on_failure:
                break
        return results

    def _run_concurrent(
        self,
        cases: list[BatchLocalizationCase],
        *,
        case_results_path: Path,
    ) -> list[BatchLocalizationCaseResult]:
        results_by_index: dict[int, BatchLocalizationCaseResult] = {}
        pending_by_future: dict[Future[BatchLocalizationCaseResult], int] = {}
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
        results: list[BatchLocalizationCaseResult],
    ) -> None:
        write_jsonl(case_results_path, [_case_result_payload(result) for result in results])

    def _run_case(self, case: BatchLocalizationCase) -> BatchLocalizationCaseResult:
        spec = ProjectSpec(case.dataset, case.project_path, case.project_id, case.bug_id or case.case_id)
        outputs = CaseOutputPaths.from_project(spec, default_output_root(self.repo_root))
        result_dir = case.result_dir or outputs.result_dir
        fl_result_path = result_dir / outputs.ranking_path.name
        if fl_result_path.exists():
            return BatchLocalizationCaseResult(
                case_id=case.case_id,
                dataset=case.dataset,
                project_path=case.project_path,
                preprocess_dir=case.preprocess_dir,
                localization_dir=case.localization_dir,
                result_dir=result_dir,
                result_path=fl_result_path,
                stage="localization",
                success=True,
                return_code=0,
                stdout=f"FL result already exists at {fl_result_path}, skipping localization.",
                stderr="",
            )
        case.localization_dir.mkdir(parents=True, exist_ok=True)
        result_dir.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            "main.py",
            "--project_path",
            str(case.project_path),
            "--dataset",
            case.dataset,
            "--preprocess_dir",
            str(case.preprocess_dir),
            "--localization_dir",
            str(case.localization_dir),
            "--result_dir",
            str(result_dir),
            "--stage",
            "localization",
        ]
        if case.project_id:
            command.extend(["--project_id", case.project_id])
        command.extend(["--bug_id", spec.bug_id])

        completed = self._command_runner(
            command,
            cwd=self.repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return BatchLocalizationCaseResult(
            case_id=case.case_id,
            dataset=case.dataset,
            project_path=case.project_path,
            preprocess_dir=case.preprocess_dir,
            localization_dir=case.localization_dir,
            stage="localization",
            success=completed.returncode == 0,
            return_code=int(completed.returncode),
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            result_dir=result_dir,
            result_path=fl_result_path,
        )


def load_batch_localization_cases(
    cases_path: Path,
    *,
    preprocess_root: Path,
    localization_root: Path,
    result_root: Path | None = None,
) -> list[BatchLocalizationCase]:
    cases: list[BatchLocalizationCase] = []
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
            BatchLocalizationCase(
                case_id=case_id,
                dataset=spec.dataset,
                project_path=spec.project_path,
                preprocess_dir=preprocess_root / project_case_id(spec) / "preprocess",
                localization_dir=localization_root / project_case_id(spec) / "localization",
                result_dir=result_root,
                project_id=spec.project_id,
                bug_id=spec.bug_id,
            )
        )
    return cases


def _parse_case_line(line: str) -> dict[str, object] | None:
    payload = json.loads(line)
    return payload if isinstance(payload, dict) else None


def _case_result_payload(result: BatchLocalizationCaseResult) -> dict[str, object]:
    return {
        "case_id": result.case_id,
        "dataset": result.dataset,
        "project_path": str(result.project_path),
        "preprocess_dir": str(result.preprocess_dir),
        "localization_dir": str(result.localization_dir),
        "result_dir": str(result.result_dir) if result.result_dir is not None else None,
        "result_path": str(result.result_path) if result.result_path is not None else None,
        "pipeline_stage": result.stage,
        "pipeline_status": "success" if result.success else "failed",
        "return_code": result.return_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
