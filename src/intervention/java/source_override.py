from __future__ import annotations

import hashlib
from pathlib import Path

from src.intervention.java.models import InterventionRequest
from src.utils.java_source import JavaMethodDescriptor, METHOD_TYPES, _java_parser


OBS_MARKER = "__CAUSALFL_OBS__"
OBS_VALUE_LIMIT = 200
OBS_MAX_CALLS_DEFAULT = 20
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
    wrapper_name = method.method_name if method.is_constructor else "__CausalFLReplacement"
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


def validate_observation_expressions(expressions) -> list[str]:
    """Require explicit Java expressions; reject statements and direct mutations."""
    if not isinstance(expressions, (list, tuple)) or not 1 <= len(expressions) <= 32:
        raise ValueError("expressions must be a nonempty list of at most 32 Java expressions")
    validated = []
    for expression in expressions:
        if not isinstance(expression, str) or not expression.strip() or len(expression) > 500:
            raise ValueError("each observation expression must be a nonempty string of at most 500 characters")
        expression = expression.strip()
        wrapper = ("class Probe { Object read() { return (" + expression + "); } }").encode("utf-8")
        root = _java_parser().parse(wrapper).root_node
        nodes = []
        stack = [root]
        while stack:
            node = stack.pop()
            nodes.append(node)
            stack.extend(node.named_children)
        returns = [n for n in nodes if n.type == "return_statement"]
        expected = ("(" + expression + ")").encode("utf-8")
        if (root.has_error or len(returns) != 1 or len(returns[0].named_children) != 1
                or returns[0].named_children[0].text != expected
                or any(n.type in _COMMENTS for n in nodes)):
            raise ValueError(f"invalid Java observation expression: {expression}")
        if any(n.type in {"assignment_expression", "update_expression", "lambda_expression",
                          "object_creation_expression", "array_creation_expression"} for n in nodes):
            raise ValueError(f"observation expressions must not assign, increment, allocate, or define code: {expression}")
        if expression not in validated:
            validated.append(expression)
    return validated


def insert_observation_prelude(
    source: str,
    method: JavaMethodDescriptor,
    expressions: list[str] | tuple[str, ...],
    *,
    max_calls: int = OBS_MAX_CALLS_DEFAULT,
    value_limit: int = OBS_VALUE_LIMIT,
) -> str:
    """Print only requested entry expressions from a temporary source copy."""
    expressions = validate_observation_expressions(expressions)
    source_bytes = source.encode("utf-8")
    node = _method_node(source_bytes, method)
    body = node.child_by_field_name("body")
    insertion = body.start_byte + 1
    # Java constructors must execute an explicit this()/super() invocation first.
    if body.named_children and body.named_children[0].type == "explicit_constructor_invocation":
        insertion = body.named_children[0].end_byte
    suffix = hashlib.sha256(source_bytes + str(method.start_byte).encode()).hexdigest()[:12]
    helper = f"__CausalFLProbe_{suffix}"
    call_var = f"__causalfl_call_{suffix}"
    value_var = f"__causalfl_value_{suffix}"
    error_var = f"__causalfl_error_{suffix}"
    lines = ["", f"int {call_var} = {helper}.calls.incrementAndGet();",
             f"if ({call_var} <= {max_calls}) {{"]
    for index, expression in enumerate(expressions):
        # Stable per-expression keys avoid collisions such as this.x and this_x.
        prefix = f'{OBS_MARKER} {method.method_name} call='
        lines.extend([
            "  try {",
            f"    String {value_var} = String.valueOf({expression});",
            f"    if ({value_var}.length() > {value_limit}) {value_var} = {value_var}.substring(0, {value_limit}) + \"...\";",
            f'    {value_var} = {value_var}.replace("\\r", "\\\\r").replace("\\n", "\\\\n");',
            f'    System.err.println("{prefix}" + {call_var} + " expr_{index}=" + {value_var});',
            f"  }} catch (Throwable {error_var}) {{",
            f'    System.err.println("{prefix}" + {call_var} + " expr_{index}=<unprintable>");',
            "  }",
        ])
    lines.extend([f"}} else if ({call_var} == {max_calls + 1}) {{",
                  f'  System.err.println("{OBS_MARKER} {method.method_name} call=" + {call_var} + " __truncated__=true");',
                  "}", ""])
    # Keep counters outside the target type, including inner classes/interfaces.
    helper_source = (f"\nfinal class {helper} {{\n"
                     "  static final java.util.concurrent.atomic.AtomicInteger calls = new java.util.concurrent.atomic.AtomicInteger();\n"
                     "}\n")
    return (source_bytes[:insertion] + "\n".join(lines).encode("utf-8")
            + source_bytes[insertion:]).decode("utf-8") + helper_source
