"""Shared case layout: working artifacts under log, rankings under results."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re

from src.models import ProjectSpec


REPO_ROOT = Path(__file__).resolve().parents[2]


def default_output_root(repo_root: Path = REPO_ROOT) -> Path:
    return Path(os.environ.get("CAUSALFL_OUTPUT_ROOT") or repo_root / "output").expanduser().resolve()


def _path_component(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", value):
        raise ValueError(f"Invalid dataset or case identifier: {value!r}")
    return value


def project_case_id(spec: ProjectSpec) -> str:
    if spec.dataset.lower() == "defects4j" and spec.project_id and spec.bug_id:
        return _path_component(f"{spec.project_id.lower()}-{spec.bug_id}")
    return _path_component(str(spec.bug_id or spec.project_id or spec.project_path.name))


def ranking_filename(case_id: str) -> str:
    return f"{_path_component(case_id)}_ranking.json"


@dataclass(frozen=True)
class CaseOutputPaths:
    dataset: str
    case_id: str
    output_root: Path = field(default_factory=default_output_root)

    def __post_init__(self):
        _path_component(self.dataset)
        _path_component(self.case_id)

    @classmethod
    def from_project(cls, spec: ProjectSpec, output_root: Path | None = None):
        return cls(spec.dataset.lower(), project_case_id(spec),
                   output_root if output_root is not None else default_output_root())

    @property
    def log_dir(self) -> Path:
        return self.output_root / "log" / self.dataset / self.case_id

    @property
    def preprocess_dir(self) -> Path:
        return self.log_dir / "preprocess"

    @property
    def localization_dir(self) -> Path:
        return self.log_dir / "localization"

    @property
    def runtime_dir(self) -> Path:
        return self.log_dir / "runtime"

    @property
    def result_dir(self) -> Path:
        return self.output_root / "results" / self.dataset

    @property
    def ranking_path(self) -> Path:
        return self.result_dir / ranking_filename(self.case_id)


def kernel_runtime_dir(case_id: str) -> Path:
    dataset = os.environ.get("CAUSALFL_KERNEL_DATASET", "cohiker").strip().lower()
    if dataset in {"recent", "recent_syz", "recentsyz"}:
        dataset = "recent_syz"
    else:
        dataset = "cohiker"
    return CaseOutputPaths(dataset, case_id).runtime_dir
