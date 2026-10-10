from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import shutil

from config import PathSettings
from src.models import PipelineStageStatus, ProjectSpec
from src.project import Project
from src.project.kernel_tree import (
    KernelTree,
    is_kernel_dataset,
    is_recent_syz_dataset,
    kernel_git_head,
    resolve_kernel_report_path,
    resolve_kernel_syz_path,
    seed_relpaths_for_case,
)
from src.preprocess.context import FAULT_CONTEXT_FILENAME



@dataclass(frozen=True)
class PreprocessRunSummary:
    project: str
    output_dir: Path
    status: PipelineStageStatus
    message: str


class PreprocessStageRunner:
    def __init__(
        self,
        *,
        project: Project,
        project_spec: ProjectSpec,
        paths: PathSettings,
    ):
        self.project = project
        self.project_spec = project_spec
        self.paths = paths

    def run(self) -> PreprocessRunSummary:
        output_dir = self.paths.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)

        print(
            f"Preprocess {self.project_spec.dataset} {self.project_spec.project_id}-{self.project_spec.bug_id}...",
            flush=True,
        )

        # Kernel cases are represented by a static fault-context graph. Runtime
        # instrumentation belongs to debug-time tools and must not create
        # an instrumentation directory during preprocessing.
        if is_kernel_dataset(self.project_spec.dataset):
            result = self._run_kernel_static_graph()
        else:
            result = self._run_java_static_graph()

        if result.status == PipelineStageStatus.SUCCESS:
            print("Preprocess finished", flush=True)
        else:
            print(f"Preprocess failed: {result.message}", flush=True)
        return result

    def _run_java_static_graph(self) -> PreprocessRunSummary:
        from src.fault_graph.java_evidence import build_java_evidence

        output_dir = self.paths.output_dir
        case_id = self.project_spec.bug_id or self.project_spec.project_id or self.project.project_path.name
        try:
            graph = build_java_evidence(project=self.project, case_id=case_id)
            # Replace old preprocessing artifacts only after input/index validation.
            if output_dir.exists():
                shutil.rmtree(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            graph_path = output_dir / FAULT_CONTEXT_FILENAME
            graph.save(graph_path)
            summary = dict(
                case_id=case_id, language="java", entities=len(graph.entities),
                relations=len(graph.relations), functions=len(graph.aliases),
                tests=sum(entity["entity_type"] == "test_method" for entity in graph.entities.values()),
                source_files=graph.metadata["source_file_count"], graph_path=str(graph_path),
                collection_strategy="static_fault_context",
                instrumentation=False, coverage_imported=False, test_execution=False,
                index_scope=graph.metadata["source_scope"],
                diagnostics=graph.metadata.get("diagnostics", []),
            )
            (output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        except Exception as exc:
            return self._failure(f"Failed to build static Java graph: {exc}")
        return PreprocessRunSummary(
            project=f"{self.project_spec.dataset} {case_id}", output_dir=output_dir,
            status=PipelineStageStatus.SUCCESS,
            message=f"Preprocess completed ({len(graph.aliases)} methods, {len(graph.relations)} relations)",
        )

    def _run_kernel_static_graph(self) -> PreprocessRunSummary:
        """Build the case fault context from the checked-out kernel tree and artifacts."""
        output_dir = self.paths.output_dir
        # Remove artifacts from older preprocessing implementations, including
        # spectra/trace files, before writing the graph.
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        case_id = getattr(self.project, "case_id", None) or self.project_spec.bug_id or self.project_spec.project_id
        if not case_id:
            return self._failure("A CoHiker case id is required")

        source_root = getattr(self.project, "linux_dir", None)
        source_root = Path(source_root) if source_root is not None else Path("/root/linux")
        if not source_root.is_dir():
            return self._failure(
                "Kernel static preprocessing must run inside test1 or provide COHIKER_LINUX_DIR"
            )

        dataset_root = Path(self.project_spec.project_path)
        syz_path = resolve_kernel_syz_path(case_id, dataset_root)
        report_path = resolve_kernel_report_path(case_id, dataset_root)
        if syz_path is None or report_path is None:
            missing = "testcase" if syz_path is None else "bug report"
            return self._failure(
                f"Required kernel {missing} is missing for case {case_id}"
            )

        expected_commit = os.environ.get("PROODOS_KERNEL_COMMIT", "").strip()
        commit_path = dataset_root / "datasets" / "case_commit.txt"
        if not expected_commit and commit_path.is_file():
            commits = {
                line.split(":", 1)[0].strip(): line.split(":", 1)[1].strip()
                for line in commit_path.read_text(encoding="utf-8", errors="replace").splitlines()
                if ":" in line
            }
            expected_commit = commits.get(case_id, "")
        actual_commit = kernel_git_head(source_root)
        if not actual_commit:
            if is_recent_syz_dataset(self.project_spec.dataset):
                actual_commit = expected_commit or os.environ.get("PROODOS_KERNEL_VERSION", "unknown")
            else:
                return self._failure(
                    f"Unable to read checked-out kernel commit at {source_root}"
                )
        if expected_commit and not actual_commit.startswith(expected_commit):
            return self._failure(
                f"Kernel commit mismatch: case expects {expected_commit}, source has {actual_commit}"
            )
        if not expected_commit and not is_recent_syz_dataset(self.project_spec.dataset):
            return self._failure(
                f"Required CoHiker artifact does not exist: {commit_path}"
            )

        try:
            from src.fault_graph.kernel_evidence import build_kernel_evidence
            from src.fault_graph.kernel_oops import parse_kernel_oops_frames
            from src.fault_graph.lifecycle import run_lifecycle_rules

            # Index the complete checked-out kernel.  Callback implementations
            # are frequently selected through function pointers and therefore
            # cannot be recovered reliably from report frames or lexical call
            # hops alone.  The compact SQLite writer stores source spans and
            # file digests, so this full index does not duplicate source text.
            index_scope = os.environ.get("COHIKER_KERNEL_INDEX_SCOPE", "full").strip().lower()
            if index_scope in {"seeded", "case"}:
                rel_paths = seed_relpaths_for_case(
                    case_id=case_id,
                    dataset_root=dataset_root,
                    kernel=KernelTree(linux_dir=source_root),
                    extra_hops=1,
                    max_files=80,
                )
            elif index_scope in {"full", "all"}:
                rel_paths = None
            else:
                return self._failure(
                    "COHIKER_KERNEL_INDEX_SCOPE must be 'full' or 'seeded'"
                )
            frame_paths = list(dict.fromkeys(
                frame.file_path for frame in parse_kernel_oops_frames(
                    report_path.read_text(encoding="utf-8", errors="replace")
                ) if frame.file_path
            ))
            if rel_paths is not None:
                # Keep every concrete source file named by the report for the
                # optional seeded mode, even across top-level subsystems.
                for rel_path in frame_paths:
                    if (rel_path and rel_path.endswith((".c", ".h"))
                        and (source_root / rel_path).is_file() and rel_path not in rel_paths):
                        rel_paths.append(rel_path)
            if rel_paths is not None and not rel_paths:
                return self._failure(
                    "No source files could be seeded from the kernel crash report"
                )
            # Lifecycle matching is deliberately scoped to report files by
            # default.  This captures ownership operations in the failing
            # subsystem without turning preprocessing into a whole-kernel
            # Coccinelle scan.  A caller can widen/narrow it with an explicit
            # source-relative scope (comma-separated files or directories).
            lifecycle_path = output_dir / "lifecycle.json"
            configured_scope = os.environ.get("COHIKER_LIFECYCLE_SCOPE", "").strip()
            lifecycle_scope = ([item.strip() for item in configured_scope.split(",") if item.strip()]
                               if configured_scope else [item for item in frame_paths
                                                         if item and (source_root / item).is_file()])
            lifecycle_error = None
            if lifecycle_scope:
                try:
                    run_lifecycle_rules(
                        source_root, lifecycle_path,
                        scope=lifecycle_scope,
                        timeout=int(os.environ.get("COHIKER_LIFECYCLE_TIMEOUT", "600")),
                    )
                except Exception as exc:  # lifecycle is incomplete evidence, not a hard gate
                    lifecycle_error = f"Coccinelle lifecycle analysis unavailable: {exc}"
                    lifecycle_path.unlink(missing_ok=True)
            graph = build_kernel_evidence(
                case_id=case_id,
                source_root=source_root,
                syz_path=syz_path,
                report_path=report_path,
                kernel_commit=actual_commit,
                retain_source=False,
                rel_paths=rel_paths,
                lifecycle_path=lifecycle_path if lifecycle_path.is_file() else None,
            )
            graph.metadata["collection_strategy"] = "static_fault_context"
            if lifecycle_error:
                graph.metadata.setdefault("diagnostics", []).append(
                    {"kind": "lifecycle_analysis_unavailable", "message": lifecycle_error}
                )
            graph_path = output_dir / FAULT_CONTEXT_FILENAME
            graph.save(graph_path)
            summary = {
                "case_id": case_id,
                "entities": len(graph.entities),
                "relations": len(graph.relations),
                "functions": len(graph.aliases),
                "graph_path": str(graph_path),
                "dataset_path": str(graph_path),
                "coverage_imported": False,
                "instrumentation": False,
                "index_scope": "all_c_and_h_under_source_root" if rel_paths is None else "case_seeded_source_files",
                "lifecycle_imported": lifecycle_path.is_file(),
            }
            (output_dir / "summary.json").write_text(
                json.dumps(summary, indent=2) + "\n", encoding="utf-8"
            )
        except Exception as exc:
            return self._failure(f"Failed to build static kernel graph: {exc}")

        return PreprocessRunSummary(
            project=f"{self.project_spec.dataset} {self.project_spec.project_id}-{self.project_spec.bug_id}",
            output_dir=output_dir,
            status=PipelineStageStatus.SUCCESS,
            message=(
                f"Static fault context completed ({len(graph.entities)} entities, "
                f"{len(graph.relations)} relations); runtime instrumentation skipped"
            ),
        )

    def _failure(self, message: str) -> PreprocessRunSummary:
        return PreprocessRunSummary(
            project=f"{self.project_spec.dataset} {self.project_spec.project_id}-{self.project_spec.bug_id}",
            output_dir=self.paths.output_dir,
            status=PipelineStageStatus.FAILED,
            message=message,
        )
