from __future__ import annotations

from src.models import ProjectSpec
from src.project.base_project import Project
from src.project.cohiker_project import CoHikerProject
from src.project.defects4j_project import Defects4JProject
from src.project.java_project import JavaProject


class ProjectFactory:
    @staticmethod
    def create_project(spec: ProjectSpec) -> Project:
        dataset = spec.dataset.lower()
        if dataset == "defects4j":
            return Defects4JProject(spec)
        if dataset in {"cohiker", "recent", "recent_syz", "recentsyz"}:
            return CoHikerProject(spec)
        if dataset in {"maven", "gradle", "vul4j"}:
            build_tool_hint = dataset if dataset in {"maven", "gradle"} else None
            return JavaProject(spec, build_tool_hint=build_tool_hint)
        return JavaProject(spec)
