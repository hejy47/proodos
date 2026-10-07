from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import re

from tree_sitter import Node

from src.fault_graph.method_id_map import build_source_to_spectra_map, fingerprint_source_method
from src.utils.java_source import (
    CLASS_LIKE_TYPES,
    METHOD_TYPES,
    JavaMethodDescriptor,
    _java_parser,
    _node_text,
    parse_java_methods,
)


_IMPORT_RE = re.compile(r"(?m)^\s*import\s+(?:static\s+)?([\w.]+(?:\.\*)?)\s*;")


@dataclass(frozen=True)
class _CallSite:
    callee_name: str
    arity: int
    receiver_type_hint: str | None  # simple or FQN type name when known
    is_constructor: bool = False


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

    index = _MethodIndex(resolved_methods)
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


class _MethodIndex:
    def __init__(self, methods: list[JavaMethodDescriptor]) -> None:
        self._by_class_name_arity: dict[tuple[str, str, int], list[JavaMethodDescriptor]] = {}
        self._by_name_arity: dict[tuple[str, int], list[JavaMethodDescriptor]] = {}
        self._fqns_by_simple: dict[str, list[str]] = {}
        for method in methods:
            jvm_name = fingerprint_source_method(method)[1]
            class_key = (method.qualified_class_name, jvm_name, method.parameter_count)
            self._by_class_name_arity.setdefault(class_key, []).append(method)
            self._by_name_arity.setdefault((jvm_name, method.parameter_count), []).append(method)
            simple = method.class_name.rsplit("$", 1)[-1]
            fqns = self._fqns_by_simple.setdefault(simple, [])
            if method.qualified_class_name not in fqns:
                fqns.append(method.qualified_class_name)

    def resolve(
        self,
        site: _CallSite,
        *,
        enclosing: JavaMethodDescriptor,
    ) -> list[JavaMethodDescriptor]:
        name = "<init>" if site.is_constructor else site.callee_name
        arity = site.arity
        class_candidates = self._resolve_receiver_classes(site, enclosing=enclosing)
        matches: list[JavaMethodDescriptor] = []
        seen: set[tuple[Path, int]] = set()
        if class_candidates:
            for class_name in class_candidates:
                for method in self._by_class_name_arity.get((class_name, name, arity), ()):
                    key = (method.file_path.resolve(), method.start_byte)
                    if key in seen:
                        continue
                    seen.add(key)
                    matches.append(method)
            if matches:
                return matches
        # Fall back to name+arity across the project index (coverage prunes later).
        for method in self._by_name_arity.get((name, arity), ()):
            key = (method.file_path.resolve(), method.start_byte)
            if key in seen:
                continue
            seen.add(key)
            matches.append(method)
        return matches

    def _resolve_receiver_classes(
        self,
        site: _CallSite,
        *,
        enclosing: JavaMethodDescriptor,
    ) -> list[str]:
        hint = site.receiver_type_hint
        if hint == "__unknown__":
            return []
        if hint is None:
            # Unqualified / this → enclosing class.
            return [enclosing.qualified_class_name]
        if hint in {"this", "super"}:
            return [enclosing.qualified_class_name]
        if "." in hint:
            return [hint]
        # Simple name: imports / same package / nested simple match.
        imports = _file_imports(enclosing.file_path)
        if hint in imports:
            return [imports[hint]]
        same_package = []
        if enclosing.package_name:
            same_package.append(f"{enclosing.package_name}.{hint}")
        else:
            same_package.append(hint)
        known = [fqn for fqn in same_package if fqn in self._fqns_by_simple.get(hint, same_package)]
        # Prefer exact known FQNs from index.
        indexed = self._fqns_by_simple.get(hint, [])
        ordered: list[str] = []
        for fqn in [*known, *indexed, *same_package]:
            if fqn not in ordered:
                ordered.append(fqn)
        return ordered


@lru_cache(maxsize=8192)
def _file_imports(file_path: Path) -> dict[str, str]:
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    imports: dict[str, str] = {}
    for match in _IMPORT_RE.finditer(text):
        imported = match.group(1).strip()
        if imported.endswith(".*"):
            continue
        simple = imported.rsplit(".", 1)[-1]
        if simple and simple[0].isupper():
            imports[simple] = imported
    return imports


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


def _extract_call_sites(method: JavaMethodDescriptor) -> list[_CallSite]:
    try:
        file_path = method.file_path.resolve()
        source_bytes, method_bodies = _parsed_call_file(file_path)
    except (OSError, RuntimeError):
        return []
    body = method_bodies.get((method.body_start_byte, method.body_end_byte))
    if body is None:
        return []
    param_types = _parameter_simple_types(method.signature)
    sites: list[_CallSite] = []
    _walk_for_calls(body, source_bytes, sites, param_types, skip_nested_types=True)
    return sites


def _walk_for_calls(
    node: Node,
    source_bytes: bytes,
    sites: list[_CallSite],
    param_types: dict[str, str],
    *,
    skip_nested_types: bool,
) -> None:
    if skip_nested_types:
        if node.type in METHOD_TYPES:
            return
        if node.type in CLASS_LIKE_TYPES:
            return
        if node.type == "class_body":
            return

    if node.type == "method_invocation":
        sites.append(_call_site_from_invocation(node, source_bytes, param_types))
    elif node.type == "object_creation_expression":
        sites.append(_call_site_from_creation(node, source_bytes))

    for child in node.named_children:
        # Still walk into object_creation arguments, but not anonymous class bodies.
        if node.type == "object_creation_expression" and child.type == "class_body":
            continue
        _walk_for_calls(child, source_bytes, sites, param_types, skip_nested_types=skip_nested_types)


def _call_site_from_invocation(
    node: Node,
    source_bytes: bytes,
    param_types: dict[str, str],
) -> _CallSite:
    name_node = node.child_by_field_name("name")
    args_node = node.child_by_field_name("arguments")
    object_node = node.child_by_field_name("object")
    callee_name = _node_text(name_node, source_bytes) if name_node is not None else ""
    arity = _argument_arity(args_node)
    receiver_hint: str | None = None
    if object_node is None:
        receiver_hint = None  # enclosing class
    elif object_node.type == "this":
        receiver_hint = "this"
    elif object_node.type == "super":
        receiver_hint = "super"
    elif object_node.type == "identifier":
        ident = _node_text(object_node, source_bytes)
        if ident[:1].isupper():
            receiver_hint = ident  # TypeName.staticMethod
        else:
            receiver_hint = param_types.get(ident)
    elif object_node.type == "field_access":
        # Leave unresolved → name/arity fallback.
        receiver_hint = ""
    else:
        receiver_hint = ""
    # Empty string means "unknown receiver" (force name/arity fallback).
    if receiver_hint == "":
        return _CallSite(callee_name=callee_name, arity=arity, receiver_type_hint="__unknown__")
    return _CallSite(callee_name=callee_name, arity=arity, receiver_type_hint=receiver_hint)


def _call_site_from_creation(node: Node, source_bytes: bytes) -> _CallSite:
    type_node = node.child_by_field_name("type")
    args_node = node.child_by_field_name("arguments")
    type_name = _node_text(type_node, source_bytes) if type_node is not None else ""
    # Strip generics: Foo<Bar> → Foo
    if "<" in type_name:
        type_name = type_name[: type_name.index("<")].strip()
    simple = type_name.split(".")[-1]
    return _CallSite(
        callee_name=simple,
        arity=_argument_arity(args_node),
        receiver_type_hint=type_name or simple,
        is_constructor=True,
    )


def _argument_arity(args_node: Node | None) -> int:
    if args_node is None:
        return 0
    return sum(1 for child in args_node.named_children)


def _parameter_simple_types(signature: str) -> dict[str, str]:
    text = signature.strip()
    if not text.startswith("(") or ")" not in text:
        return {}
    inner = text[1 : text.index(")")].strip()
    if not inner:
        return {}
    mapping: dict[str, str] = {}
    for part in _split_params(inner):
        cleaned = re.sub(r"@\w+(?:\([^)]*\))?\s*", "", part.strip())
        for modifier in ("final ", "volatile "):
            if cleaned.startswith(modifier):
                cleaned = cleaned[len(modifier) :]
        cleaned = cleaned.replace("...", "").strip()
        tokens = cleaned.rsplit(None, 1)
        if len(tokens) != 2:
            continue
        type_name, var_name = tokens
        if "<" in type_name:
            type_name = type_name[: type_name.index("<")].strip()
        mapping[var_name] = type_name.split(".")[-1]
    return mapping


def _split_params(inner: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for char in inner:
        if char in "(<[":
            depth += 1
        elif char in ")>]":
            depth = max(depth - 1, 0)
        elif char == "," and depth == 0:
            part = "".join(current).strip()
            if part:
                parts.append(part)
            current = []
            continue
        current.append(char)
    part = "".join(current).strip()
    if part:
        parts.append(part)
    return parts
