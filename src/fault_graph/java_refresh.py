"""Transactional file and failure-context refreshes after accepted Java repairs."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3

from src.fault_graph.evidence_graph import EvidenceGraph
from src.fault_graph.evidence_sqlite import _compact_entity, _compact_relation
from src.fault_graph.java_evidence import index_java_source
from src.fault_graph.java_repair_index import write_repair_tables
from src.fault_graph.method_id_map import resolve_crash_method_id
from src.utils.java_source import JavaMethodDescriptor
from src.utils.java_util import split_test_id


def has_repair_index(path):
    if not Path(path).is_file():
        return False
    with closing(sqlite3.connect(path)) as db:
        return db.execute("SELECT 1 FROM sqlite_master WHERE name='repair_methods'").fetchone() is not None


def _catalog(db, excluded_files=()):
    result = []
    for raw, in db.execute("SELECT payload FROM repair_methods"):
        record = json.loads(raw)
        if record["file"] in excluded_files or "descriptor" not in record:
            continue
        values = dict(record["descriptor"])
        values["file_path"] = Path(values["file_path"])
        values["enclosing_classes"] = tuple(values["enclosing_classes"])
        result.append((JavaMethodDescriptor(**values), record["entity_id"]))
    return result


def _remove_search_document(db, node_id):
    row = db.execute("SELECT document FROM entity_documents WHERE node_id=?", (node_id,)).fetchone()
    if row:
        db.execute("INSERT INTO entity_search(entity_search,rowid,document) VALUES ('delete',?,?)", (node_id, row[0]))
        db.execute("DELETE FROM entity_documents WHERE node_id=?", (node_id,))


def _replace_fragment(db, graph, previous):
    """Preserve stable node IDs and incoming edges to unchanged method IDs."""
    real = {eid: e for eid, e in graph.entities.items() if not e.get("reference_only")}
    previous = dict(previous)
    for eid, node_id in previous.items():
        _remove_search_document(db, node_id)
        db.execute("DELETE FROM relations WHERE source_node=?", (node_id,))
        if eid not in real:
            db.execute("DELETE FROM relations WHERE target_node=?", (node_id,))
            db.execute("DELETE FROM aliases WHERE entity_id=?", (eid,))
            db.execute("DELETE FROM entities WHERE node_id=?", (node_id,))
    for eid, entity in real.items():
        compact, file_name, start, end = _compact_entity(entity)
        file_id = db.execute("SELECT file_id FROM source_files WHERE path=?", (file_name,)).fetchone()[0] if file_name else None
        content = entity["content"]
        values = (entity["entity_type"], str(entity.get("name", "")), content.get("method_id"),
                  content.get("operation_kind"), int(entity.get("coverage", {}).get("status") == "observed"),
                  file_id, start, end, compact, eid)
        if eid in previous:
            db.execute("UPDATE entities SET kind=?,name=?,method_id=?,operation_kind=?,observed=?,file_id=?,start_byte=?,end_byte=?,payload=? WHERE public_id=?", values)
        else:
            db.execute("INSERT INTO entities(kind,name,method_id,operation_kind,observed,file_id,start_byte,end_byte,payload,public_id) VALUES (?,?,?,?,?,?,?,?,?,?)", values)
        node_id = db.execute("SELECT node_id FROM entities WHERE public_id=?", (eid,)).fetchone()[0]
        doc = graph._documents[eid]
        db.execute("INSERT INTO entity_documents VALUES (?,?)", (node_id, doc))
        db.execute("INSERT INTO entity_search(rowid,document) VALUES (?,?)", (node_id, doc))
    for method_id, eid in graph.aliases.items():
        if eid in real:
            db.execute("INSERT OR REPLACE INTO aliases VALUES (?,?)", (method_id, eid))
    for rid, edge in graph.relations.items():
        source = db.execute("SELECT node_id FROM entities WHERE public_id=?", (edge["source"],)).fetchone()
        target = db.execute("SELECT node_id FROM entities WHERE public_id=?", (edge["target"],)).fetchone()
        if source is None or target is None:
            raise ValueError("Refresh produced a missing relation endpoint")
        db.execute("INSERT OR REPLACE INTO relations VALUES (?,?,?,?,?)", (rid, source[0], target[0], edge["relation_type"], _compact_relation(edge)))


def refresh_java_files(path, project, changed_files):
    """Parse only changed production files; atomically replace their SQLite records."""
    root = project.project_path.resolve()
    files = sorted(set(changed_files))
    if not files:
        return
    resolved = [(root / file).resolve() for file in files]
    production_roots = [p.resolve() for p in project.discover_source_roots()]
    test_roots = [p.resolve() for p in project.discover_test_roots()]
    if any(p.suffix != ".java" or not p.is_relative_to(root) or not any(p.is_relative_to(s) for s in production_roots)
           or any(p.is_relative_to(t) for t in test_roots) for p in resolved):
        raise ValueError("Only accepted production Java files may be refreshed")
    with closing(sqlite3.connect(path)) as db, db:
        payload = json.loads(db.execute("SELECT payload FROM metadata").fetchone()[0])
        metadata = payload["metadata"]
        known = [r[0] for r in db.execute("SELECT type_id FROM repair_types")]
        graph = EvidenceGraph(payload["case_id"], metadata=dict(source_root=str(root), language="java"))
        index_java_source(graph, root, test_roots, files=resolved, external_methods=_catalog(db, files), known_types=known)
        if any(r["status"] != "ok" for r in graph.repair_index["files"]):
            raise ValueError("Accepted source could not be parsed; the previous SQLite index was retained")
        previous = []
        for file in files:
            file_id = db.execute("SELECT file_id FROM source_files WHERE path=?", (file,)).fetchone()
            if file_id is None:
                raise ValueError(f"Accepted file was not in the baseline index: {file}")
            previous.extend(db.execute("SELECT public_id,node_id FROM entities WHERE file_id=?", file_id))
            db.execute("UPDATE source_files SET sha256=?,encoding=? WHERE path=?",
                       (graph.metadata["source_sha256"][file], graph.metadata["source_encodings"][file], file))
        _replace_fragment(db, graph, previous)
        write_repair_tables(db, graph.repair_index, changed_files=files)
        metadata.setdefault("source_sha256", {}).update(graph.metadata["source_sha256"])
        metadata.setdefault("source_encodings", {}).update(graph.metadata["source_encodings"])
        metadata["source_root"] = str(root)
        metadata["diagnostics"] = [d for d in metadata.get("diagnostics", []) if d.get("file") not in files]
        metadata["diagnostics"].extend(graph.metadata.get("diagnostics", []))
        db.execute("UPDATE metadata SET payload=?", (json.dumps(payload, ensure_ascii=False),))
        # Check the new spans against exactly the bytes that were parsed.
        import hashlib
        for file in files:
            if hashlib.sha256((root / file).read_bytes()).hexdigest() != graph.metadata["source_sha256"][file]:
                raise ValueError("Source changed during index refresh")


def update_java_failure_context(path, failing_tests):
    """Update failure reports from the latest regression without rescanning Java."""
    with closing(sqlite3.connect(path)) as db, db:
        payload = json.loads(db.execute("SELECT payload FROM metadata").fetchone()[0])
        metadata = payload["metadata"]
        graph = EvidenceGraph(payload["case_id"])
        methods = {eid.removeprefix("func:"): descriptor for descriptor, eid in _catalog(db)}
        tests_by_name = {}
        for eid, mid, raw, file in db.execute("SELECT e.public_id,e.method_id,e.payload,s.path FROM entities e LEFT JOIN source_files s ON e.file_id=s.file_id WHERE e.kind='test_method'"):
            content = json.loads(raw)["content"]
            class_name, rest = mid.split("#", 1)
            name = rest.split("(", 1)[0]
            tests_by_name[f"{class_name}::{name}"] = (eid, file, content.get("location") or {})
        records, crash_points = [], {}

        def reference(eid):
            if eid not in graph.entities:
                graph.add_entity(eid, "source_reference", content={}, provenance={}, reference_only=True)

        for test in failing_tests:
            class_name, method_name = split_test_id(test.test_id)
            source_id, file, _ = tests_by_name.get(test.test_id, (None, None, {}))
            records.append(dict(test_id=test.test_id, class_name=class_name, method_name=method_name,
                                source_entity_id=source_id, file_path=file, success=False,
                                metadata=dict(outcome_source="current_regression")))
            report = test.stack_trace or test.failure_message or "No failure report available"
            report_id = "report:" + test.test_id
            graph.add_entity(report_id, "crash_report", name=f"failure report: {test.test_id}",
                             content=dict(text=report), provenance=dict(source="current_regression", extractor="failure_report_import"))
            if source_id:
                reference(source_id)
                graph.add_relation(report_id, "failure_of", source_id, dict(source="current_regression"))
            crash = resolve_crash_method_id(report, list(methods), source_methods_by_spectra=methods)
            if crash:
                crash_points[test.test_id] = crash
                reference("func:" + crash)
                graph.add_relation(report_id, "reports_frame", "func:" + crash, dict(source="current_regression", extractor="java_stack_frame"))
        previous = list(db.execute("SELECT public_id,node_id FROM entities WHERE kind='crash_report'"))
        _replace_fragment(db, graph, previous)
        metadata.update(test_records=records, crash_points=crash_points)
        db.execute("UPDATE metadata SET payload=?", (json.dumps(payload, ensure_ascii=False),))
