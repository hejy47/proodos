"""Build the case fault-context graph from source and reproducer artifacts."""

from __future__ import annotations

from collections import defaultdict
import json
import hashlib
from pathlib import Path
import re

from src.fault_graph.evidence_graph import EvidenceGraph
from src.fault_graph.syz_resources import add_syz_resources
from src.project.kernel_tree import is_c_repro
from src.utils.c_source import _c_parser, _collect_functions, _declarator_name


# libc / syscall names that a C reproducer typically issues. Helpers such as
# printf/memset are omitted so association sees the guest-facing call sequence.
_C_REPRO_SYSCALLS = frozenset({
    "accept", "bind", "bpf", "chmod", "clone", "clone3", "close", "connect",
    "dup", "dup2", "dup3", "epoll_create", "epoll_create1", "epoll_ctl",
    "epoll_wait", "eventfd", "eventfd2", "fcntl", "fchmod", "fork", "fstat",
    "getsockopt", "ioctl", "listen", "lseek", "lstat", "mkdir", "mkdirat",
    "mmap", "mount", "munmap", "open", "openat", "perf_event_open", "pipe",
    "pipe2", "poll", "ppoll", "prctl", "pread", "pread64", "pselect",
    "pwrite", "pwrite64", "read", "recv", "recvfrom", "recvmsg", "select",
    "send", "sendmsg", "sendto", "setsockopt", "signalfd", "socket", "stat",
    "syscall", "syz_open_dev", "syz_open_procfs", "timerfd_create", "umount",
    "umount2", "unlink", "unlinkat", "write",
})


def index_c_source(graph: EvidenceGraph, source_root: Path, *, retain_source: bool = True,
                   rel_paths: list[str] | None = None,
                   include_coverage: bool = True) -> None:
    source_root = source_root.resolve()
    if not source_root.is_dir():
        raise ValueError(f"Source root is not a directory: {source_root}")
    by_name, by_file = defaultdict(list), defaultdict(list)
    pending_calls = []
    parser = _c_parser()
    count = 0
    paths = (source_root.rglob("*") if rel_paths is None
             else (source_root / rel_path for rel_path in sorted(set(rel_paths))))
    for path in paths:
        if path.suffix not in {".c", ".h"} or not path.is_file():
            continue
        if not path.resolve().is_relative_to(source_root):
            continue
        rel = path.relative_to(source_root).as_posix()
        data = path.read_bytes()
        graph.metadata.setdefault("source_sha256", {})[rel] = hashlib.sha256(data).hexdigest()
        tree = parser.parse(data)
        functions = []
        _collect_functions(tree.root_node, data, path, rel, functions)
        by_start = {function.start_byte: function for function in functions}
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            location = dict(file=rel, line=node.start_point[0] + 1,
                            end_line=node.end_point[0] + 1)
            provenance = dict(extractor="tree_sitter_c", source=rel, location=location)
            source = data[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
            if node.type == "function_definition":
                descriptor = by_start.get(node.start_byte)
                if descriptor is None:
                    continue
                mid = descriptor.method_id
                # Conditional definitions can share file/name. Preserve both locations.
                if mid in graph.aliases:
                    mid = f"{mid}@{descriptor.start_line}"
                eid = "func:" + mid
                attributes = {"coverage": {"status": "unavailable"}} if include_coverage else {}
                graph.add_entity(eid, "function", name=descriptor.function_name,
                                 content=dict(method_id=mid, source_code=source if retain_source else None, location=location,
                                              source_span=dict(file=rel, start_byte=node.start_byte,
                                                               end_byte=node.end_byte)),
                                 provenance=provenance, **attributes)
                by_name[descriptor.function_name].append((eid, descriptor.is_static))
                by_file[(rel, descriptor.function_name)].append(eid)
                body = node.child_by_field_name("body")
                nodes = [body] if body else []
                while nodes:
                    child = nodes.pop()
                    if child.type == "call_expression":
                        target = child.child_by_field_name("function")
                        if target and target.type == "identifier":
                            name = data[target.start_byte:target.end_byte].decode()
                            pending_calls.append((eid, rel, name, child.start_point[0] + 1))
                    nodes.extend(child.named_children)
                continue
            kind = None
            name = ""
            if node.type in {"preproc_def", "preproc_function_def"}:
                kind = "macro"
                name_node = node.child_by_field_name("name")
                name = data[name_node.start_byte:name_node.end_byte].decode() if name_node else ""
            elif node.type in {"struct_specifier", "union_specifier", "enum_specifier"}:
                if node.child_by_field_name("body"):
                    kind = "type_definition"
                    name_node = node.child_by_field_name("name")
                    name = data[name_node.start_byte:name_node.end_byte].decode() if name_node else "anonymous"
            elif node.type in {"declaration", "type_definition"}:
                kind = "global_declaration" if node.type == "declaration" else "type_definition"
                decl = node.child_by_field_name("declarator")
                name = _declarator_name(decl, data) if decl else ""
            if kind and name:
                eid = f"source:{rel}:{node.start_byte}:{kind}"
                graph.add_entity(eid, kind, name=name,
                                 content=dict(source_code=source, location=location,
                                              source_span=dict(file=rel, start_byte=node.start_byte,
                                                               end_byte=node.end_byte)), provenance=provenance)
            stack.extend(reversed(node.named_children))
        count += 1
        if count % 10000 == 0:
            print(f"Indexed {count} source files", flush=True)
    for caller, rel, name, line in pending_calls:
        matches = by_file.get((rel, name)) or [eid for eid, static in by_name[name] if not static]
        for callee in matches:
            graph.add_relation(caller, "static_call_candidate", callee,
                               dict(extractor="tree_sitter_c", source=rel, line=line,
                                    callee_name=name, candidate_count=len(matches)))
    graph.metadata.update(source_root=str(source_root), source_scope=(
        "all_c_and_h_under_source_root" if rel_paths is None else "case_seeded_source_files"
    ), indexed_files=count)


def add_c_repro_calls(graph: EvidenceGraph, text: str, *, source: str) -> None:
    """Index libc/syscall call sites from a C reproducer as ``call:N`` entities."""
    data = text.encode("utf-8", errors="replace")
    tree = _c_parser().parse(data)
    found: list[tuple[int, str, str, str]] = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "call_expression":
            function_node = node.child_by_field_name("function")
            name = ""
            if function_node is not None and function_node.type == "identifier":
                name = data[function_node.start_byte:function_node.end_byte].decode()
            if name in _C_REPRO_SYSCALLS:
                arguments_node = node.child_by_field_name("arguments")
                arguments = (
                    data[arguments_node.start_byte + 1:arguments_node.end_byte - 1]
                    .decode("utf-8", errors="replace")
                    if arguments_node is not None and arguments_node.end_byte > arguments_node.start_byte + 1
                    else ""
                )
                command = _c_repro_ioctl_command(arguments_node, data) if name == "ioctl" else ""
                display = f"{name}${command}" if command else name
                source_text = data[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
                found.append((node.start_byte, display, source_text, arguments))
        stack.extend(reversed(node.named_children))
    found.sort(key=lambda item: item[0])
    for index, (_, name, source_text, arguments) in enumerate(found):
        provenance = dict(
            extractor="c_repro_call_scanner_v1",
            source=source,
            call_index=index,
            resource_types="not_inferred",
        )
        graph.add_entity(
            f"call:{index}",
            "syscall",
            name=name,
            content=dict(
                call_index=index,
                name=name,
                source_text=source_text,
                arguments=arguments,
                definitions=[],
                uses=[],
            ),
            provenance=provenance,
        )


def _c_repro_ioctl_command(arguments_node, data: bytes) -> str:
    if arguments_node is None:
        return ""
    named = [child for child in arguments_node.named_children]
    if len(named) < 2 or named[1].type != "identifier":
        return ""
    command = named[1]
    return data[command.start_byte:command.end_byte].decode()


def add_coverage(graph: EvidenceGraph, payload: dict, *, source: str) -> None:
    if str(payload.get("case_id", graph.case_id)) != graph.case_id:
        raise ValueError("Coverage case_id does not match the fault context")
    coverage_extractor = str(payload.get("source", "runtime_coverage"))
    graph.metadata["coverage"] = {key: value for key, value in payload.items()
                                   if key not in {"functions", "function_pc_counts", "per_call"}}
    graph.metadata["coverage"]["source_path"] = source
    by_name = defaultdict(list)
    for eid, entity in graph.entities.items():
        if entity["entity_type"] == "function":
            by_name[entity["name"]].append(eid)
            entity["coverage"] = dict(status="not_observed" if payload.get("coverage_complete") is True
                                      else "unavailable", syscall_indices=[], unassigned=False,
                                      provenance={"extractor": coverage_extractor, "source": source})

    def resolve(symbol: str) -> str | None:
        if symbol in graph.aliases:
            return graph.aliases[symbol]
        # Kernel compiler clones retain a traceable base symbol name.
        name = re.sub(r"\.(?:isra|constprop|part)\.\d+$", "", symbol)
        candidates = by_name.get(name, [])
        if len(candidates) == 1:
            return candidates[0]
        graph.metadata.setdefault("coverage_unresolved_symbols", {})[symbol] = candidates
        return None

    for symbol in payload.get("functions", []):
        eid = resolve(symbol)
        if eid:
            graph.entities[eid]["coverage"]["status"] = "observed"
    for entry in payload.get("per_call", []):
        call = str(entry["call"])
        cid = f"call:{call}"
        for symbol in entry.get("functions", []):
            eid = resolve(symbol)
            if not eid:
                continue
            coverage = graph.entities[eid]["coverage"]
            coverage["status"] = "observed"
            if call == "extra":
                coverage["unassigned"] = True
            elif cid in graph.entities:
                index = int(call)
                if index not in coverage["syscall_indices"]:
                    coverage["syscall_indices"].append(index)
                graph.add_relation(cid, "covers", eid,
                                   dict(extractor=coverage_extractor, source=source, call=call, symbol=symbol,
                                        symbolization=payload.get("symbolization")))
            else:
                graph.metadata.setdefault("diagnostics", []).append(
                    dict(kind="coverage_call_without_program_call", call=call, symbol=symbol))

    # ftrace has no KCOV syscall ownership, but its caller -> callee events are
    # still useful navigation evidence.  Add only edges whose endpoints resolve
    # to indexed functions; unresolved symbols remain in diagnostics.
    for edge in payload.get("function_call_edges", []):
        caller = resolve(str(edge.get("caller", "")))
        callee = resolve(str(edge.get("callee", "")))
        if caller and callee:
            graph.add_relation(
                caller,
                "ftrace_call_observed",
                callee,
                dict(extractor=coverage_extractor, source=source,
                     count=int(edge.get("count", 1)),
                     symbolization=payload.get("symbolization")),
            )


def build_kernel_evidence(*, case_id: str, source_root: Path, syz_path: Path,
                          report_path: Path, coverage_path: Path | None = None,
                          lifecycle_path: Path | None = None, kernel_commit: str | None = None,
                          retain_source: bool = True, rel_paths: list[str] | None = None,
                          include_coverage: bool | None = None) -> EvidenceGraph:
    graph = EvidenceGraph(case_id, metadata=dict(kernel_commit=kernel_commit))
    if include_coverage is None:
        include_coverage = coverage_path is not None
    index_c_source(graph, source_root, retain_source=retain_source, rel_paths=rel_paths,
                   include_coverage=include_coverage)
    graph.add_entity("report", "crash_report", name="crash report",
                     content=dict(text=report_path.read_text(encoding="utf-8", errors="replace")),
                     provenance=dict(source=str(report_path)))
    syz = syz_path.read_text(encoding="utf-8", errors="replace")
    repro_kind = "gcc" if is_c_repro(syz_path) else "syz"
    if repro_kind == "gcc":
        add_c_repro_calls(graph, syz, source=str(syz_path))
    else:
        add_syz_resources(graph, syz, source=str(syz_path))
    graph.metadata.update(syz_path=str(syz_path), report_path=str(report_path),
                          repro_kind=repro_kind)
    graph.add_entity("program", "reproducer", name="executed reproducer",
                     content=dict(text=syz), provenance=dict(source=str(syz_path)))
    if coverage_path:
        coverage = json.loads(coverage_path.read_text())
        manifest = coverage.get("execution_manifest", {})
        recorded_commit = manifest.get("kernel_commit")
        if recorded_commit and kernel_commit and recorded_commit != kernel_commit:
            raise ValueError("Coverage kernel commit does not match indexed source")
        recorded_hash = manifest.get("executed_syz_sha256")
        if recorded_hash and recorded_hash != hashlib.sha256(syz.encode()).hexdigest():
            raise ValueError("Coverage reproducer hash does not match supplied syz_path")
        if not manifest:
            graph.metadata.setdefault("diagnostics", []).append(
                {"kind": "legacy_coverage_without_execution_manifest"})
        add_coverage(graph, coverage, source=str(coverage_path))
    # A static kernel graph has no runtime coverage claims.  Coverage metadata
    # is added only when an explicit coverage artifact is imported.
    if lifecycle_path:
        from src.fault_graph.lifecycle import add_lifecycle_matches
        add_lifecycle_matches(graph, lifecycle_path)
    return graph
