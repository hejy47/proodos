"""Case graph loading and readable entity tool responses for DebugDiagnosisAgent."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re

from src.fault_graph.evidence_graph import EvidenceGraph
from src.debug.semantic_agent.tools.tool_response import format_tool_result


def load_case_graph(dataset, test_id: str) -> EvidenceGraph:
    paths = (dataset.metadata.get("fault_context_paths")
             or dataset.metadata.get("evidence_graph_paths", {}))
    path = (os.environ.get("CAUSALFL_FAULT_CONTEXT_GRAPH")
            or os.environ.get("CAUSALFL_EVIDENCE_GRAPH")
            or paths.get(test_id))
    if path:
        from src.preprocess.context import SourceMethodRecords
        records = dataset.method_records
        graph = (records.graph if isinstance(records, SourceMethodRecords)
                 and not (os.environ.get("CAUSALFL_EVIDENCE_GRAPH") or os.environ.get("CAUSALFL_FAULT_CONTEXT_GRAPH"))
                 else EvidenceGraph.load(Path(path)))
        graph_tests = {str(test["test_id"]) for test in graph.metadata.get("test_records", [])}
        if graph.case_id != test_id and test_id not in graph_tests:
            raise ValueError("Fault context case_id does not match the debug test")
        return graph
    # In-memory contexts (e.g. tests) can provide records directly.
    graph = EvidenceGraph(test_id, metadata={"source_scope": "preprocessed_method_records"})
    for record in dataset.method_records:
        mid = str(record["method_id"])
        graph.add_entity("func:" + mid, "function", name=str(record.get("method_name") or mid.split("#")[-1]),
                         content=dict(method_id=mid, source_code=record.get("source_code"),
                                      location=dict(file=record.get("file_path"), line=record.get("start_line"),
                                                    end_line=record.get("end_line"))),
                         provenance=dict(extractor="preprocessed_method_records", source=record.get("source", "dataset")),
                         coverage={"status": "unavailable"})
    test = next((r for r in dataset.test_records if str(r["test_id"]) == test_id), {})
    graph.add_entity("report", "crash_report", name="crash report",
                     content=dict(text=test.get("metadata", {}).get("test_failure_message", "")),
                     provenance={"source": "test_record"})
    return graph


def tool_json(fn, *args, **kwargs) -> str:
    try:
        return json.dumps(fn(*args, **kwargs), ensure_ascii=False)
    except (ValueError, KeyError) as exc:
        return json.dumps({"error": str(exc)})


def _text_fields(values: dict, indent: str = "") -> str:
    """Render nested evidence fields without serializing them as JSON."""
    lines = []
    for key, value in values.items():
        if isinstance(value, dict):
            lines.append(f"{indent}{key}:")
            lines.append(_text_fields(value, indent + "  ") or indent + "  (none)")
        elif isinstance(value, list):
            lines.append(f"{indent}{key}:")
            if not value:
                lines.append(indent + "  (none)")
            for item in value:
                text = _text_fields(item) if isinstance(item, dict) else str(item)
                lines.append(indent + "  - " + text.replace("\n", "\n" + indent + "    "))
        else:
            text = "unknown" if value is None else str(value)
            lines.append(f"{indent}{key}: " + text.replace("\n", "\n" + indent + "  "))
    return "\n".join(lines)


def _location_text(location: dict | None) -> str:
    location = location or {}
    text = str(location.get("file") or "unknown")
    if location.get("line") is not None:
        text += f":{location['line']}"
        if location.get("end_line") is not None:
            text += f"-{location['end_line']}"
    return text


def _compact(text: str, limit: int = 190) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _function_brief(content: dict, name: str) -> str:
    source = str(content.get("source_code") or "")
    signature = ""
    if source:
        signature = _compact(source.split("{", 1)[0], 100)
    statements = []
    fallback_statements = []
    body = source.split("{", 1)[1] if "{" in source else ""
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line in {"{", "}"} or line.startswith(("/*", "*", "//", "#")):
            continue
        # Prefer an executable statement over local declarations. A call in a
        # declaration initializer is still useful when no later statement is
        # available, so keep it as a fallback.
        if re.match(r"^(?:const\s+)?(?:struct\s+\w+|unsigned|signed|bool|int|long|short|u\d+)\b.*;\s*$", line):
            continue
        if "(" in line or line.startswith(("return ", "if ", "for ", "while ", "switch ")):
            compact = _compact(re.sub(r"[;}\s]+$", "", line), 120)
            if re.search(r"\b(?:memcpy|memmove|memset|copy\w*|alloc\w*|free\w*|register\w*|crypto\w*)\s*\(", line) or line.startswith("return "):
                statements.append(compact)
                break
            fallback_statements.append(compact)
    if not statements and fallback_statements:
        statements.append(fallback_statements[0])
    if not statements:
        for raw in body.splitlines():
            line = raw.strip()
            if line and line not in {"{", "}"} and not line.startswith(("/*", "*", "//", "#")):
                statements.append(_compact(re.sub(r"[;}\s]+$", "", line), 120))
                break
    if signature and statements:
        return f"Function {name}; first relevant operation: {statements[0]}."
    if signature:
        return f"Function {name}; signature: {signature}."
    return f"Function {name} at {_location_text(content.get('location'))}."


def _entity_brief(graph: EvidenceGraph, item: dict) -> str:
    try:
        entity = graph.entities[graph.resolve(item["entity_id"])]
        content = entity.get("content") or {}
    except (KeyError, ValueError):
        content = {}
    kind = item.get("entity_type")
    name = str(item.get("name") or kind or "entity")
    if kind == "function":
        if not content.get("source_code"):
            try:
                source = graph.source_text(item["entity_id"])
            except (KeyError, ValueError):
                source = None
            if source:
                content = dict(content, source_code=source)
        return _compact(_function_brief(content, name), 240)
    if kind == "syscall":
        definitions = len(content.get("definitions") or [])
        uses = len(content.get("uses") or [])
        return f"Syscall {name}; defines {definitions} explicit resource(s), uses {uses}."
    if kind == "crash_report":
        report = str(content.get("text") or "")
        first = next((line.strip() for line in report.splitlines() if line.strip()), "Failure report")
        trace = "includes a call trace" if "Call Trace:" in report else "call trace unavailable"
        return _compact(f"{first}; {trace}.", 240)
    if kind == "lifecycle_operation":
        operation = content.get("operation_kind") or "operation"
        api = content.get("api") or name
        return _compact(f"Lifecycle {operation} via {api} at {_location_text(content.get('location'))}.", 240)
    if kind == "explicit_resource":
        reference = content.get("reference") or name
        return f"Explicit syzkaller resource {reference}; type is not inferred."
    if content.get("source_code"):
        return _compact(f"{kind} {name}; source entity at {_location_text(content.get('location'))}.", 240)
    return _compact(f"{kind} {name}.", 240)


def search_entities_text(graph: EvidenceGraph, query: str, entity_type: str | None = None,
                         operation_kind: str | None = None, limit: int = 10,
                         cursor: str | None = None) -> str:
    try:
        result = graph.search_code(query, entity_type, operation_kind, min(limit, 10), cursor)
    except (ValueError, KeyError) as exc:
        return format_tool_result("search_entities", status="error", summary=str(exc))
    cards = []
    for index, item in enumerate(result["items"], 1):
        lines = [f"[{index}] entity_id: {item['entity_id']}",
                 f"    entity_type: {item['entity_type']}", f"    name: {item['name']}"]
        if item.get("method_id"):
            lines.append(f"    method_id: {item['method_id']}")
        if item.get("location"):
            lines.append(f"    location: {_location_text(item['location'])}")
        lines.append(f"    brief: {_entity_brief(graph, item)}")
        if item.get("operation_kind"):
            lines.append(f"    operation_kind: {item['operation_kind']}")
        terms = item.get("match_reason", {}).get("matched_terms", [])
        if terms:
            lines.append("    match_reason: matched terms: " + ", ".join(terms))
        cards.append("\n".join(lines))
    cards.extend([f"Showing {len(result['items'])} of {result['total']} matched entities.",
                  f"index_scope: {result['index_scope']}",
                  f"next_cursor: {result['next_cursor'] or '(none)'}"])
    return format_tool_result(
        "search_entities", status="success" if result["items"] else "empty",
        summary=f'Found {len(result["items"])} candidate entities for query "{query}".', fields=cards,
    )


def read_entity_text(graph: EvidenceGraph, entity_id: str, offset: int = 0,
                     limit: int = 12000) -> str:
    try:
        entity_id = graph.resolve(entity_id)
        if offset < 0 or not 1 <= limit <= 30000:
            raise ValueError("offset must be nonnegative; limit must be between 1 and 30000")
        entity = graph.entities[entity_id]
    except (ValueError, KeyError) as exc:
        return format_tool_result("read_entity", status="error", summary=str(exc))
    content = entity["content"]
    location = content.get("location")
    fields = [f"entity_id: {entity_id}", f"entity_type: {entity['entity_type']}"]
    if content.get("method_id"):
        fields.append(f"method_id: {content['method_id']}")
    if location:
        fields.append(f"location: {_location_text(location)}")
    if "source_code" in content or entity["entity_type"] == "function":
        # Kernel graphs keep source spans and a source-root manifest instead
        # of copying every function body into SQLite.  Materialize the body
        # only for this read request.
        body = graph.source_text(entity_id) or "(source not available)"
        language = "c" if str((location or {}).get("file", "")).endswith((".c", ".h")) else "java"
        label = "Source"
    elif entity["entity_type"] == "crash_report":
        body, language, label = str(content.get("text") or ""), "text", "Report"
    else:
        body, language, label = _text_fields(content), "text", "Content"
    if offset > len(body):
        return format_tool_result("read_entity", status="error", summary="offset is outside the content")
    page = body[offset:offset + limit]
    next_offset = offset + limit if offset + limit < len(body) else None
    fields.append(f"{label}:\n```{language}\n{page}\n```")
    fields.append(_text_fields({"provenance": entity["provenance"]}))
    fields.extend([f"total_chars: {len(body)}", f"next_offset: {next_offset if next_offset is not None else '(none)'}"])
    # Preserve the existing causal handoff contract, recording only the page seen.
    graph._queried[f"{entity_id}@{offset}"] = dict(
        entity_id=entity_id, entity_type=entity["entity_type"], content=page,
        content_format="text", provenance=entity["provenance"],
        total_chars=len(body), next_offset=next_offset,
    )
    return format_tool_result("read_entity", status="success",
                              summary=f"{label} for {entity_id}.", fields=fields)
