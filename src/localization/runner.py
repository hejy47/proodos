from dataclasses import dataclass
import json
import time
import shutil
from pathlib import Path
from config import PathSettings, LLMSettings
from src.project import Project
from src.models import PipelineStageStatus
from src.preprocess.context import load_preprocess_context, resolve_preprocess_path
from src.localization.fl_engine import FaultLocalizationEngine
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
            result_dir: Path | None = None,):
        self.project = project
        self.paths = paths
        self.llm_settings = llm_settings
        self.preprocess_dataset_path = resolve_preprocess_path(preprocess_dataset_path)
        self.result_dir = Path(result_dir) if result_dir is not None else None

    def run(self):
        started_at = time.perf_counter()
        stage_dir = self.paths.output_dir
        outputs = CaseOutputPaths.from_project(self.project.spec, default_output_root(self.paths.project_root))
        result_dir = self.result_dir or outputs.result_dir
        report_json_path = result_dir / outputs.ranking_path.name

        # Cleaning one case's logs must never remove the shared rankings.
        if result_dir.resolve().is_relative_to(stage_dir.resolve()):
            raise ValueError("The result directory must be outside the localization log directory")

        if stage_dir.exists():
            shutil.rmtree(stage_dir)
        stage_dir.mkdir(parents=True, exist_ok=True)
        result_dir.mkdir(parents=True, exist_ok=True)
        report_json_path.unlink(missing_ok=True)

        if not self.preprocess_dataset_path.exists():
            payload = {
                "project_spec": self.project.spec,
                "message": f"Dataset not found at {self.preprocess_dataset_path}. Please run the preprocess stage first."
            }
            raise FileNotFoundError(payload)

        preprocess_data = load_preprocess_context(self.preprocess_dataset_path)
        if not preprocess_data.method_ids:
            payload = {
                "project_spec": self.project.spec,
                "message": f"No candidate methods found in the dataset at {self.preprocess_dataset_path}. Cannot run localization stage."
            }
            raise ValueError(payload)

        fl_engine = FaultLocalizationEngine(
            llm_settings=self.llm_settings,
            preprocess_data=preprocess_data,
            output_dir=stage_dir,
            project=self.project,
        )
        with llm_util.collect_usage() as cb:
            fl_ranks = fl_engine.run()
        total_tokens = cb.total_tokens
        duration_seconds = time.perf_counter() - started_at

        (stage_dir / "usage.json").write_text(json_utils.json_dumps({
            "duration_seconds": round(duration_seconds, 2), "requests": cb.requests,
            "input_tokens": cb.input_tokens, "output_tokens": cb.output_tokens,
            "total_tokens": cb.total_tokens,
        }) + "\n", encoding="utf-8")

        json_utils.write_json_atomic(report_json_path, fl_ranks, sort_keys=False)
        # Read back the exact artifact before returning SUCCESS. This catches
        # path/serialization errors while the stage can still report failure.
        saved_report = json.loads(report_json_path.read_text(encoding="utf-8"))
        if not isinstance(saved_report.get("ranked_methods"), list) or not isinstance(saved_report.get("explanation"), str):
            raise ValueError(f"Localization result at {report_json_path} is missing ranked_methods or explanation")

        return LocalizationRunSummary(
            project=str(self.project.spec),
            output_dir=stage_dir,
            result_dir=result_dir,
            result_path=report_json_path,
            status=PipelineStageStatus.SUCCESS,
            message=f"Localization completed in {duration_seconds:.2f} seconds. Token cost: {total_tokens}. Report saved in {report_json_path}."
        )
