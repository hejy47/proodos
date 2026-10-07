from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from src.intervention.java.models import InterventionRequest
from src.utils.java_source import JavaMethodDescriptor, parse_java_methods


@dataclass(frozen=True)
class GateResult:
    ok: bool
    reason: str | None = None


def check_request_supported(
    request: InterventionRequest,
    *,
    target_descriptor: JavaMethodDescriptor | None = None,
) -> GateResult:
    if not (request.replacement_function and request.replacement_function.strip()):
        return GateResult(ok=False, reason="replacement_function is required")
    if target_descriptor is not None:
        modifiers = target_descriptor.modifiers_text.lower()
        if "native" in modifiers:
            return GateResult(ok=False, reason="native method cannot be intervened in v1")
        if "abstract" in modifiers:
            return GateResult(ok=False, reason="abstract method cannot be intervened in v1")
        if _enclosing_type_is_interface(target_descriptor):
            return GateResult(ok=False, reason="interface method cannot be intervened in v1")
    return GateResult(ok=True)


def check_observation_supported(
    target_descriptor: JavaMethodDescriptor,
) -> GateResult:
    """Observation only injects prints; wider than intervention gates."""
    modifiers = target_descriptor.modifiers_text.lower()
    if "native" in modifiers:
        return GateResult(ok=False, reason="native method cannot be observed in v1")
    if "abstract" in modifiers:
        return GateResult(ok=False, reason="abstract method cannot be observed in v1")
    return GateResult(ok=True)


def _enclosing_type_is_interface(method: JavaMethodDescriptor) -> bool:
    """True when the innermost enclosing type is declared as an interface."""
    try:
        from src.intervention.java.source_override import read_java_source

        source = read_java_source(method.file_path)
    except OSError:
        return False
    simple = method.enclosing_classes[-1] if method.enclosing_classes else ""
    if not simple:
        return False
    return (
        re.search(
            rf"\binterface\s+{re.escape(simple)}\b",
            source,
        )
        is not None
    )


def find_target_method_descriptor(
    source_roots: list[Path],
    target_class: str,
    target_method: str,
    parameter_types: tuple[str, ...] = (),
) -> JavaMethodDescriptor | None:
    # JVM method_ids use <init>; tree-sitter names constructors after the class.
    lookup_name = target_method
    if target_method == "<init>":
        lookup_name = target_class.rsplit(".", 1)[-1].rsplit("$", 1)[-1]

    matches: list[JavaMethodDescriptor] = []
    for root in source_roots:
        if not root.is_dir():
            continue
        for java_file in root.rglob("*.java"):
            for method in parse_java_methods(java_file):
                if method.method_name != lookup_name:
                    continue
                if method.qualified_class_name != target_class:
                    continue
                matches.append(method)

    if not matches:
        return None

    # Overlapping source roots can discover the same method twice.
    deduped: list[JavaMethodDescriptor] = []
    seen: set[tuple[Path, int]] = set()
    for method in matches:
        key = (method.file_path.resolve(), method.start_byte)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(method)
    matches = deduped

    # Always disambiguate by parameter_types. An empty tuple selects the no-arg
    # overload (e.g. Foo#bar()V), which must not fall through to "unique name".
    wanted = tuple(_normalize_param(p) for p in parameter_types)
    filtered = [m for m in matches if _source_params(m.signature) == wanted]
    if len(filtered) == 1:
        return filtered[0]

    # Generic methods often declare params as type variables (`M map` for
    # `<M extends Map<...>>`), while method_ids carry the erased JVM type
    # (`Ljava/util/Map;`). Soft-match those when arity agrees.
    soft = [m for m in matches if _params_compatible(m.signature, wanted)]
    if len(soft) == 1:
        return soft[0]

    # Unique method name + matching arity is enough when overloads don't collide.
    same_arity = [m for m in matches if len(_source_params(m.signature)) == len(wanted)]
    if len(same_arity) == 1:
        return same_arity[0]
    return None


def _is_type_variable(name: str) -> bool:
    """Heuristic for Java type-parameter names (T, E, K, V, M, ...)."""
    return len(name) == 1 and name.isupper()


def _params_compatible(signature: str, wanted: tuple[str, ...]) -> bool:
    source = _source_params(signature)
    if source == wanted:
        return True
    if len(source) != len(wanted):
        return False
    for src, want in zip(source, wanted):
        if src == want:
            continue
        # Type var stands for the erased bound (Map, List, Object, ...).
        if _is_type_variable(src):
            continue
        # JVM inner-class names use `$` (DiGraph$DiGraphNode) while source uses
        # the simple nested name (DiGraphNode).
        if "$" in want and want.rsplit("$", 1)[-1] == src:
            continue
        if "$" in src and src.rsplit("$", 1)[-1] == want:
            continue
        return False
    return True


def _normalize_param(param: str) -> str:
    text = param.strip().replace(" ", "")
    # Strip Java generics so Class<?> / Set<Object> match JVM Class / Set.
    if "<" in text:
        text = text[: text.index("<")]
    aliases = {
        "boolean": "boolean",
        "byte": "byte",
        "short": "short",
        "int": "int",
        "long": "long",
        "float": "float",
        "double": "double",
        "char": "char",
        "java.lang.String": "String",
        "String": "String",
    }
    if text in aliases:
        return aliases[text]
    # Prefer the simple name; for JVM inner types keep the trailing segment
    # after `$` so DiGraph$DiGraphNode matches source DiGraphNode.
    simple = text.rsplit(".", 1)[-1]
    if "$" in simple:
        simple = simple.rsplit("$", 1)[-1]
    return simple


_PARAM_MODIFIERS = frozenset(
    {
        "final",
        "public",
        "protected",
        "private",
        "static",
        "volatile",
        "transient",
        "synchronized",
    }
)


def _source_params(signature: str) -> tuple[str, ...]:
    inner = signature.strip()
    if inner.startswith("(") and inner.endswith(")"):
        inner = inner[1:-1].strip()
    if not inner:
        return ()
    # Split on commas only at depth 0 so Map<K, V> stays one parameter.
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in inner:
        if ch == "<":
            depth += 1
            buf.append(ch)
            continue
        if ch == ">":
            depth = max(0, depth - 1)
            buf.append(ch)
            continue
        if ch == "," and depth == 0:
            part = "".join(buf).strip()
            if part:
                parts.append(part)
            buf = []
            continue
        buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)

    types: list[str] = []
    for part in parts:
        tokens = part.split()
        # Skip Java parameter modifiers (e.g. "final int x" -> "int").
        while tokens and tokens[0] in _PARAM_MODIFIERS:
            tokens = tokens[1:]
        types.append(_normalize_param(tokens[0] if tokens else part))
    return tuple(types)
