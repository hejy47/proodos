from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context


from config import LLMSettings
from src.localization.semantic_agent.workflow import LocalizationWorkflow
from src.preprocess.context import PreprocessContext, SourceMethodRecords, load_preprocess_context
from src.project.base_project import Project
from src.utils.failing_test_selection import select_failing_tests, selection_seed_from_key, is_instrumentable_test_id

@dataclass(frozen=True)
class FaultLocalizationEngine:
    llm_settings: LLMSettings
    preprocess_data: PreprocessContext
    output_dir: Path
    max_workers: int = 4
    project: Project | None = None

    def run(self) -> dict[str, object]:
        ranked_methods, explanations = self._localize_failing_tests_concurrently()
        return {"ranked_methods": ranked_methods, "explanation": "\n\n".join(explanations)}

    def _localize_failing_tests_concurrently(self) -> tuple[list[str], list[str]]:
        failing_tests = self._failing_tests()
        if not failing_tests:
            return [], []

        test_ids = [str(test["test_id"]) for test in failing_tests]
        # Kernel tools share a checkout and VM artifacts: never run cases in parallel.
        workers = 1 if self.preprocess_data.metadata.get("language") == "c" else max(1, self.max_workers)
        if workers == 1:
            results = [self._localize_test(test_id) for test_id in test_ids]
        else:
            with ThreadPoolExecutor(max_workers=min(workers, len(test_ids))) as pool:
                futures = [pool.submit(copy_context().run, self._localize_test, test_id) for test_id in test_ids]
                results = [future.result() for future in futures]
        ranked_methods: list[str] = []
        summaries = []
        for test_id, (candidates, reason) in zip(test_ids, results):
            for method_id in candidates:
                method_id = method_id.strip()
                if len(ranked_methods) >= 10 or not method_id or method_id in ranked_methods:
                    continue
                ranked_methods.append(method_id)
            summaries.append(reason if len(test_ids) == 1 else f"Test ID: {test_id}\n{reason}")
        return ranked_methods, summaries

    def _localize_test(self, test_id: str) -> tuple[list[str], str]:
        context = self.preprocess_data
        graph_path = context.metadata.get("fault_context_paths", {}).get(test_id)
        if (context.metadata.get("language") == "java"
                and isinstance(context.method_records, SourceMethodRecords) and graph_path):
            # Java failures run concurrently. Give each workflow its own SQLite
            # connection and query/source caches instead of sharing a reader.
            context = load_preprocess_context(Path(graph_path))
        agent = LocalizationWorkflow(
            llm_settings=self.llm_settings,
            preprocess_data=context,
            output_dir=self.output_dir,
            test_id=test_id,
            project=self.project,
        )
        try:
            return agent.run()
        finally:
            if context is not self.preprocess_data:
                context.method_records.graph.connection.close()

    def _failing_tests(self) -> list[dict[str, object]]:
        failing_tests = [
            test for test in self.preprocess_data.test_records
            if not test.get("success", True)
        ]
        if not failing_tests:
            return list(self.preprocess_data.test_records)
        seed_key = str(self.preprocess_data.metadata.get("case_id") or self.preprocess_data.metadata.get("project_path") or "")
        instrumentable = [
            test
            for test in failing_tests
            if is_instrumentable_test_id(str(test["test_id"]), method_name=str(test.get("method_name") or ""))
        ]
        return select_failing_tests(
            instrumentable or failing_tests,
            class_name_fn=self._test_class_name,
            test_id_fn=lambda test: str(test["test_id"]),
            seed=selection_seed_from_key(seed_key) if seed_key else None,
        )

    def _test_class_name(self, test: dict[str, object]) -> str:
        class_name = test.get("class_name")
        if isinstance(class_name, str) and class_name.strip():
            return class_name.strip()
        test_id = str(test.get("test_id", ""))
        if "::" in test_id:
            return test_id.split("::", 1)[0]
        return test_id
