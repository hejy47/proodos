from __future__ import annotations

from pathlib import Path

from src.patching.java.models import InterventionRequest
from src.utils.java_source import JavaMethodDescriptor, METHOD_TYPES, _java_parser


_COMMENTS = {"line_comment", "block_comment"}


def read_java_source(path: Path) -> str:
    """Preserve newlines; source slicing uses UTF-8 byte offsets from tree-sitter."""
    return Path(path).read_bytes().decode("utf-8")


def _method_node(source: bytes, method: JavaMethodDescriptor):
    root = _java_parser().parse(source).root_node
    node = root.descendant_for_byte_range(method.start_byte, method.end_byte - 1)
    if node.type not in METHOD_TYPES or (node.start_byte, node.end_byte) != (method.start_byte, method.end_byte):
        raise ValueError("target method source no longer matches its indexed span")
    return node


def _declaration_tokens(node, source: bytes) -> list[bytes]:
    """Compare declarations without treating whitespace or comments as changes."""
    body = node.child_by_field_name("body")
    tokens = []

    def visit(child):
        if child == body or child.type in _COMMENTS:
            return
        if not child.children:
            tokens.append(source[child.start_byte:child.end_byte])
        else:
            for descendant in child.children:
                visit(descendant)

    visit(node)
    return tokens


def patch_target_method_source(
    source: str,
    method: JavaMethodDescriptor,
    request: InterventionRequest,
) -> str:
    """Replace exactly one complete method, preserving its declaration and neighbors."""
    replacement = (request.replacement_function or "").strip()
    if not replacement:
        raise ValueError("replacement_function must contain one complete Java method definition")
    # The wrapper supplies parsing context only; it never appears in the project.
    wrapper_name = method.method_name if method.is_constructor else "__ProodosReplacement"
    wrapped = (f"class {wrapper_name} {{\n" + replacement + "\n}").encode("utf-8")
    root = _java_parser().parse(wrapped).root_node
    classes = [n for n in root.named_children if n.type not in _COMMENTS]
    if root.has_error or len(classes) != 1 or classes[0].type != "class_declaration":
        raise ValueError("replacement_function must contain one complete Java method definition")
    members = [n for n in classes[0].child_by_field_name("body").named_children if n.type not in _COMMENTS]
    if len(members) != 1 or members[0].type not in METHOD_TYPES or members[0].child_by_field_name("body") is None:
        raise ValueError("replacement_function must contain exactly one complete Java method, without a class or extra members")
    changed = members[0]
    original_bytes = source.encode("utf-8")
    original = _method_node(original_bytes, method)
    if changed.type != original.type or _declaration_tokens(changed, wrapped) != _declaration_tokens(original, original_bytes):
        raise ValueError(
            "replacement_function must preserve the original method declaration "
            "(name, parameters, return type, modifiers, annotations, and throws); change only its body"
        )
    return (original_bytes[:method.start_byte]
            + wrapped[changed.start_byte:changed.end_byte]
            + original_bytes[method.end_byte:]).decode("utf-8")
