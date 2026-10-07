from dataclasses import dataclass
import time
import shutil
from pathlib import Path
from config import PathSettings, LLMSettings
from src.project import Project
from src.models import PipelineStageStatus
from src.localization.repair_engine import RepairOrchestrator
from src.utils import json_utils, llm_util
from src.utils.output_paths import CaseOutputPaths, default_output_root

@dataclass(frozen=True)
class LocalizationRunSummary:
    project: str
    output_dir: Path
    result_dir: Path
    result_path: Path
    status: PipelineStageStatus
    message: str

class LocalizationStageRunner:
    def __init__(
            self,
            project: Project,
            paths: PathSettings,
            llm_settings: LLMSettings,
            preprocess_dataset_path: Path,
            result_dir: Path | None = None,
            test_case_id: str | None = None,):
        self.project = project
        self.paths = paths
        self.llm_settings = llm_settings
        self.preprocess_dataset_path = Path(preprocess_dataset_path)
        self.test_case_id = test_case_id
        self.result_dir = Path(result_dir) if result_dir is not None else None

    def run(self):
        started_at = time.perf_counter()
        stage_dir = self.paths.output_dir
        outputs = CaseOutputPaths.from_project(self.project.spec, default_output_root(self.paths.project_root))
        result_dir = self.result_dir or outputs.result_dir
        patch_path = result_dir / outputs.patch_path.name

        # Cleaning one case's logs must never remove the shared patch artifact.
        if result_dir.resolve().is_relative_to(stage_dir.resolve()):
            raise ValueError("The result directory must be outside the localization log directory")

        if stage_dir.exists():
            shutil.rmtree(stage_dir)
        stage_dir.mkdir(parents=True, exist_ok=True)
        completion_marker = stage_dir / "repair_complete"
        result_dir.mkdir(parents=True, exist_ok=True)
        patch_path.unlink(missing_ok=True)
        (result_dir / f"{outputs.case_id}_ranking.json").unlink(missing_ok=True)

        repair_engine = RepairOrchestrator(
            project=self.project, project_spec=self.project.spec, paths=self.paths,
            llm_settings=self.llm_settings, result_dir=result_dir,
            test_case_id=self.test_case_id,
        )
        with llm_util.collect_usage() as cb:
            repair_result = repair_engine.run()
        total_tokens = cb.total_tokens
        duration_seconds = time.perf_counter() - started_at

        (stage_dir / "usage.json").write_text(json_utils.json_dumps({
            "duration_seconds": round(duration_seconds, 2), "requests": cb.requests,
            "input_tokens": cb.input_tokens, "output_tokens": cb.output_tokens,
            "total_tokens": cb.total_tokens,
        }) + "\n", encoding="utf-8")

        patch_text = "".join(
            str(item.get("diff") or "").rstrip("\n") + "\n"
            for item in repair_result.get("accepted_patches", [])
            if str(item.get("diff") or "").strip()
        )
        patch_path.write_text(patch_text, encoding="utf-8")
        if patch_path.read_text(encoding="utf-8") != patch_text:
            raise ValueError(f"Repair patch at {patch_path} could not be verified")

        run_status = (
            PipelineStageStatus.SUCCESS
            if repair_result.get("repair_status") == "success"
            else PipelineStageStatus.FAILED
        )
        if run_status == PipelineStageStatus.SUCCESS:
            completion_marker.touch()
        return LocalizationRunSummary(
            project=str(self.project.spec),
            output_dir=stage_dir,
            result_dir=result_dir,
            result_path=patch_path,
            status=run_status,
            message=f"Repair completed in {duration_seconds:.2f} seconds. Token cost: {total_tokens}. Patch saved in {patch_path}."
        )
