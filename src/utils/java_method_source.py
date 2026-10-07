from __future__ import annotations
from collections import defaultdict
from pathlib import Path
import re
from src.models import MethodRef
from src.project import Project
from src.utils.java_source import JavaMethodDescriptor, parse_java_methods, discover_top_level_types

def _build_method_record(
    method_id: str,
    source_file_resolver: "_SourceFileResolver",
) -> dict[str, object]:
    try:
        method_ref = MethodRef.from_qualified_name(method_id)
    except ValueError:
        class_name = method_id.split("#", 1)[0].rsplit(".", 1)[-1]
        return {
            "method_id": method_id,
            "class_name": class_name,
            "method_name": method_id.split("#", 1)[-1],
            "package_name": None,
            "signature": None,
            "jvm_descriptor": None,
            "file_path": None,
            "start_line": None,
            "end_line": None,
            "source_code": None,
            "source": "java_source",
            "metadata": {},
        }

    source_file = source_file_resolver.resolve(method_ref)
    descriptor = source_file_resolver.resolve_method_descriptor(method_ref)
    start_line = descriptor.start_line if descriptor is not None else None
    end_line = descriptor.end_line if descriptor is not None else None
    start_byte = descriptor.start_byte if descriptor is not None else None
    end_byte = descriptor.end_byte if descriptor is not None else None
    source_code = None
    if source_file and start_byte is not None and end_byte is not None:
        try:
            file_bytes = (source_file_resolver.project_path / source_file).read_bytes()
            source_code = file_bytes[start_byte:end_byte].decode("utf-8", errors="replace")
        except Exception:
            pass
    return {
        "method_id": method_id,
        "class_name": method_ref.class_name,
        "method_name": method_ref.method_name,
        "package_name": method_ref.package_name,
        "signature": method_ref.signature,
        "jvm_descriptor": method_ref.signature,
        "file_path": source_file,
        "start_line": start_line,
        "end_line": end_line,
        "source_code": source_code,
        "source": "java_source",
        "metadata": {},
    }


class MethodSourceResolver:
    """Public facade for resolving method ids to source-code records.

    Wraps the pipeline's method-id → source-file resolution so other
    components (e.g. ranking evaluation) can attach source snippets without
    reaching into private helpers.
    """

    def __init__(self, project: Project) -> None:
        self._resolver = _SourceFileResolver(project)

    def resolve(self, method_id: str) -> dict[str, object]:
        """Return the method record (file_path, start/end lines, source_code, ...)."""
        return _build_method_record(method_id, self._resolver)


class _SourceFileResolver:
    def __init__(self, project: Project) -> None:
        self.project = project
        self.project_path = project.project_path
        self._candidate_roots = project.discover_source_roots() + project.discover_test_roots()
        self._cache: dict[str, str | None] = {}
        self._basename_index: dict[str, list[Path]] | None = None
        self._file_methods_cache: dict[Path, list[JavaMethodDescriptor]] = {}

    def resolve(self, method_ref: MethodRef) -> str | None:
        qualified_class_name = method_ref.qualified_name.split("#", 1)[0]
        cached = self._cache.get(qualified_class_name)
        if qualified_class_name in self._cache:
            return cached

        resolved = self._resolve_uncached(method_ref)
        self._cache[qualified_class_name] = resolved
        return resolved

    def resolve_method_descriptor(self, method_ref: MethodRef) -> JavaMethodDescriptor | None:
        file_path_text = self.resolve(method_ref)
        if not file_path_text:
            return None
        file_path = self.project_path / Path(file_path_text)
        methods = self._file_methods(file_path)
        qualified_class_name = method_ref.qualified_name.split("#", 1)[0]
        target_jvm_types = _parse_jvm_parameter_types(method_ref.signature)
        candidates = [
            descriptor
            for descriptor in methods
            if descriptor.qualified_class_name == qualified_class_name
            and (
                (method_ref.method_name == "<init>" and descriptor.is_constructor)
                or descriptor.method_name == method_ref.method_name
            )
        ]
        if target_jvm_types is not None:
            type_matches = [
                descriptor
                for descriptor in candidates
                if _source_signature_matches_jvm_types(descriptor.signature, target_jvm_types)
            ]
            if type_matches:
                candidates = type_matches
            else:
                arity = len(target_jvm_types)
                arity_matches = [descriptor for descriptor in candidates if descriptor.parameter_count == arity]
                if arity_matches:
                    candidates = arity_matches
        if not candidates:
            simplified_class_name = method_ref.class_name.split(".")[-1]
            candidates = [
                descriptor
                for descriptor in methods
                if descriptor.class_name.replace("$", ".") == simplified_class_name.replace("$", ".")
                and (
                    (method_ref.method_name == "<init>" and descriptor.is_constructor)
                    or descriptor.method_name == method_ref.method_name
                )
            ]
        if not candidates:
            return None
        return min(candidates, key=lambda item: item.start_line)

    def _resolve_uncached(self, method_ref: MethodRef) -> str | None:
        if not method_ref.package_name:
            return None

        top_level_class = method_ref.class_name.replace("$", ".").split(".", 1)[0]
        relative_path = Path(*method_ref.package_name.split("."), f"{top_level_class}.java")
        for root in self._candidate_roots:
            candidate = root / relative_path
            if candidate.exists():
                return str(candidate.relative_to(self.project_path))

        for candidate in self._basename_candidates(f"{top_level_class}.java"):
            if candidate.name != f"{top_level_class}.java":
                continue
            if _looks_like_test_source(candidate):
                continue
            if _path_matches_package(candidate, method_ref.package_name):
                return str(candidate.relative_to(self.project_path))

        # Package-private sibling top-level types are compiled from another
        # public type's .java file (e.g. BaseNodeDeserializer in JsonNodeDeserializer.java).
        target_fqn = f"{method_ref.package_name}.{top_level_class}"
        for root in self._candidate_roots:
            package_dir = root.joinpath(*method_ref.package_name.split("."))
            if not package_dir.is_dir():
                continue
            for java_file in package_dir.glob("*.java"):
                if _looks_like_test_source(java_file):
                    continue
                try:
                    declared = discover_top_level_types(java_file)
                except OSError:
                    continue
                if target_fqn in declared:
                    return str(java_file.relative_to(self.project_path))
        return None

    def _basename_candidates(self, basename: str) -> list[Path]:
        if self._basename_index is None:
            self._basename_index = defaultdict(list)
            for candidate in self.project_path.rglob("*.java"):
                self._basename_index[candidate.name].append(candidate)
        return self._basename_index.get(basename, [])

    def _file_methods(self, file_path: Path) -> list[JavaMethodDescriptor]:
        cached = self._file_methods_cache.get(file_path)
        if cached is None:
            cached = parse_java_methods(file_path)
            self._file_methods_cache[file_path] = cached
        return cached


def _looks_like_test_source(path: Path) -> bool:
    normalized_parts = {part.lower() for part in path.parts}
    if "test" in normalized_parts or "tests" in normalized_parts:
        return True
    return "src" in normalized_parts and "test" in normalized_parts


def _path_matches_package(path: Path, package_name: str) -> bool:
    expected_parts = tuple(package_name.split("."))
    path_parts = path.parts[:-1]
    if len(path_parts) < len(expected_parts):
        return False
    return tuple(path_parts[-len(expected_parts) :]) == expected_parts


def _parse_jvm_parameter_types(descriptor: str | None) -> list[str] | None:
    """Parse JVM method descriptor parameter types, e.g. ``(Ljava/lang/String;I)Z`` -> ``['Ljava/lang/String;', 'I']``."""
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


def _descriptor_arity(descriptor: str | None) -> int | None:
    parameter_types = _parse_jvm_parameter_types(descriptor)
    if parameter_types is None:
        return None
    return len(parameter_types)


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


def _source_signature_matches_jvm_types(source_signature: str, jvm_types: list[str]) -> bool:
    source_types = _parse_source_signature_types(source_signature)
    if source_types is None or len(source_types) != len(jvm_types):
        return False
    return all(
        _jvm_type_matches_source_type(jvm_type, source_type)
        for jvm_type, source_type in zip(jvm_types, source_types, strict=True)
    )


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
    return False
