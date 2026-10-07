from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from config import RuntimeSettings, build_runtime_settings, resolve_output_dir, resolve_project_path
from src.models import PipelineRunSummary, PipelineStageResult, PipelineStageStatus, ProjectSpec
from src.preprocess import PreprocessStageRunner
from src.localization import LocalizationStageRunner
from src.project import Project, ProjectFactory
from src.utils.output_paths import CaseOutputPaths, default_output_root


STAGE_ORDER = (
    "bootstrap",
    "project",
    "preprocess",
    "localization",
)


@dataclass(frozen=True)
class PipelineContext:
    settings: RuntimeSettings
    project_spec: ProjectSpec
    dry_run: bool
    test_case_id: str | None = None
    result_dir: Path | None = None


class CausalFLPipeline:
    def __init__(self, context: PipelineContext):
        self.context = context
        self.project: Project = ProjectFactory.create_project(context.project_spec)

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "CausalFLPipeline":
        project_spec = ProjectSpec(
            dataset=args.dataset.lower(),
            project_path=resolve_project_path(args.project_path),
            project_id=args.project_id,
            bug_id=args.bug_id,
        )
        output_root = (resolve_output_dir(args.output_root)
                       if getattr(args, "output_root", None) else default_output_root())
        if getattr(args, "output_root", None):
            # Runtime tools share the same root as the command's stage outputs.
            os.environ["CAUSALFL_OUTPUT_ROOT"] = str(output_root)
        outputs = CaseOutputPaths.from_project(project_spec, output_root)
        settings = build_runtime_settings(
            project_path=project_spec.project_path,
            preprocess_dir=args.preprocess_dir or outputs.preprocess_dir,
            localization_dir=args.localization_dir or outputs.localization_dir,
        )
        context = PipelineContext(
            settings=settings,
            project_spec=project_spec,
            dry_run=args.dry_run,
            test_case_id=getattr(args, "test_case_id", None),
            result_dir=(resolve_output_dir(args.result_dir) if getattr(args, "result_dir", None) else outputs.result_dir),
        )
        return cls(context)

    def run(self, stage: str = "bootstrap") -> PipelineRunSummary:
        selected_stage = stage.lower()
        if selected_stage == "all":
            stage_names = STAGE_ORDER
        else:
            stage_names = (selected_stage,)

        stage_runners = {
            "bootstrap": self._run_bootstrap,
            "project": self._run_project_stage,
            "preprocess": self._run_preprocess_stage,
            "localization": self._run_localization_stage,
        }
        stage_results = []
        for stage_name in stage_names:
            runner = stage_runners.get(stage_name)
            if runner is None:
                stage_results.append(
                    PipelineStageResult(
                        stage=stage_name,
                        status=PipelineStageStatus.FAILED,
                        message="Unknown pipeline stage",
                    )
                )
                continue
            stage_results.append(runner())

        return PipelineRunSummary(
            project=self.context.project_spec,
            stage_results=stage_results,
            preprocess_dir=(
                self.context.settings.preprocess_paths.output_dir
                if self.context.settings.preprocess_paths is not None
                else None
            ),
            localization_dir=(
                self.context.settings.localization_paths.output_dir
                if self.context.settings.localization_paths is not None
                else None
            ),
            result_dir=self.context.result_dir,
        )

    def _run_bootstrap(self) -> PipelineStageResult:
        paths = self.context.settings.preprocess_paths
        if paths is None:
            raise ValueError("preprocess_paths are required for bootstrap")
        paths.output_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = paths.output_dir / "bootstrap_manifest.json"
        payload = {
            "dataset": self.context.project_spec.dataset,
            "project_path": str(self.context.project_spec.project_path),
            "project_id": self.context.project_spec.project_id,
            "bug_id": self.context.project_spec.bug_id,
            "collection_strategy": "static_fault_context",
            "instrumentation": False,
            "dry_run": self.context.dry_run,
            "project_adapter": self.project.__class__.__name__,
            "project_metadata": self.project.describe(),
            "registered_stages": list(STAGE_ORDER),
        }
        manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

        return PipelineStageResult(
            project=f"{self.context.project_spec.dataset} {self.context.project_spec.project_id}-{self.context.project_spec.bug_id}",
            stage="bootstrap",
            status=PipelineStageStatus.SUCCESS,
            message="Resolved runtime settings, stage roots, and project adapter",
            metadata={},
        )

    def _run_project_stage(self) -> PipelineStageResult:
        paths = self.context.settings.preprocess_paths
        if paths is None:
            raise ValueError("preprocess_paths are required for project stage")
        paths.output_dir.mkdir(parents=True, exist_ok=True)
        report_path = paths.output_dir / "project_stage.json"
        environment_valid = self.project.validate_environment()
        report_payload: dict[str, object] = {
            "project_adapter": self.project.__class__.__name__,
            "environment_valid": environment_valid,
            "project_metadata": self.project.describe(),
            "test_case_id": self.context.test_case_id,
            "dry_run": self.context.dry_run,
        }

        if not environment_valid:
            report_payload["message"] = "Project environment is not ready for execution"
            report_path.write_text(self._json_dump(report_payload), encoding="utf-8")
            return PipelineStageResult(
                project=f"{self.context.project_spec.dataset} {self.context.project_spec.project_id}-{self.context.project_spec.bug_id}",
                stage="project",
                status=PipelineStageStatus.FAILED,
                message="Project environment validation failed",
            )

        if self.context.dry_run:
            report_payload["message"] = "Dry run: environment validated without compile or test execution"
            report_path.write_text(self._json_dump(report_payload), encoding="utf-8")
            return PipelineStageResult(
                project=f"{self.context.project_spec.dataset} {self.context.project_spec.project_id}-{self.context.project_spec.bug_id}",
                stage="project",
                status=PipelineStageStatus.SUCCESS,
                message="Project adapter validated in dry-run mode",
            )

        compilation_result = self.project.compile()
        report_payload["compilation_result"] = self._jsonable(compilation_result)
        if not compilation_result.success:
            report_payload["message"] = "Compilation failed"
            report_path.write_text(self._json_dump(report_payload), encoding="utf-8")
            return PipelineStageResult(
                project=f"{self.context.project_spec.dataset} {self.context.project_spec.project_id}-{self.context.project_spec.bug_id}",
                stage="project",
                status=PipelineStageStatus.FAILED,
                message="Compilation failed",
            )

        test_result = self.project.run_tests()

        report_payload["test_result"] = self._jsonable(test_result)
        report_payload["message"] = (
            f"Test execution completed with {test_result.failed} failing tests and {test_result.errors} execution errors"
        )
        report_path.write_text(self._json_dump(report_payload), encoding="utf-8")
        return PipelineStageResult(
            project=f"{self.context.project_spec.dataset} {self.context.project_spec.project_id}-{self.context.project_spec.bug_id}",
            stage="project",
            status=PipelineStageStatus.SUCCESS if test_result.errors == 0 else PipelineStageStatus.FAILED,
            message=report_payload["message"],
            metadata={
                "failing_tests": test_result.failed,
                "execution_errors": test_result.errors,
            },
        )

    def _run_preprocess_stage(self) -> PipelineStageResult:
        paths = self.context.settings.preprocess_paths
        if paths is None:
            raise ValueError("preprocess_paths are required for preprocess stage")
        runner = PreprocessStageRunner(
            project=self.project,
            project_spec=self.context.project_spec,
            paths=paths,
            test_case_id=self.context.test_case_id,
        )
        result = runner.run(
            dry_run=self.context.dry_run,
        )
        return PipelineStageResult(
            project=result.project,
            stage="preprocess",
            status=result.status,
            message=result.message,
            metadata={
                "output_dir": str(result.output_dir),
            },
        )

    def _run_localization_stage(self) -> PipelineStageResult:
        preprocess_paths = self.context.settings.preprocess_paths
        localization_paths = self.context.settings.localization_paths
        if preprocess_paths is None or localization_paths is None:
            raise ValueError("preprocess_paths and localization_paths are required for localization stage")
        runner = LocalizationStageRunner(
            project=self.project,
            paths=localization_paths,
            llm_settings=self.context.settings.llm,
            preprocess_dataset_path=preprocess_paths.output_dir,
            result_dir=self.context.result_dir,
            test_case_id=self.context.test_case_id,
        )
        result = runner.run()
        return PipelineStageResult(
            project=result.project,
            stage="localization",
            status=result.status,
            message=result.message,
            metadata={
                "output_dir": str(result.output_dir),
                "result_dir": str(result.result_dir),
                "result_path": str(result.result_path),
            },
        )

    def _json_dump(self, payload: object) -> str:
        return json.dumps(payload, indent=2, sort_keys=True, default=self._jsonable)

    def _jsonable(self, value: object) -> object:
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, Enum):
            return value.value
        try:
            return asdict(value)
        except TypeError:
            return value
