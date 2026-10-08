from __future__ import annotations

from dataclasses import dataclass
import re

from src.utils.java_source import JavaMethodDescriptor


_STACK_FRAME_RE = re.compile(
    r"^\s*at\s+(?P<class>[\w.$]+)\.(?P<method>[\w$<>]+)\((?P<file>[^:)]+)(?::(?P<line>\d+))?\)\s*$"
)

_SKIP_FRAME_PREFIXES = (
    "java.",
    "javax.",
    "jdk.",
    "sun.",
    "org.junit.",
    "junit.",
    "org.hamcrest.",
    "org.testng.",
    "com.sun.",
    "proodos.runner.",
    "org.apache.maven.",
    "org.apache.tools.ant.",
)

_SOURCE_PRIMITIVE_TO_JVM: dict[str, str] = {
    "byte": "B",
    "char": "C",
    "double": "D",
    "float": "F",
    "int": "I",
    "long": "J",
    "short": "S",
    "boolean": "Z",
}


@dataclass(frozen=True)
class StackFrame:
    class_name: str
    method_name: str
    file_name: str | None = None
    line: int | None = None


def fingerprint_spectra_method_id(method_id: str) -> tuple[str, str, int] | None:
    if "#" not in method_id:
        return None
    class_part, _, rest = method_id.partition("#")
    name, _, _tail = rest.partition("(")
    if not name:
        return None
    params = _parse_jvm_parameter_types(rest[len(name) :] if name else rest)
    if params is None:
        params = _parse_jvm_parameter_types(rest[rest.find("(") :] if "(" in rest else "()")
    arity = len(params) if params is not None else 0
    return class_part, name, arity


def resolve_known_method_id(
    query: str,
    known_ids: list[str] | tuple[str, ...] | set[str],
) -> str | None:
    """Map an exact or fingerprint-equivalent method_id onto a known catalog id.

    Agents often invent fully-qualified JVM descriptors from stack frames while
    spectra/expanded catalogs may store under-qualified parameter types (or the
    reverse). Match on ``(class, method_name, arity)`` and prefer the most
    qualified known id when several collide.
    """
    term = (query or "").strip()
    if not term:
        return None
    known_list = list(known_ids)
    known_set = set(known_list)
    if term in known_set:
        return term
    key = fingerprint_spectra_method_id(term)
    if key is None:
        if "#" in term:
            return None
        # Bare function name (common for C-style path#func ids): accept a
        # unique method-name match; stay unresolved when ambiguous.
        name_matches = [
            method_id
            for method_id in known_list
            if method_id.rsplit("#", 1)[-1].split("(", 1)[0] == term
        ]
        if len(name_matches) == 1:
            return name_matches[0]
        return None
    matches = [
        method_id
        for method_id in known_list
        if fingerprint_spectra_method_id(method_id) == key
    ]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    return sorted(matches, key=_qualification_score, reverse=True)[0]


def fingerprint_source_method(descriptor: JavaMethodDescriptor) -> tuple[str, str, int]:
    name = "<init>" if descriptor.is_constructor else descriptor.method_name
    return descriptor.qualified_class_name, name, descriptor.parameter_count


def map_source_method_to_spectra(
    descriptor: JavaMethodDescriptor,
    spectra_ids: list[str] | tuple[str, ...] | set[str],
) -> str | None:
    mapping = build_source_to_spectra_map([descriptor], list(spectra_ids))
    return mapping.get(descriptor.method_id)


def build_source_to_spectra_map(
    methods: list[JavaMethodDescriptor],
    spectra_ids: list[str],
) -> dict[str, str]:
    by_fingerprint: dict[tuple[str, str, int], list[str]] = {}
    for method_id in spectra_ids:
        key = fingerprint_spectra_method_id(method_id)
        if key is None:
            continue
        by_fingerprint.setdefault(key, []).append(method_id)

    mapping: dict[str, str] = {}
    for method in methods:
        key = fingerprint_source_method(method)
        candidates = list(by_fingerprint.get(key, ()))
        if not candidates:
            continue
        chosen = _pick_best_spectra_candidate(method, candidates)
        if chosen is not None:
            mapping[method.method_id] = chosen
    return mapping


def parse_stack_frames(stacktrace: str | None) -> list[StackFrame]:
    if not stacktrace:
        return []
    frames: list[StackFrame] = []
    for raw_line in str(stacktrace).splitlines():
        match = _STACK_FRAME_RE.match(raw_line.rstrip())
        if match is None:
            continue
        line_text = match.group("line")
        frames.append(
            StackFrame(
                class_name=match.group("class"),
                method_name=match.group("method"),
                file_name=match.group("file"),
                line=int(line_text) if line_text else None,
            )
        )
    return frames


def resolve_crash_method_id(
    stacktrace: str | None,
    pruned_method_ids: set[str] | list[str] | tuple[str, ...],
    *,
    source_methods_by_spectra: dict[str, JavaMethodDescriptor] | None = None,
) -> str | None:
    pruned = set(pruned_method_ids)
    if not pruned:
        return None

    by_class_method: dict[tuple[str, str], list[str]] = {}
    for method_id in pruned:
        key = fingerprint_spectra_method_id(method_id)
        if key is None:
            continue
        class_name, method_name, _arity = key
        by_class_method.setdefault((class_name, method_name), []).append(method_id)

    for frame in parse_stack_frames(stacktrace):
        if _is_skipped_frame(frame):
            continue
        frame_method = frame.method_name
        if frame_method == frame.class_name.rsplit(".", 1)[-1].rsplit("$", 1)[-1]:
            frame_method = "<init>"
        candidates = by_class_method.get((frame.class_name, frame_method), [])
        if not candidates:
            continue
        if len(candidates) == 1:
            return candidates[0]
        if source_methods_by_spectra and frame.line is not None:
            line_matches = [
                method_id
                for method_id in candidates
                if (descriptor := source_methods_by_spectra.get(method_id)) is not None
                and descriptor.start_line <= frame.line <= descriptor.end_line
            ]
            if len(line_matches) == 1:
                return line_matches[0]
            if line_matches:
                return min(
                    line_matches,
                    key=lambda mid: (
                        source_methods_by_spectra[mid].end_line - source_methods_by_spectra[mid].start_line,
                        mid,
                    ),
                )
        return sorted(candidates, key=_qualification_score, reverse=True)[0]
    return None


def resolve_crash_method_id_from_trace(
    events: list[str] | tuple[str, ...],
    method_ids_by_trace_id: dict[int, str],
) -> str | None:
    """Infer crash method from compact enter/exit trace tokens.

    Prefer the last method with an ``eN`` that has no matching ``xN`` (still on
    the call stack). If every enter has an exit, use the method of the last
    trace event.
    """
    open_ids: list[int] = []
    last_seen: int | None = None
    for raw in events:
        token = str(raw).strip()
        if len(token) < 2 or token[0] not in {"e", "x"}:
            continue
        try:
            trace_method_id = int(token[1:])
        except ValueError:
            continue
        last_seen = trace_method_id
        if token[0] == "e":
            open_ids.append(trace_method_id)
            continue
        for index in range(len(open_ids) - 1, -1, -1):
            if open_ids[index] == trace_method_id:
                open_ids.pop(index)
                break

    chosen = open_ids[-1] if open_ids else last_seen
    if chosen is None:
        return None
    return method_ids_by_trace_id.get(chosen)


def _pick_best_spectra_candidate(
    descriptor: JavaMethodDescriptor,
    candidates: list[str],
) -> str | None:
    if len(candidates) == 1:
        return candidates[0]
    scored: list[tuple[tuple[int, int, int, int], str]] = []
    for method_id in candidates:
        overlap = _param_token_overlap(descriptor, method_id)
        scored.append(((overlap, *_qualification_score(method_id)), method_id))
    scored.sort(reverse=True)
    return scored[0][1] if scored else None


def _param_token_overlap(descriptor: JavaMethodDescriptor, method_id: str) -> int:
    source_types = _parse_source_signature_types(descriptor.signature) or []
    jvm_types = _parse_jvm_parameter_types(method_id.partition("#")[2]) or []
    if len(source_types) != len(jvm_types):
        return 0
    return sum(
        1
        for source_type, jvm_type in zip(source_types, jvm_types, strict=True)
        if _jvm_type_matches_source_type(jvm_type, source_type)
    )


def _qualification_score(method_id: str) -> tuple[int, int, int]:
    desc = method_id.split("#", 1)[1] if "#" in method_id else method_id
    return (desc.count("/"), desc.count("$"), len(method_id))


def _is_skipped_frame(frame: StackFrame) -> bool:
    class_name = frame.class_name
    if any(class_name.startswith(prefix) for prefix in _SKIP_FRAME_PREFIXES):
        return True
    simple = class_name.rsplit(".", 1)[-1]
    if "Test" in simple or simple.endswith("Tests"):
        return True
    return False


def _parse_jvm_parameter_types(descriptor: str | None) -> list[str] | None:
    if descriptor is None:
        return None
    text = descriptor.strip()
    paren_start = text.find("(")
    if paren_start == -1:
        return None
    parameter_types: list[str] = []
    index = paren_start + 1
    while index < len(text) and text[index] != ")":
        if text[index] == "[":
            type_start = index
            index += 1
            while index < len(text) and text[index] == "[":
                index += 1
            if index < len(text) and text[index] == "L":
                semicolon_index = text.find(";", index)
                if semicolon_index == -1:
                    return parameter_types
                index = semicolon_index + 1
            else:
                index += 1
            parameter_types.append(text[type_start:index])
            continue
        if text[index] == "L":
            type_start = index
            semicolon_index = text.find(";", index)
            if semicolon_index == -1:
                return parameter_types
            index = semicolon_index + 1
            parameter_types.append(text[type_start:index])
            continue
        parameter_types.append(text[index])
        index += 1
    return parameter_types


def _parse_source_signature_types(signature: str) -> list[str] | None:
    text = signature.strip()
    if not text.startswith("("):
        return None
    close_index = text.find(")")
    if close_index == -1:
        return None
    inner = text[1:close_index].strip()
    if not inner:
        return []
    return [_parameter_declaration_type(part) for part in _split_parameter_declarations(inner)]


def _split_parameter_declarations(inner: str) -> list[str]:
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


def _parameter_declaration_type(parameter: str) -> str:
    text = re.sub(r"@\w+(?:\([^)]*\))?\s*", "", parameter.strip())
    for modifier in ("final ", "volatile "):
        if text.startswith(modifier):
            text = text[len(modifier) :]
    text = text.replace("...", "").strip()
    tokens = text.rsplit(None, 1)
    if len(tokens) == 2 and re.fullmatch(r"[\w$]+", tokens[1]):
        return _erase_generics(tokens[0].strip())
    return _erase_generics(text)


def _erase_generics(type_name: str) -> str:
    return re.sub(r"<[^<>]*(?:<[^<>]*>[^<>]*)*>", "", type_name).strip()


def _jvm_type_matches_source_type(jvm_type: str, source_type: str) -> bool:
    primitive = _SOURCE_PRIMITIVE_TO_JVM.get(source_type)
    if primitive is not None:
        return jvm_type == primitive
    source_simple = source_type.split(".")[-1]
    if jvm_type.startswith("L") and jvm_type.endswith(";"):
        jvm_simple = jvm_type[1:-1].split("/")[-1]
        return jvm_simple == source_simple
    if jvm_type.startswith("[") and source_type.endswith("[]"):
        return _jvm_type_matches_source_type(jvm_type.lstrip("["), source_type[: -2].strip())
    return False
