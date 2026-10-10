"""Run the unified fault-diagnosis and repair stage."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import time

from config import LLMSettings, PathSettings
from src.debug.repair_engine import RepairOrchestrator
from src.models import PipelineStageStatus
from src.patching.java.patch_validation import snapshot_production_sources
from src.project import Project
from src.utils import json_utils, llm_util
from src.utils.output_paths import CaseOutputPaths


@dataclass(frozen=True)
class DebugRunSummary:
    project: str
    debug_dir: Path
    result_dir: Path
    result_path: Path | None
    status: PipelineStageStatus
    message: str


class DebugStageRunner:
    """Coordinate one case's diagnosis, repair, and full regression."""

    def __init__(
        self,
        project: Project,
        paths: PathSettings,
        llm_settings: LLMSettings,
        preprocess_dir: Path,
        result_dir: Path,
        test_case_id: str | None = None,
        started_at: float | None = None,
    ):
        self.project = project
        self.paths = paths
        self.llm_settings = llm_settings
        self.preprocess_dir = Path(preprocess_dir)
        self.result_dir = Path(result_dir)
        self.test_case_id = test_case_id
        self.started_at = started_at

    def run(self) -> DebugRunSummary:
        started_at = time.perf_counter()
        print(
            f"Debug {self.project.spec.dataset} {self.project.spec.project_id}-{self.project.spec.bug_id}...",
            flush=True,
        )
        debug_dir = self.paths.output_dir
        outputs = CaseOutputPaths.from_project(self.project.spec, self.result_dir)
        patch_path = outputs.patch_path

        if debug_dir.exists():
            shutil.rmtree(debug_dir)
        debug_dir.mkdir(parents=True, exist_ok=True)
        outputs.patch_dir.mkdir(parents=True, exist_ok=True)
        patch_path.unlink(missing_ok=True)

        repair_engine = RepairOrchestrator(
            project=self.project,
            project_spec=self.project.spec,
            paths=self.paths,
            preprocess_dir=self.preprocess_dir,
            llm_settings=self.llm_settings,
            test_case_id=self.test_case_id,
            baseline_sources=snapshot_production_sources(self.project),
            budget_started_at=self.started_at,
        )
        with llm_util.collect_usage() as usage:
            repair_result = repair_engine.run()
        duration_seconds = time.perf_counter() - started_at

        (debug_dir / "usage.json").write_text(
            json_utils.json_dumps(
                {
                    "duration_seconds": round(duration_seconds, 2),
                    "requests": usage.requests,
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "total_tokens": usage.total_tokens,
                }
            )
            + "\n",
            encoding="utf-8",
        )

        status = (
            PipelineStageStatus.SUCCESS
            if repair_result.get("repair_status") == "success"
            else PipelineStageStatus.FAILED
        )
        result_path = None
        if status == PipelineStageStatus.SUCCESS:
            patch_text = str(repair_result.get("final_diff") or "")
            if patch_text.strip():
                # Preserve source newlines, including CRLF diff context lines.
                patch_bytes = patch_text.encode("utf-8")
                try:
                    patch_path.write_bytes(patch_bytes)
                    if patch_path.read_bytes() != patch_bytes:
                        raise ValueError(f"Repair patch at {patch_path} could not be verified")
                except Exception:
                    patch_path.unlink(missing_ok=True)
                    raise
                result_path = patch_path
            message = (
                f"Debug completed in {duration_seconds:.2f} seconds. "
                f"Token cost: {usage.total_tokens}. "
                + (f"Patch saved in {result_path}." if result_path else "No source changes; no patch generated.")
            )
            (debug_dir / "repair_complete").touch()
            print("Debug finished", flush=True)
        else:
            reason = str(repair_result.get("explanation") or "Repair did not complete")
            message = (
                f"Debug failed in {duration_seconds:.2f} seconds. "
                f"Token cost: {usage.total_tokens}. Reason: {reason}"
            )
            print(f"Debug failed: {reason}", flush=True)
        return DebugRunSummary(
            project=str(self.project.spec),
            debug_dir=debug_dir,
            result_dir=self.result_dir,
            result_path=result_path,
            status=status,
            message=message,
        )
