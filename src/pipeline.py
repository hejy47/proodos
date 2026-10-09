"""Public command-line pipeline for preprocessing and automated debugging."""

from __future__ import annotations

import argparse
import os
import time
from dataclasses import dataclass
from pathlib import Path

from config import (
    RuntimeSettings,
    build_runtime_settings,
    resolve_output_dir,
    resolve_project_path,
)
from src.debug import DebugStageRunner
from src.models import (
    PipelineRunSummary,
    PipelineStageResult,
    PipelineStageStatus,
    ProjectSpec,
)
from src.preprocess import PreprocessStageRunner
from src.project import Project, ProjectFactory
from src.utils.output_paths import CaseOutputPaths


STAGE_ORDER = (
    "preprocess",
    "debug",
)


@dataclass(frozen=True)
class PipelineContext:
    settings: RuntimeSettings
    project_spec: ProjectSpec
    result_dir: Path
    test_case_id: str | None = None


class DebugPipeline:
    """Run static preprocessing and the unified debug/repair stage."""

    def __init__(self, context: PipelineContext):
        self.context = context
        self.project: Project = ProjectFactory.create_project(context.project_spec)

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "DebugPipeline":
        project_spec = ProjectSpec(
            dataset=args.dataset.lower(),
            project_path=resolve_project_path(args.project_path),
            project_id=args.project_id,
            bug_id=args.bug_id,
        )
        result_dir = resolve_output_dir(args.result_dir)
        os.environ["PROODOS_RESULT_DIR"] = str(result_dir)
        outputs = CaseOutputPaths.from_project(project_spec, result_dir)
        settings = build_runtime_settings(
            project_path=project_spec.project_path,
            preprocess_dir=outputs.preprocess_dir,
            debug_dir=outputs.debug_dir,
        )
        return cls(
            PipelineContext(
                settings=settings,
                project_spec=project_spec,
                result_dir=result_dir,
                test_case_id=args.test_case_id,
            )
        )

    def run(self, stage: str = "all") -> PipelineRunSummary:
        self._run_started_at = time.monotonic()
        selected_stage = stage.lower()
        stage_names = STAGE_ORDER if selected_stage == "all" else (selected_stage,)
        stage_runners = {
            "preprocess": self._run_preprocess_stage,
            "debug": self._run_debug_stage,
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
                        project=str(self.context.project_spec),
                    )
                )
                continue
            stage_results.append(runner())

        return PipelineRunSummary(
            project=self.context.project_spec,
            stage_results=stage_results,
            result_dir=self.context.result_dir,
        )

    def _run_preprocess_stage(self) -> PipelineStageResult:
        paths = self.context.settings.preprocess_paths
        if paths is None:
            raise ValueError("preprocess paths are required for preprocess stage")
        result = PreprocessStageRunner(
            project=self.project,
            project_spec=self.context.project_spec,
            paths=paths,
            test_case_id=self.context.test_case_id,
        ).run()
        return self._stage_result(
            "preprocess",
            result.message,
            status=result.status,
            metadata={"output_dir": str(result.output_dir)},
        )

    def _run_debug_stage(self) -> PipelineStageResult:
        preprocess_paths = self.context.settings.preprocess_paths
        debug_paths = self.context.settings.debug_paths
        if preprocess_paths is None or debug_paths is None:
            raise ValueError("preprocess and debug paths are required for debug stage")
        result = DebugStageRunner(
            project=self.project,
            paths=debug_paths,
            llm_settings=self.context.settings.llm,
            preprocess_dir=preprocess_paths.output_dir,
            result_dir=self.context.result_dir,
            test_case_id=self.context.test_case_id,
            started_at=getattr(self, "_run_started_at", None),
        ).run()
        return self._stage_result(
            "debug",
            result.message,
            status=result.status,
            metadata={
                "debug_dir": str(result.debug_dir),
                "result_dir": str(result.result_dir),
                "result_path": str(result.result_path),
            },
        )

    def _stage_result(
        self,
        stage: str,
        message: str,
        *,
        status: PipelineStageStatus = PipelineStageStatus.SUCCESS,
        failed: bool = False,
        metadata: dict[str, object] | None = None,
    ) -> PipelineStageResult:
        return PipelineStageResult(
            project=str(self.context.project_spec),
            stage=stage,
            status=PipelineStageStatus.FAILED if failed else status,
            message=message,
            metadata=metadata or {},
        )
