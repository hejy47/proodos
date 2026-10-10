"""Static Java source index; no failure reports, test execution, or coverage.

Java preprocessing indexes declarations and static call candidates. Resource
relationships and lifecycle analysis are outside its current scope.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re

from src.fault_graph.evidence_graph import EvidenceGraph
from src.fault_graph.java_call_resolver import JavaCallResolver
from src.fault_graph.static_call_graph import _extract_call_sites
from src.utils.java_source import CLASS_LIKE_TYPES, METHOD_TYPES, _java_parser, _extract_package_name, parse_java_methods

_EXCLUDED_DIRS = {".git", ".gradle", ".idea", ".mvn", "target", "out", "node_modules", "VUL4J"}
_PRIMITIVES = dict(byte="B", char="C", double="D", float="F", int="I", long="J", short="S", boolean="Z", void="V")
_JAVA_LANG = set("Object String StringBuilder StringBuffer Class Throwable Exception RuntimeException Error "
                 "Boolean Byte Character Double Float Integer Long Short Number Void Iterable Comparable "
                 "CharSequence Enum Thread Runnable Cloneable AutoCloseable AssertionError "
                 "IllegalArgumentException IllegalStateException NullPointerException UnsupportedOperationException".split())


def _nodes(root):
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.named_children))


def _text(node, data):
    return data[node.start_byte:node.end_byte].decode("utf-8", errors="replace") if node else ""


def _java_files(root: Path):
    for directory, dirs, files in os.walk(root, followlinks=False):
        current = Path(directory)
        dirs[:] = sorted(
            name for name in dirs
            if not name.startswith(".")
            and name not in _EXCLUDED_DIRS
            and not _is_build_output_directory(root, current / name)
        )
        for name in sorted(files):
            path = Path(directory) / name
            if name.endswith(".java") and path.resolve().is_relative_to(root):
                yield path


def _is_build_output_directory(root: Path, path: Path) -> bool:
    """Skip build outputs without excluding Java packages named ``build``."""
    if path.name != "build":
        return False
    relative_parts = path.relative_to(root).parts
    # Build directories inside conventional source trees may be package names
    # (for example ``src/main/java/.../steps/build``). Generated Gradle
    # outputs instead live in top-level/module ``build`` directories.
    return not any(part in {"src", "source"} for part in relative_parts[:-1])


def _erase_type(text: str) -> str:
    while re.search(r"<[^<>]*>", text):
        text = re.sub(r"<[^<>]*>", "", text)
    return re.sub(r"\s+", "", text)


def _type_descriptor(type_name, method, imports, wildcard_imports, known_types, variables):
    name = _erase_type(type_name).replace("...", "[]")
    dimensions = 0
    while name.endswith("[]"):
        dimensions += 1
        name = name[:-2]
    if name in variables:
        name = variables[name]
    if name in _PRIMITIVES:
        base = _PRIMITIVES[name]
    else:
        first, _, rest = name.partition(".")
        if first in imports:
            name = imports[first] + ("." + rest if rest else "")
        elif name in _JAVA_LANG:
            name = "java.lang." + name
        elif method.qualified_class_name + "$" + name in known_types:
            name = method.qualified_class_name + "$" + name
        elif method.package_name and method.package_name + "." + name in known_types:
            name = method.package_name + "." + name
        elif len(wildcard_imports) == 1 and "." not in name:
            name = wildcard_imports[0] + "." + name
        # Resolve nested project types to their binary names without guessing
        # package boundaries for external types.
        name = next((known for known in sorted(known_types) if known.replace("$", ".") == name), name)
        base = "L" + name.replace(".", "/") + ";"
    return "[" * dimensions + base


def _method_id(method, node, data, imports, wildcard_imports, known_types):
    variables = {}
    parents = []
    parent = node
    while parent is not None:
        parents.append(parent)
        parent = parent.parent
    for parent in reversed(parents):
        params = parent.child_by_field_name("type_parameters")
        for parameter in params.named_children if params else ():
            if parameter.type != "type_parameter":
                continue
            name_node = parameter.child_by_field_name("name") or parameter.named_children[0]
            bound = next((child for child in parameter.named_children if child.type == "type_bound"), None)
            variables[_text(name_node, data)] = _text(bound.named_children[0], data) if bound and bound.named_children else "Object"
    def descriptor(type_name):
        return _type_descriptor(type_name, method, imports, wildcard_imports, known_types, variables)
    args = []
    parameters = node.child_by_field_name("parameters")
    for parameter in parameters.named_children if parameters else ():
        if parameter.type not in {"formal_parameter", "spread_parameter"}:
            continue
        type_node = parameter.child_by_field_name("type")
        if type_node is None:
            type_node = next((child for child in parameter.named_children
                              if child.type not in {"modifiers", "variable_declarator", "identifier", "dimensions"}), None)
        type_name = _text(type_node, data)
        if parameter.type == "spread_parameter":
            type_name += "[]"
        for child in parameter.named_children:
            if child.type == "dimensions":
                type_name += _text(child, data)
        args.append(descriptor(type_name))
    ret = "V" if method.is_constructor else descriptor(method.return_type or "void")
    name = "<init>" if method.is_constructor else method.method_name
    return f"{method.qualified_class_name}#{name}({''.join(args)}){ret}"


def index_java_source(graph: EvidenceGraph, project_root: Path, test_roots=(), *,
                      files=None, external_methods=(), external_symbols=None, known_types=()):
    root = project_root.resolve()
    units = []
    known_types = set(known_types)
    tests_by_name = {}
    all_methods = []
    for path in _java_files(root) if files is None else files:
        data = path.read_bytes()
        tree = _java_parser().parse(data)
        methods = parse_java_methods(path)
        rel = path.relative_to(root).as_posix()
        test_source = any(path.is_relative_to(Path(t).resolve()) for t in test_roots) or bool({"test", "tests"} & set(path.relative_to(root).parts))
        known_types.update(method.qualified_class_name for method in methods)
        package = _extract_package_name(tree.root_node, data)
        for node in _nodes(tree.root_node):
            if node.type not in CLASS_LIKE_TYPES:
                continue
            names = []
            ancestor = node
            while ancestor is not None:
                if ancestor.type in CLASS_LIKE_TYPES:
                    names.append(_text(ancestor.child_by_field_name("name"), data))
                ancestor = ancestor.parent
            local = "$".join(reversed(names))
            known_types.add(f"{package}.{local}" if package else local)
        graph.metadata.setdefault("source_sha256", {})[rel] = hashlib.sha256(data).hexdigest()
        try:
            data.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            encoding = "iso-8859-1"
        graph.metadata.setdefault("source_encodings", {})[rel] = encoding
        if tree.root_node.has_error:
            graph.metadata.setdefault("diagnostics", []).append(dict(kind="java_parse_error", file=rel))
        units.append((path, rel, data, tree, methods, test_source))
        all_methods.extend(methods)
    method_entities = {}
    production_methods = {}
    for path, rel, data, tree, methods, test_source in units:
        nodes_by_start = {node.start_byte: node for node in _nodes(tree.root_node) if node.type in METHOD_TYPES}
        imports, wildcards = {}, []
        for node in tree.root_node.named_children:
            if node.type != "import_declaration":
                continue
            imported = _text(node, data).removeprefix("import ").removeprefix("static ").rstrip(";").strip()
            if imported.endswith(".*"):
                wildcards.append(imported[:-2])
            else:
                imports[imported.rsplit(".", 1)[-1]] = imported
        for method in methods:
            node = nodes_by_start[method.start_byte]
            mid = _method_id(method, node, data, imports, wildcards, known_types)
            if mid in graph.aliases:
                graph.metadata.setdefault("diagnostics", []).append(dict(kind="duplicate_java_method", method_id=mid, file=rel))
                mid += f"@{rel}:{method.start_line}"
            entity_type = "test_method" if test_source else "function"
            entity_prefix = "test_method:" if test_source else "func:"
            eid = entity_prefix + mid
            # Test methods are deliberately not added to ``graph.aliases``.
            # Large Maven monorepos can contain the same fully qualified test
            # class in multiple modules, so use the source location to keep
            # those test entities distinct as well.
            if eid in graph.entities:
                graph.metadata.setdefault("diagnostics", []).append(
                    dict(kind="duplicate_java_method", method_id=mid, file=rel)
                )
                mid += f"@{rel}:{method.start_line}"
                eid = entity_prefix + mid
            content = dict(
                method_id=mid, class_name=method.qualified_class_name, signature=method.signature,
                descriptor_basis="source_erased_types",
                location=dict(file=rel, line=method.start_line, end_line=method.end_line),
                source_span=dict(file=rel, start_byte=method.start_byte, end_byte=method.end_byte),
            )
            graph.add_entity(eid, entity_type,
                             name=method.method_name, content=content,
                             provenance=dict(extractor="tree_sitter_java", source=rel),
                             coverage={"status": "unavailable"})
            method_entities[(path, method.start_byte)] = eid
            if test_source:
                tests_by_name[f"{method.qualified_class_name}::{method.method_name}"] = eid
            else:
                production_methods[mid] = method
        for node in _nodes(tree.root_node):
            if node.type not in CLASS_LIKE_TYPES | {"field_declaration", "constant_declaration", "import_declaration"}:
                continue
            kind = ("type_definition" if node.type in CLASS_LIKE_TYPES else
                    "import" if node.type == "import_declaration" else "field")
            name_node = node.child_by_field_name("name")
            name = _text(name_node, data)
            if not name:
                name = _text(node, data).splitlines()[0][:160]
            eid = f"source:{rel}:{node.start_byte}:{kind}"
            graph.add_entity(eid, kind, name=name,
                             content=dict(location=dict(file=rel, line=node.start_point.row + 1,
                                                        end_line=node.end_point.row + 1),
                                          source_span=dict(file=rel, start_byte=node.start_byte, end_byte=node.end_byte),
                                          test_source=test_source),
                             provenance=dict(extractor="tree_sitter_java", source=rel))
            if kind == "type_definition":
                for method in methods:
                    owner = nodes_by_start[method.start_byte].parent
                    while owner is not None and owner.type not in CLASS_LIKE_TYPES:
                        owner = owner.parent
                    if owner is not None and owner.start_byte == node.start_byte:
                        graph.add_relation(eid, "declares", method_entities[(path, method.start_byte)],
                                           dict(extractor="tree_sitter_java", source=rel))
    from src.fault_graph.java_repair_index import build_repair_index
    graph.call_index = build_repair_index(units, method_entities, known_types, include_tests=True)
    production_files = {rel for _, rel, _, _, _, test_source in units if not test_source}
    graph.repair_index = {kind: [r for r in records if r["file"] in production_files]
                          for kind, records in graph.call_index.items()}
    graph.metadata["repair_ingredients_version"] = 1
    external = {method: eid for method, eid in external_methods}
    symbols = {kind: records + (external_symbols or {}).get(kind, [])
               for kind, records in graph.call_index.items()}
    index = JavaCallResolver(all_methods + list(external), symbols)
    call_bodies = {(path, node.start_byte): (data, node.child_by_field_name("body"))
                   for path, _, data, tree, _, _ in units
                   for node in _nodes(tree.root_node) if node.type in METHOD_TYPES}
    call_stats = dict(version=2, strategy="lexical_types_and_class_hierarchy", sites=0, resolved_sites=0,
                      unresolved_or_external_sites=0, candidate_targets=0)
    for caller in all_methods:
        caller_eid = method_entities[(caller.file_path, caller.start_byte)]
        data, body = call_bodies[caller.file_path, caller.start_byte]
        for site in _extract_call_sites(caller, source_bytes=data, body=body):
            callees = index.resolve(site, enclosing=caller)
            call_stats["sites"] += 1
            call_stats["resolved_sites" if callees else "unresolved_or_external_sites"] += 1
            call_stats["candidate_targets"] += len(callees)
            for callee in callees:
                target = method_entities.get((callee.file_path, callee.start_byte)) or external[callee]
                if target not in graph.entities:
                    # A file refresh references existing nodes without rewriting them.
                    graph.add_entity(target, "source_reference", name=callee.method_name,
                                     content={}, provenance={}, reference_only=True)
                graph.add_relation(caller_eid, "calls_candidate", target,
                                   dict(extractor="tree_sitter_java", source=str(caller.file_path.relative_to(root)),
                                        resolution="lexical_types_and_class_hierarchy",
                                        interpretation="Static candidate; execution and dynamic dispatch are not established."))
    graph.metadata.update(source_file_count=len(units), source_scope="all_project_java_sources",
                          excluded_directories=sorted(_EXCLUDED_DIRS), java_call_resolution=call_stats)
    return tests_by_name, production_methods


def build_java_evidence(*, project, case_id: str) -> EvidenceGraph:
    """Index project sources; debug adds failure context from its regression."""
    root = project.project_path.resolve()
    if not root.is_dir():
        raise ValueError(f"Java project root does not exist: {root}")
    graph = EvidenceGraph(case_id, metadata=dict(
        language="java", dataset=project.spec.dataset, source_root=str(root),
        collection_strategy="static_fault_context", instrumentation=False, coverage_imported=False,
        test_records=[], crash_points={},
    ))
    _, methods = index_java_source(graph, root, project.discover_test_roots())
    if not methods:
        raise ValueError("No Java methods with source bodies were found")
    return graph
