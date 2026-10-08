"""Extract explicit value references without interpreting syscall semantics.

This scanner understands quoting and balanced arguments, including nested
``<rN=>`` definitions. It does not infer resource types or implicit dependencies.
Unsupported syntax is reported instead of renumbering the remaining calls.
"""

from __future__ import annotations

import re

from src.fault_graph.evidence_graph import EvidenceGraph


def _mask_literals(text: str) -> str:
    out = list(text)
    quote = None
    escaped = False
    comment = False
    for i, char in enumerate(text):
        if comment:
            if char == "\n":
                comment = False
            else:
                out[i] = " "
        elif quote:
            if char != "\n":
                out[i] = " "
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in "\"'":
            quote = char
            out[i] = " "
        elif char == "#":
            comment = True
            out[i] = " "
    if quote:
        raise ValueError("Unterminated syzkaller string literal")
    return "".join(out)


def parse_explicit_resources(text: str) -> list[dict]:
    masked = _mask_literals(text)
    calls = []
    start = 0
    depth = 0
    for i, char in enumerate(masked + "\n"):
        if char in "({[":
            depth += 1
        elif char in ")}]":
            depth -= 1
            if depth < 0:
                raise ValueError("Unbalanced syzkaller arguments")
        if char != "\n" or depth:
            continue
        segment = masked[start:i]
        source = text[start:i].strip()
        start = i + 1
        if not segment.strip():
            continue
        match = re.match(r"\s*(?:(r\d+)\s*=\s*)?([A-Za-z_][\w$]*)\s*\(", segment)
        if not match:
            raise ValueError(f"Unsupported syzkaller syntax at call {len(calls)}")
        definitions = []
        if match.group(1):
            definitions.append(dict(reference=match.group(1), position="return"))
        body_start = match.end()
        # Locate the closing call parenthesis, excluding execution properties.
        nesting, end = 1, body_start
        while end < len(segment) and nesting:
            if segment[end] == "(":
                nesting += 1
            elif segment[end] == ")":
                nesting -= 1
            end += 1
        if nesting:
            raise ValueError("Unterminated syzkaller call")
        body = segment[body_start:end - 1]
        definitions_spans = []
        for definition in re.finditer(r"<\s*(r\d+)\s*=>", body):
            definitions.append(dict(reference=definition.group(1),
                                    position=f"arguments@{definition.start()}"))
            definitions_spans.append(definition.span())
        uses = []
        for reference in re.finditer(r"\br\d+\b", body):
            if any(a <= reference.start() < b for a, b in definitions_spans):
                continue
            uses.append(dict(reference=reference.group(), position=f"arguments@{reference.start()}"))
        calls.append(dict(call_index=len(calls), name=match.group(2), source_text=source,
                          arguments=text[i - len(segment) + body_start:i - len(segment) + end - 1],
                          definitions=definitions, uses=uses))
    if depth:
        raise ValueError("Unbalanced syzkaller arguments")
    return calls


def add_syz_resources(graph: EvidenceGraph, text: str, *, source: str) -> None:
    calls = parse_explicit_resources(text)
    defined = {}
    for call in calls:
        # The graph is scoped to one debugging case; keep node IDs local to
        # that graph and store the case ID in graph metadata.
        cid = f"call:{call['call_index']}"
        provenance = dict(extractor="syz_explicit_reference_scanner_v1", source=source,
                          call_index=call["call_index"], resource_types="not_inferred")
        graph.add_entity(cid, "syscall", name=call["name"], content=call, provenance=provenance)
        # References are resolved only to preceding definitions, never by name alone.
        for use in call["uses"]:
            rid = defined.get(use["reference"])
            if rid is None:
                graph.metadata.setdefault("diagnostics", []).append(
                    dict(kind="unresolved_resource_reference", call=cid, **use))
                continue
            graph.add_relation(cid, "uses_resource", rid, dict(provenance, **use))
        for definition in call["definitions"]:
            reference = definition["reference"]
            if reference in defined:
                raise ValueError(f"Duplicate syzkaller resource definition: {reference}")
            rid = f"resource:{reference}"
            graph.add_entity(rid, "explicit_resource", name=reference,
                             content=dict(reference=reference, resource_kind=None),
                             provenance=provenance)
            defined[reference] = rid
            graph.add_relation(cid, "defines_resource", rid, dict(provenance, **definition))
