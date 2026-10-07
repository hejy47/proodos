from __future__ import annotations

from pathlib import Path

from src.fault_graph.static_call_graph import prune_call_graph
from src.utils.c_source import CFunctionDescriptor, extract_c_call_names, parse_c_functions

__all__ = ["build_c_call_graph", "collect_c_functions", "prune_call_graph"]


def collect_c_functions(source_roots: list[Path]) -> list[CFunctionDescriptor]:
    functions: list[CFunctionDescriptor] = []
    seen: set[tuple[Path, int]] = set()
    for root in source_roots:
        if root.is_file() and root.suffix == ".c":
            files = [root]
            parse_root = root.parent
        elif root.is_dir():
            files = sorted(root.rglob("*.c"))
            parse_root = root
        else:
            continue
        for c_file in files:
            for function in parse_c_functions(c_file, source_root=parse_root):
                key = (function.file_path.resolve(), function.start_byte)
                if key in seen:
                    continue
                seen.add(key)
                functions.append(function)
    return functions


def build_c_call_graph(
    source_roots: list[Path],
    spectra_ids: list[str] | None = None,
    *,
    functions: list[CFunctionDescriptor] | None = None,
) -> dict[str, list[str]]:
    resolved = functions if functions is not None else collect_c_functions(source_roots)
    if not resolved:
        return {}
    allowed = set(spectra_ids) if spectra_ids else None
    by_name: dict[str, list[CFunctionDescriptor]] = {}
    by_file_name: dict[tuple[str, str], list[CFunctionDescriptor]] = {}
    for function in resolved:
        by_name.setdefault(function.function_name, []).append(function)
        by_file_name.setdefault((function.rel_path, function.function_name), []).append(function)

    adjacency: dict[str, set[str]] = {}
    for function in resolved:
        caller_id = function.method_id
        if allowed is not None and caller_id not in allowed:
            continue
        adjacency.setdefault(caller_id, set())
        for callee_name in extract_c_call_names(function):
            callees = _resolve_callees(callee_name, function, by_name, by_file_name)
            for callee in callees:
                callee_id = callee.method_id
                if allowed is not None and callee_id not in allowed:
                    continue
                adjacency[caller_id].add(callee_id)

    if allowed is not None:
        for method_id in allowed:
            adjacency.setdefault(method_id, set())

    return {caller: sorted(callees) for caller, callees in sorted(adjacency.items())}


def _resolve_callees(
    callee_name: str,
    caller: CFunctionDescriptor,
    by_name: dict[str, list[CFunctionDescriptor]],
    by_file_name: dict[tuple[str, str], list[CFunctionDescriptor]],
) -> list[CFunctionDescriptor]:
    same_file = by_file_name.get((caller.rel_path, callee_name), [])
    if same_file:
        return same_file
    matches = by_name.get(callee_name, [])
    if not matches:
        return []
    non_static = [item for item in matches if not item.is_static]
    if len(non_static) == 1:
        return non_static
    if len(matches) == 1:
        return matches
    return non_static or matches
