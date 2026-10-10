from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from tree_sitter import Node

from src.fault_graph.java_call_resolver import JavaCallResolver, source_symbols
from src.fault_graph.method_id_map import build_source_to_spectra_map
from src.utils.java_source import (
    CLASS_LIKE_TYPES,
    METHOD_TYPES,
    JavaMethodDescriptor,
    _java_parser,
    _node_text,
    parse_java_methods,
)


@dataclass(frozen=True)
class _CallSite:
    callee_name: str
    arity: int
    receiver_type_hint: str | None
    is_constructor: bool
    node: Node
    source_bytes: bytes


def build_static_call_graph(
    source_roots: list[Path],
    spectra_ids: list[str],
    *,
    methods: list[JavaMethodDescriptor] | None = None,
) -> dict[str, list[str]]:
    """Build method-level adjacency keyed by spectra method ids."""
    resolved_methods = methods if methods is not None else _collect_methods(source_roots)
    source_to_spectra = build_source_to_spectra_map(resolved_methods, spectra_ids)
    if not source_to_spectra:
        return {}

    _parsed_call_file.cache_clear()
    files = sorted({p for root in source_roots for p in root.rglob("*.java")})
    index = JavaCallResolver(resolved_methods, source_symbols(resolved_methods, files))
    adjacency: dict[str, set[str]] = {spectra_id: set() for spectra_id in source_to_spectra.values()}

    for method in resolved_methods:
        caller_spectra = source_to_spectra.get(method.method_id)
        if caller_spectra is None:
            continue
        call_sites = _extract_call_sites(method)
        for site in call_sites:
            for callee in index.resolve(site, enclosing=method):
                callee_spectra = source_to_spectra.get(callee.method_id)
                if callee_spectra is None:
                    continue
                adjacency.setdefault(caller_spectra, set()).add(callee_spectra)

    return {
        caller: sorted(callees)
        for caller, callees in sorted(adjacency.items())
    }


def prune_call_graph(
    adjacency: dict[str, list[str]],
    covered_method_ids: set[str] | list[str] | tuple[str, ...],
) -> dict[str, list[str]]:
    covered = set(covered_method_ids)
    pruned: dict[str, list[str]] = {}
    for caller, callees in adjacency.items():
        if caller not in covered:
            continue
        pruned[caller] = [callee for callee in callees if callee in covered]
    for method_id in covered:
        pruned.setdefault(method_id, [])
    return pruned


def _collect_methods(source_roots: list[Path]) -> list[JavaMethodDescriptor]:
    methods: list[JavaMethodDescriptor] = []
    seen: set[tuple[Path, int]] = set()
    for root in source_roots:
        if not root.is_dir():
            continue
        for java_file in root.rglob("*.java"):
            for method in parse_java_methods(java_file):
                key = (method.file_path.resolve(), method.start_byte)
                if key in seen:
                    continue
                seen.add(key)
                methods.append(method)
    return methods


@lru_cache(maxsize=1)
def _parsed_call_file(file_path: Path) -> tuple[bytes, dict[tuple[int, int], Node]]:
    """Parse one source file and index method bodies for call extraction.

    Methods from a source file are visited consecutively during graph building.
    Reusing one parse tree avoids rereading and reparsing that entire file for
    every method it contains.
    """
    try:
        source_bytes = file_path.read_bytes()
    except OSError:
        return b"", {}
    tree = _java_parser().parse(source_bytes)
    method_bodies: dict[tuple[int, int], Node] = {}
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type in METHOD_TYPES:
            body = node.child_by_field_name("body")
            if body is not None:
                method_bodies[(body.start_byte, body.end_byte)] = body
        stack.extend(reversed(node.named_children))
    return source_bytes, method_bodies


def _extract_call_sites(method: JavaMethodDescriptor, *, source_bytes=None, body=None) -> list[_CallSite]:
    if source_bytes is None:
        source_bytes, method_bodies = _parsed_call_file(method.file_path)
        body = method_bodies.get((method.body_start_byte, method.body_end_byte))
    if body is None:
        return []
    sites = []
    pending = [body]
    while pending:
        node = pending.pop()
        if node is not body and (node.type in METHOD_TYPES | CLASS_LIKE_TYPES or node.type == "class_body"):
            continue
        args = node.child_by_field_name("arguments")
        if node.type == "method_invocation":
            sites.append(_CallSite(_node_text(node.child_by_field_name("name"), source_bytes),
                                   _argument_arity(args), None, False, node, source_bytes))
        elif node.type in {"object_creation_expression", "explicit_constructor_invocation"}:
            type_node = node.child_by_field_name("type") or node.child_by_field_name("constructor")
            type_name = _node_text(type_node, source_bytes)
            sites.append(_CallSite(type_name, _argument_arity(args), type_name, True, node, source_bytes))
        pending.extend(reversed(node.named_children))
    return sites


def _argument_arity(args_node: Node | None) -> int:
    if args_node is None:
        return 0
    return sum(1 for child in args_node.named_children)
