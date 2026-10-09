"""Load the shared static fault-context graph used by preprocessing and debugging."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

FAULT_CONTEXT_FILENAME = "fault_context.sqlite"


@dataclass(frozen=True)
class PreprocessContext:
    test_ids: tuple[str, ...]
    test_records: tuple[dict[str, object], ...]
    method_ids: tuple[str, ...]
    method_records: Sequence[dict[str, object]]
    metadata: dict[str, object]
    graph: object | None = None


class SourceMethodRecords(Sequence):
    """Materialize method source from the graph only when requested."""

    def __init__(self, graph, method_ids):
        self.graph = graph
        self.method_ids = method_ids

    def __len__(self):
        return len(self.method_ids)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return tuple(self.get(mid) for mid in self.method_ids[index])
        return self.get(self.method_ids[index])

    def get(self, method_id):
        entity_id = self.graph.aliases.get(method_id)
        if entity_id is None:
            return None
        entity = self.graph.entities[entity_id]
        content = entity["content"]
        loc = content.get("location") or {}
        language = self.graph.metadata.get("language", "c")
        return dict(
            method_id=method_id, method_name=entity.get("name"),
            class_name=content.get("class_name", loc.get("file")),
            source_code=self.graph.source_text(entity_id),
            file_path=loc.get("file"), start_line=loc.get("line"),
            end_line=loc.get("end_line"), metadata={},
            source=f"{language}_source",
        )


def get_method_record(context, method_id: str):
    records = context.method_records
    if isinstance(records, SourceMethodRecords):
        return records.get(method_id)
    return next((record for record in records if record.get("method_id") == method_id), None)


def resolve_preprocess_path(path: Path) -> Path:
    if path.is_dir():
        graph = path / FAULT_CONTEXT_FILENAME
        legacy = path / "evidence_graph.sqlite"
        return legacy if not graph.exists() and legacy.is_file() else graph
    if path.suffix != ".sqlite":
        raise ValueError("Expected a static fault-context SQLite file or preprocessing directory")
    if not path.exists() and path.name == "evidence_graph.sqlite":
        migrated = path.with_name(FAULT_CONTEXT_FILENAME)
        if migrated.is_file():
            return migrated
    return path


def load_preprocess_context(path: Path) -> PreprocessContext:
    from src.fault_graph.evidence_graph import EvidenceGraph

    path = resolve_preprocess_path(path)
    graph = EvidenceGraph.load(path)
    language = graph.metadata.get("language", "c")
    if language == "java":
        tests = []
        for stored in graph.metadata.get("test_records", []):
            record = dict(stored)
            test_id = str(record["test_id"])
            entity_id = record.get("source_entity_id")
            record["source_code"] = graph.source_text(entity_id) if entity_id else None
            report = graph.entities.get(f"report:{test_id}", {}).get("content", {}).get("text", "")
            record["metadata"] = dict(record.get("metadata") or {}, test_failure_message=report)
            tests.append(record)
    else:
        report = graph.entities["report"]["content"]["text"]
        program = graph.entities["program"]["content"]["text"]
        tests = [dict(test_id=graph.case_id, class_name="syzkaller", method_name=graph.case_id,
                      source_code=program, file_path=graph.metadata.get("syz_path"),
                      success=False, metadata={"test_failure_message": report})]
    method_ids = tuple(graph.aliases)
    test_ids = tuple(str(test["test_id"]) for test in tests)
    return PreprocessContext(
        test_ids=test_ids, test_records=tuple(tests), method_ids=method_ids,
        method_records=SourceMethodRecords(graph, method_ids),
        graph=graph,
        metadata=dict(graph.metadata, language=language, case_id=graph.case_id,
                      fault_context_paths={test_id: str(path.resolve()) for test_id in test_ids}),
    )
