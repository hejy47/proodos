from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Language, Node, Parser
import tree_sitter_c


C_LANGUAGE = Language(tree_sitter_c.language())


@dataclass(frozen=True)
class CFunctionDescriptor:
    file_path: Path
    rel_path: str
    function_name: str
    parameter_count: int
    is_static: bool
    start_byte: int
    end_byte: int
    start_line: int
    end_line: int
    body_start_byte: int
    body_end_byte: int

    @property
    def method_id(self) -> str:
        return f"{self.rel_path}#{self.function_name}"


def _c_parser() -> Parser:
    return Parser(C_LANGUAGE)


def parse_c_functions(file_path: Path, *, source_root: Path | None = None) -> list[CFunctionDescriptor]:
    source_bytes = file_path.read_bytes()
    tree = _c_parser().parse(source_bytes)
    root = source_root if source_root is not None else file_path.parent
    try:
        rel_path = file_path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        rel_path = file_path.name
    functions: list[CFunctionDescriptor] = []
    _collect_functions(tree.root_node, source_bytes, file_path, rel_path, functions)
    return functions


def extract_c_call_names(function: CFunctionDescriptor) -> list[str]:
    try:
        source_bytes = function.file_path.read_bytes()
    except OSError:
        return []
    tree = _c_parser().parse(source_bytes)
    body = _find_body_node(tree.root_node, function)
    if body is None:
        return []
    names: list[str] = []
    seen: set[str] = set()
    _walk_calls(body, source_bytes, names, seen)
    return names


def extract_c_call_names_from_source(source_bytes: bytes) -> list[str]:
    tree = _c_parser().parse(source_bytes)
    names: list[str] = []
    seen: set[str] = set()
    _walk_calls(tree.root_node, source_bytes, names, seen)
    return names


def _collect_functions(
    node: Node,
    source_bytes: bytes,
    file_path: Path,
    rel_path: str,
    functions: list[CFunctionDescriptor],
) -> None:
    # Generated kernel headers can exceed Python's recursion depth.
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == "function_definition":
            descriptor = _function_from_node(current, source_bytes, file_path, rel_path)
            if descriptor is not None:
                functions.append(descriptor)
                continue
        stack.extend(reversed(current.named_children))


def _function_from_node(
    node: Node,
    source_bytes: bytes,
    file_path: Path,
    rel_path: str,
) -> CFunctionDescriptor | None:
    body = node.child_by_field_name("body")
    declarator = node.child_by_field_name("declarator")
    if body is None or declarator is None:
        return None
    name = _declarator_name(declarator, source_bytes)
    if not name:
        return None
    params = _function_declarator(declarator)
    arity = _parameter_arity(params, source_bytes) if params is not None else 0
    storage = _node_text(node.child_by_field_name("storage_class_specifier"), source_bytes)
    is_static = "static" in _leading_type_text(node, source_bytes)
    if storage:
        is_static = is_static or "static" in storage
    return CFunctionDescriptor(
        file_path=file_path,
        rel_path=rel_path,
        function_name=name,
        parameter_count=arity,
        is_static=is_static,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        body_start_byte=body.start_byte,
        body_end_byte=body.end_byte,
    )


def _leading_type_text(node: Node, source_bytes: bytes) -> str:
    parts: list[str] = []
    for child in node.children:
        if child.type in {"function_declarator", "pointer_declarator", "parenthesized_declarator", "compound_statement"}:
            break
        if child.is_named:
            parts.append(_node_text(child, source_bytes))
    return " ".join(parts)


def _function_declarator(node: Node) -> Node | None:
    current: Node | None = node
    while current is not None:
        if current.type == "function_declarator":
            return current
        nested = current.child_by_field_name("declarator")
        if nested is None:
            break
        current = nested
    return None


def _declarator_name(node: Node, source_bytes: bytes) -> str:
    current: Node | None = node
    while current is not None:
        if current.type == "identifier":
            return _node_text(current, source_bytes)
        name_field = current.child_by_field_name("declarator")
        if name_field is not None:
            current = name_field
            continue
        identifier = next((child for child in current.named_children if child.type == "identifier"), None)
        if identifier is not None:
            return _node_text(identifier, source_bytes)
        current = next((child for child in current.named_children if "declarator" in child.type), None)
    return ""


def _parameter_arity(declarator: Node, source_bytes: bytes) -> int:
    params = declarator.child_by_field_name("parameters")
    if params is None:
        return 0
    count = 0
    for child in params.named_children:
        if child.type == "parameter_declaration":
            text = _node_text(child, source_bytes).strip()
            if text == "void":
                continue
            count += 1
        elif child.type == "variadic_parameter":
            count += 1
    return count


def _find_body_node(root: Node, function: CFunctionDescriptor) -> Node | None:
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "function_definition":
            body = node.child_by_field_name("body")
            if (
                body is not None
                and body.start_byte == function.body_start_byte
                and body.end_byte == function.body_end_byte
            ):
                return body
        stack.extend(reversed(node.named_children))
    return None


def _walk_calls(node: Node, source_bytes: bytes, names: list[str], seen: set[str]) -> None:
    if node.type == "call_expression":
        function_node = node.child_by_field_name("function")
        name = _call_callee_name(function_node, source_bytes)
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    for child in node.named_children:
        _walk_calls(child, source_bytes, names, seen)


def _call_callee_name(node: Node | None, source_bytes: bytes) -> str:
    current = node
    while current is not None:
        if current.type == "identifier":
            return _node_text(current, source_bytes)
        if current.type == "parenthesized_expression" and current.named_child_count == 1:
            current = current.named_children[0]
            continue
        return ""
    return ""


def _node_text(node: Node | None, source_bytes: bytes) -> str:
    if node is None:
        return ""
    return source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")
