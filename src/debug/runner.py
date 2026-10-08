"""Run the unified fault-diagnosis and repair stage."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import time

from config import LLMSettings, PathSettings
from src.debug.repair_engine import RepairOrchestrator
from src.models import PipelineStageStatus
from src.project import Project
from src.utils import json_utils, llm_util
from src.utils.output_paths import CaseOutputPaths


@dataclass(frozen=True)
class DebugRunSummary:
    project: str
    debug_dir: Path
    result_dir: Path
    result_path: Path
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
    ):
        self.project = project
        self.paths = paths
        self.llm_settings = llm_settings
        self.preprocess_dir = Path(preprocess_dir)
        self.result_dir = Path(result_dir)
        self.test_case_id = test_case_id

    def run(self) -> DebugRunSummary:
        started_at = time.perf_counter()
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

        patch_text = "".join(
            str(item.get("diff") or "").rstrip("\n") + "\n"
            for item in repair_result.get("accepted_patches", [])
            if str(item.get("diff") or "").strip()
        )
        patch_path.write_text(patch_text, encoding="utf-8")
        if patch_path.read_text(encoding="utf-8") != patch_text:
            raise ValueError(f"Repair patch at {patch_path} could not be verified")

        status = (
            PipelineStageStatus.SUCCESS
            if repair_result.get("repair_status") == "success"
            else PipelineStageStatus.FAILED
        )
        if status == PipelineStageStatus.SUCCESS:
            (debug_dir / "repair_complete").touch()
        return DebugRunSummary(
            project=str(self.project.spec),
            debug_dir=debug_dir,
            result_dir=self.result_dir,
            result_path=patch_path,
            status=status,
            message=(
                f"Debug completed in {duration_seconds:.2f} seconds. "
                f"Token cost: {usage.total_tokens}. Patch saved in {patch_path}."
            ),
        )
