"""Shared output layout for one debugging/repair case."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re

from src.models import ProjectSpec


REPO_ROOT = Path(__file__).resolve().parents[2]


def default_result_dir(repo_root: Path = REPO_ROOT) -> Path:
    """Return the root directory for generated case artifacts."""
    return Path(
        os.environ.get("CAUSALFL_RESULT_DIR") or repo_root / "output"
    ).expanduser().resolve()


def _path_component(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", value):
        raise ValueError(f"Invalid dataset or case identifier: {value!r}")
    return value


def project_case_id(spec: ProjectSpec) -> str:
    if spec.dataset.lower() == "defects4j" and spec.project_id and spec.bug_id:
        return _path_component(f"{spec.project_id.lower()}-{spec.bug_id}")
    return _path_component(str(spec.bug_id or spec.project_id or spec.project_path.name))


def patch_filename(case_id: str) -> str:
    """Return the standard unified-diff artifact name for one repaired case."""
    return f"{_path_component(case_id)}.patch"


@dataclass(frozen=True)
class CaseOutputPaths:
    """All artifacts for a case below one caller-provided result directory."""

    dataset: str
    case_id: str
    result_dir: Path = field(default_factory=default_result_dir)

    def __post_init__(self):
        _path_component(self.dataset)
        _path_component(self.case_id)

    @classmethod
    def from_project(cls, spec: ProjectSpec, result_dir: Path | None = None):
        return cls(
            spec.dataset.lower(),
            project_case_id(spec),
            result_dir if result_dir is not None else default_result_dir(),
        )

    @property
    def log_dir(self) -> Path:
        return self.result_dir / "log" / self.dataset / self.case_id

    @property
    def preprocess_dir(self) -> Path:
        return self.log_dir / "preprocess"

    @property
    def debug_dir(self) -> Path:
        return self.log_dir / "debug"

    @property
    def patch_dir(self) -> Path:
        return self.result_dir / "results" / self.dataset

    @property
    def patch_path(self) -> Path:
        return self.patch_dir / patch_filename(self.case_id)
