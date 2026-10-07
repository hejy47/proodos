from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from tree_sitter import Language, Node, Parser
import tree_sitter_java

from src.models import TestCase


JAVA_LANGUAGE = Language(tree_sitter_java.language())
CLASS_LIKE_TYPES = {
    "class_declaration",
    "enum_declaration",
    "interface_declaration",
    "record_declaration",
}
METHOD_TYPES = {
    "method_declaration",
    "constructor_declaration",
}


@dataclass(frozen=True)
class JavaMethodDescriptor:
    file_path: Path
    package_name: str | None
    enclosing_classes: tuple[str, ...]
    method_name: str
    signature: str
    modifiers_text: str
    return_type: str | None
    start_byte: int
    end_byte: int
    start_line: int
    end_line: int
    body_start_byte: int
    body_end_byte: int
    body_start_column: int
    is_constructor: bool = False

    @property
    def class_name(self) -> str:
        return "$".join(self.enclosing_classes)

    @property
    def qualified_class_name(self) -> str:
        if self.package_name:
            return f"{self.package_name}.{self.class_name}"
        return self.class_name

    @property
    def method_id(self) -> str:
        return f"{self.qualified_class_name}#{self.method_name}{self.signature}"

    @property
    def is_test_method(self) -> bool:
        annotation_markers = (
            "@Test",
            "@ParameterizedTest",
            "@RepeatedTest",
            "@TestFactory",
            "@TestTemplate",
        )
        if any(marker in self.modifiers_text for marker in annotation_markers):
            return True
        return self.method_name.startswith("test")

    @property
    def parameter_count(self) -> int:
        signature = self.signature.strip()
        if not signature.startswith("(") or not signature.endswith(")"):
            return 0
        inner = signature[1:-1].strip()
        if not inner:
            return 0
        depth = 0
        count = 1
        for char in inner:
            if char in "(<[":
                depth += 1
            elif char in ")>]":
                depth = max(0, depth - 1)
            elif char == "," and depth == 0:
                count += 1
        return count


def _java_parser() -> Parser:
    return Parser(JAVA_LANGUAGE)


def parse_java_methods(file_path: Path) -> list[JavaMethodDescriptor]:
    source_bytes = file_path.read_bytes()
    tree = _java_parser().parse(source_bytes)
    root = tree.root_node
    package_name = _extract_package_name(root, source_bytes)
    methods: list[JavaMethodDescriptor] = []
    _collect_methods(root, source_bytes, file_path, package_name, (), methods, {})
    return methods


def discover_test_cases_in_file(file_path: Path) -> list[TestCase]:
    test_cases: list[TestCase] = []
    for method in parse_java_methods(file_path):
        if not method.is_test_method:
            continue
        test_cases.append(
            TestCase(
                test_id=f"{method.qualified_class_name}::{method.method_name}",
                class_name=method.qualified_class_name,
                method_name=method.method_name,
                file_path=file_path,
                metadata={
                    "signature": method.signature,
                    "modifiers": method.modifiers_text,
                },
            )
        )
    return test_cases


def extract_package_name(file_path: Path) -> str | None:
    source_bytes = file_path.read_bytes()
    tree = _java_parser().parse(source_bytes)
    return _extract_package_name(tree.root_node, source_bytes)


def discover_package_prefixes(source_roots: list[Path]) -> list[str]:
    prefixes: set[str] = set()
    for source_root in source_roots:
        for file_path in source_root.rglob("*.java"):
            package_name = extract_package_name(file_path)
            if package_name:
                prefixes.add(package_name)
    reduced: list[str] = []
    for candidate in sorted(prefixes, key=lambda value: (len(value.split(".")), value)):
        if any(candidate == existing or candidate.startswith(existing + ".") for existing in reduced):
            continue
        reduced.append(candidate)
    return reduced

def discover_include_classes(source_roots: list[Path]) -> list[str]:
    """Collect FQNs that the trace agent should instrument.

    Path-stem classes cover the usual public top-level type per file. Java also
    allows additional package-private top-level types in the same compilation
    unit (e.g. ``BaseNodeDeserializer`` beside ``JsonNodeDeserializer``). Those
    compile to distinct ``.class`` files and must be listed explicitly —
    ``startswith`` matching on the public type name does not cover them.
    """
    include_classes: set[str] = set()
    for source_root in source_roots:
        for file_path in source_root.rglob("*.java"):
            rel_path = file_path.relative_to(source_root)
            class_name = str(rel_path).replace("/", ".").replace("\\", ".")
            if class_name.endswith(".java"):
                class_name = class_name[:-5]
            if class_name:
                include_classes.add(class_name)
            include_classes.update(discover_top_level_types(file_path))
    return list(include_classes)


def discover_top_level_types(file_path: Path) -> list[str]:
    """Return FQNs of top-level class/enum/interface/record types in a Java file."""
    try:
        source_bytes = file_path.read_bytes()
    except OSError:
        return []
    tree = _java_parser().parse(source_bytes)
    root = tree.root_node
    package_name = _extract_package_name(root, source_bytes)
    names: list[str] = []
    for child in root.named_children:
        if child.type not in CLASS_LIKE_TYPES:
            continue
        name_node = child.child_by_field_name("name")
        if name_node is None:
            continue
        simple = _node_text(name_node, source_bytes)
        if not simple:
            continue
        names.append(f"{package_name}.{simple}" if package_name else simple)
    return names


def _collect_methods(
    node: Node,
    source_bytes: bytes,
    file_path: Path,
    package_name: str | None,
    class_stack: tuple[str, ...],
    methods: list[JavaMethodDescriptor],
    anonymous_class_counts: dict[tuple[str, ...], int],
) -> None:
    if node.type in CLASS_LIKE_TYPES:
        name_node = node.child_by_field_name("name")
        body_node = node.child_by_field_name("body")
        if name_node is None or body_node is None:
            return
        class_name = _node_text(name_node, source_bytes)
        next_stack = (*class_stack, class_name)
        for child in body_node.named_children:
            _collect_methods(child, source_bytes, file_path, package_name, next_stack, methods, anonymous_class_counts)
        return

    if node.type in {"object_creation_expression", "enum_constant"} and class_stack:
        body_node = _find_direct_child(node, "class_body")
        if body_node is None:
            return
        anonymous_index = anonymous_class_counts.get(class_stack, 0) + 1
        anonymous_class_counts[class_stack] = anonymous_index
        next_stack = (*class_stack, str(anonymous_index))
        for child in body_node.named_children:
            _collect_methods(child, source_bytes, file_path, package_name, next_stack, methods, anonymous_class_counts)
        return

    if node.type in METHOD_TYPES and class_stack:
        body_node = node.child_by_field_name("body")
        expected_body_types = {"constructor_body"} if node.type == "constructor_declaration" else {"block"}
        if body_node is None or body_node.type not in expected_body_types:
            return

        name_node = node.child_by_field_name("name")
        parameters_node = node.child_by_field_name("parameters")
        modifiers_node = _find_direct_child(node, "modifiers")
        type_node = node.child_by_field_name("type")
        method_name = _node_text(name_node, source_bytes) if name_node is not None else class_stack[-1]
        signature = _normalize_signature(_node_text(parameters_node, source_bytes) if parameters_node is not None else "()")
        modifiers_text = _node_text(modifiers_node, source_bytes) if modifiers_node is not None else ""
        return_type = _node_text(type_node, source_bytes) if type_node is not None else None
        methods.append(
            JavaMethodDescriptor(
                file_path=file_path,
                package_name=package_name,
                enclosing_classes=class_stack,
                method_name=method_name,
                signature=signature,
                modifiers_text=modifiers_text,
                return_type=return_type,
                start_byte=node.start_byte,
                end_byte=node.end_byte,
                start_line=node.start_point.row + 1,
                end_line=node.end_point.row + 1,
                body_start_byte=body_node.start_byte,
                body_end_byte=body_node.end_byte,
                body_start_column=body_node.start_point.column,
                is_constructor=node.type == "constructor_declaration",
            )
        )
        return

    for child in node.named_children:
        _collect_methods(child, source_bytes, file_path, package_name, class_stack, methods, anonymous_class_counts)


def _extract_package_name(root: Node, source_bytes: bytes) -> str | None:
    for child in root.named_children:
        if child.type != "package_declaration":
            continue
        text = _node_text(child, source_bytes)
        if not text.startswith("package "):
            return None
        return text.removeprefix("package ").rstrip(";").strip()
    return None


def _node_text(node: Node | None, source_bytes: bytes) -> str:
    if node is None:
        return ""
    return source_bytes[node.start_byte : node.end_byte].decode("utf-8")


def _normalize_signature(signature: str) -> str:
    return " ".join(signature.split())


def _find_direct_child(node: Node, node_type: str) -> Node | None:
    for child in node.children:
        if child.type == node_type:
            return child
    return None
