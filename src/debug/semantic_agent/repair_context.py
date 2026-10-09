"""Small, on-demand assertion and call-chain context for patch generation.

Reads the existing source index without modifying preprocessing. Static call
paths are navigation candidates, not dynamic slices or inferred specifications.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import re

from src.fault_graph.method_id_map import _parse_jvm_parameter_types
from src.utils.java_source import CLASS_LIKE_TYPES, _java_parser


CONTEXT_LIMIT = 6000
FAILURE_LIMIT = 2000
MAX_DEPTH = 8
MAX_SEARCH_NODES = 120
MAX_NEIGHBORS = 100
_FRAME = re.compile(r"^\s*at\s+([\w.$/<>-]+)\(([^():]+\.java):(\d+)\)", re.MULTILINE)


def _clip(text, limit):
    return text if len(text) <= limit else text[:max(0, limit - 16)].rstrip() + "\n...[truncated]"


def _name(method_id):
    owner, _, signature = method_id.split(":", 1)[-1].partition("#")
    return owner.rsplit(".", 1)[-1] + "." + signature.split("(", 1)[0]


def _marked(text, start_line, line, label):
    lines = text.splitlines(keepends=True)
    index = line - start_line if line is not None else -1
    if 0 <= index < len(lines):
        indent = re.match(r"\s*", lines[index]).group().rstrip("\r\n")
        lines.insert(index, f"{indent}// >>> {label}\n")
    return "".join(lines)


@dataclass
class _Source:
    entity_id: str
    method_id: str
    file: str
    line: int
    text: str

    def nodes(self):
        data = ("class RepairContext {\n" + self.text + "\n}").encode("utf-8")
        tree = _java_parser().parse(data)
        pending = list(tree.root_node.named_children)
        while pending:
            node = pending.pop()
            # Walk the wrapper, but exclude nested/anonymous class declarations.
            if node.type in CLASS_LIKE_TYPES and node.start_byte != 0:
                continue
            if node.type == "class_body" and node.parent.type in {"object_creation_expression", "enum_constant"}:
                continue
            yield node, data
            pending.extend(reversed(node.named_children))

    def assertions(self):
        found = {}
        for node, data in self.nodes():
            if node.type == "method_invocation":
                name = node.child_by_field_name("name")
                name = data[name.start_byte:name.end_byte].decode() if name else ""
                if not (re.match(r"assert[A-Z_]", name) or name == "fail"):
                    continue
                while node.parent and node.parent.type not in {"block", "constructor_body"}:
                    node = node.parent
            elif node.type != "assert_statement":
                continue
            start = self.line + node.start_point.row - 1
            end = self.line + node.end_point.row - 1
            found[(start, end)] = data[node.start_byte:node.end_byte].decode("utf-8")
        return [(start, end, value) for (start, end), value in sorted(found.items())]

    def excerpt(self, line=None, callee=None):
        if callee:
            owner, _, signature = callee.partition("#")
            name = signature.split("(", 1)[0]
            arity = len(_parse_jvm_parameter_types(signature) or [])
            nodes = list(self.nodes())
            hints = {}
            for node, data in nodes:
                if node.type in {"formal_parameter", "local_variable_declaration"}:
                    declared = node.child_by_field_name("type")
                    if declared:
                        variables = [node] if node.type == "formal_parameter" else node.named_children
                        for variable in variables:
                            ident = variable.child_by_field_name("name")
                            if ident:
                                hints.setdefault(data[ident.start_byte:ident.end_byte].decode(), set()).add(
                                    data[declared.start_byte:declared.end_byte].decode())
            candidates = []
            for node, data in nodes:
                called = node.child_by_field_name("name") if node.type == "method_invocation" else None
                if not called or data[called.start_byte:called.end_byte].decode() != name:
                    continue
                arguments = node.child_by_field_name("arguments")
                if arguments is None or len([a for a in arguments.named_children if a.type not in {"line_comment", "block_comment"}]) != arity:
                    continue
                receiver = node.child_by_field_name("object")
                score = 1  # Unknown receivers remain candidates.
                hint = None
                if receiver is None or receiver.type == "this":
                    hint = self.method_id.split("#", 1)[0]
                elif receiver.type == "object_creation_expression":
                    declared = receiver.child_by_field_name("type")
                    hint = data[declared.start_byte:declared.end_byte].decode() if declared else None
                elif receiver.type == "identifier":
                    ident = data[receiver.start_byte:receiver.end_byte].decode()
                    types = hints.get(ident, set())
                    hint = next(iter(types)) if len(types) == 1 else ident if ident[:1].isupper() else None
                if hint:
                    score = 0 if hint.rsplit(".", 1)[-1] == owner.rsplit(".", 1)[-1] else 2
                candidates.append((score, node.start_point.row))
            if candidates:
                line = self.line + min(candidates)[1] - 1
        if line is None and len(self.text) <= 1200:
            return self.line, self.text
        lines = self.text.splitlines()
        index = max(0, min(len(lines) - 1, (line or self.line) - self.line))
        start, end = max(0, index - 3), min(len(lines), index + 5)
        return self.line + start, "\n".join(lines[start:end])


class _ContextBuilder:
    def __init__(self, graph, test_id, method_id, failure_report=None):
        self.graph, self.test_id, self.method_id = graph, test_id, method_id
        self.failure_report = failure_report
        self.sources = {}
        self.source_errors = {}

    def source(self, entity_id):
        if entity_id not in self.sources:
            try:
                entity = self.graph.entities[entity_id]
                content = entity["content"]
                location = content.get("location") or {}
                text = self.graph.source_text(entity_id)
                if not text:
                    raise ValueError("source unavailable")
                self.sources[entity_id] = _Source(
                    entity_id, content.get("method_id") or entity_id,
                    str(location.get("file") or "unknown"), int(location.get("line") or 1), text,
                )
            except (KeyError, ValueError, OSError) as exc:
                self.source_errors[entity_id] = str(exc)
                self.sources[entity_id] = None
        return self.sources[entity_id]

    def find(self, class_name, method_name, line=None, file=None):
        prefix = class_name + "#" + method_name + "("
        db = getattr(self.graph, "connection", None)
        if db is not None and getattr(self.graph, "_schema_version", 1) >= 2:
            ids = [row[0] for row in db.execute(
                "SELECT public_id FROM entities WHERE method_id >= ? AND method_id < ? "
                "AND kind IN ('function', 'test_method') ORDER BY public_id LIMIT 20",
                (prefix, prefix + "\uffff"),
            )]
        else:
            ids = [eid for eid, entity in self.graph.entities.items()
                   if str(entity["content"].get("method_id", "")).startswith(prefix)][:20]
        result = []
        for eid in ids:
            source = self.source(eid)
            if source and (file is None or source.file.rsplit("/", 1)[-1] == file):
                if line is None or source.line <= line < source.line + len(source.text.splitlines()):
                    result.append(source)
        return result

    def incoming(self, entity_id, anchors):
        owner = entity_id.split(":", 1)[-1].split("#", 1)[0] + "#"
        db = getattr(self.graph, "connection", None)
        if db is not None and getattr(self.graph, "_schema_version", 1) >= 2:
            # Read IDs only: unrelated method bodies need not be materialized.
            placeholders = ",".join("?" for _ in anchors)
            order = f"(e.public_id IN ({placeholders})) DESC," if anchors else ""
            rows = db.execute(
                "SELECT DISTINCT e.public_id FROM relations r "
                "JOIN entities t ON t.node_id=r.target_node "
                "JOIN entities e ON e.node_id=r.source_node "
                "WHERE t.public_id=? AND r.kind='calls_candidate' "
                f"ORDER BY {order} (e.method_id >= ? AND e.method_id < ?) DESC, e.public_id LIMIT ?",
                (entity_id, *anchors, owner, owner + "\uffff", MAX_NEIGHBORS + 1),
            ).fetchall()
            ids = [row[0] for row in rows]
        else:
            ids = sorted({self.graph.relations[rid]["source"]
                          for rid in self.graph.incoming[entity_id]
                          if self.graph.relations[rid]["relation_type"] == "calls_candidate"},
                         key=lambda eid: (eid not in anchors, not eid.split(":", 1)[-1].startswith(owner), eid))
        return ids[:MAX_NEIGHBORS]

    def path(self, target, anchors):
        pending, seen = deque([(target, [target])]), {target}
        visited = 0
        while pending and visited < MAX_SEARCH_NODES:
            current, path = pending.popleft()
            visited += 1
            if current in anchors:
                return path
            if len(path) > MAX_DEPTH:
                continue
            for caller in self.incoming(current, anchors):
                if caller not in seen:
                    seen.add(caller)
                    pending.append((caller, [caller, *path]))
        return []

    def build(self):
        report = self.failure_report
        if report is None:
            report = self.graph.entities.get("report:" + self.test_id, {}).get("content", {}).get("text", "")
        report = str(report or "")
        test_class, separator, test_method = self.test_id.partition("::")
        tests = self.find(test_class, test_method) if separator else []
        frames = []
        for match in list(_FRAME.finditer(report))[:64]:
            symbol, file, line = match.groups()
            class_name, _, method_name = symbol.rsplit("/", 1)[-1].rpartition(".")
            for source in self.find(class_name, method_name, int(line), file):
                frames.append((source, int(line)))
            if len(frames) >= 12:
                break
        target = self.graph.aliases.get(self.method_id, self.method_id)
        anchors = list(dict.fromkeys([s.entity_id for s in tests] + [s.entity_id for s, _ in frames]))
        path = self.path(target, anchors) if anchors else []
        assertions = {}
        for source, line in frames:
            for start, end, text in source.assertions():
                if start <= line <= end:
                    assertions.setdefault(source.entity_id, (source, start, text))

        test_blocks = ["## Test Code", f"Test: `{self.test_id}`"]
        frame_lines = {source.entity_id: line for source, line in frames}
        if assertions:
            source, line, _ = next(iter(assertions.values()))
            test_blocks.append(f"Failing assertion: `{_name(source.method_id)}` — {source.file}:{line}")
        elif not any(source.entity_id in frame_lines for source in tests):
            test_blocks.append("Failure location: unavailable.")
        for source in tests:
            assertion = assertions.get(source.entity_id)
            line = assertion[1] if assertion else frame_lines.get(source.entity_id)
            label = "FAILING ASSERTION" if assertion else "FAILURE TRIGGER" if assertions else "FAILURE LOCATION"
            text = _marked(source.text, source.line, line, label)
            test_blocks.append(f"{source.file}:{source.line}\n```java\n{text}\n```")
        if not tests:
            detail = next(iter(self.source_errors.values()), "not found in the source index")
            test_blocks.append(f"Test source unavailable: {detail}")
        # Preserve the whole selected test method even when it exceeds the normal
        # context budget. Bound the failure report and supplementary source only.
        test_section = "\n\n".join(test_blocks)
        failure_section = "## Test Failure Message\n\n```text\n" + _clip(
            report or "Failure message unavailable.", FAILURE_LIMIT) + "\n```"

        chain = []
        if path:
            # Join the reported helper calls to the static suffix when the
            # selected test is present in the stack. No new edges are inferred.
            reported = list(dict.fromkeys(source.entity_id for source, _ in reversed(frames)))
            if path[0] in reported and any(source.entity_id in reported for source in tests):
                first = next(i for i, eid in enumerate(reported) if eid in {s.entity_id for s in tests})
                last = reported.index(path[0])
                if first <= last:
                    chain = reported[first:last]
            chain.extend(path)
        dependencies = ["## Dependency Chain", f"Target method: `{self.method_id}`"]
        if chain:
            dependencies.append(" → ".join(f"`{_name(eid)}`" for eid in chain))
        else:
            dependencies.append("No call candidate chain to the target was found within the search budget; "
                                "the connection remains unresolved.")
        budget = max(800, CONTEXT_LIMIT - len(test_section) - len(failure_section) - 4)

        def snippet(source, start, text):
            label = _name(source.method_id) if source.entity_id.startswith("test_method:") else source.method_id
            header = f"`{label}` — {source.file}:{start}\n```java\n"
            available = budget - len("\n\n".join(dependencies)) - len(header) - 8
            if available < 100:
                return
            dependencies.append(header + _clip(text, min(1200, available)) + "\n```")

        test_ids = {source.entity_id for source in tests}
        shown = set(test_ids)
        # Assertions inside helpers belong with their dependency source, while
        # the complete selected test remains in Test Code.
        for source, line, assertion in list(assertions.values())[:2]:
            if source.entity_id in shown:
                continue
            start, text = source.excerpt(line)
            if assertion not in text:
                start, text = line, assertion
            snippet(source, start, _marked(text, start, line, "FAILING ASSERTION"))
            shown.add(source.entity_id)
        pairs = list(dict.fromkeys([*zip(path, path[1:]), *zip(chain, chain[1:])]))
        for caller, callee in pairs:
            source = self.source(caller)
            # A helper can contain both the failed assertion and the production
            # call, so keep both locations when they differ.
            if source and caller not in test_ids and (caller not in shown or caller in assertions):
                start, text = source.excerpt(callee=callee.split(":", 1)[-1])
                if caller in assertions and start <= assertions[caller][1] < start + len(text.splitlines()):
                    continue
                snippet(source, start, text)
                shown.add(caller)
        return test_section + "\n\n" + failure_section + "\n\n" + "\n\n".join(dependencies)


def build_repair_context(context, test_id: str, method_id: str, *, failure_report: str | None = None) -> str:
    """Read assertions, stack frames and one bounded static call path as Markdown."""
    graph = getattr(context, "graph", None) or getattr(getattr(context, "method_records", None), "graph", None)
    if graph is None:
        return (f"## Test Code\n\nTest: `{test_id}`\n\nSource index unavailable.\n\n"
                "## Test Failure Message\n\n```text\n" +
                _clip(failure_report or "Failure message unavailable.", FAILURE_LIMIT) +
                f"\n```\n\n## Dependency Chain\n\nTarget method: `{method_id}`\n\nSource index unavailable.")
    return _ContextBuilder(graph, str(test_id), method_id.removeprefix("func:"), failure_report).build()
