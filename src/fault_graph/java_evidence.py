"""Static Java source and PoV context; no compilation, test execution, or coverage.

Java preprocessing indexes declarations and static call candidates. Resource
relationships and lifecycle analysis are outside its current scope.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import re

from src.fault_graph.evidence_graph import EvidenceGraph
from src.fault_graph.method_id_map import resolve_crash_method_id
from src.fault_graph.static_call_graph import _MethodIndex, _extract_call_sites
from src.utils.java_source import CLASS_LIKE_TYPES, METHOD_TYPES, _java_parser, _extract_package_name, parse_java_methods
from src.utils.java_util import parse_defects4j_failing_tests, parse_junit_xml_file, split_test_id

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


def index_java_source(graph: EvidenceGraph, project_root: Path, test_roots=()):
    root = project_root.resolve()
    units = []
    known_types = set()
    tests_by_name = {}
    all_methods = []
    for path in _java_files(root):
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
    index = _MethodIndex(all_methods)
    for caller in all_methods:
        caller_eid = method_entities[(caller.file_path, caller.start_byte)]
        for site in _extract_call_sites(caller):
            for callee in index.resolve(site, enclosing=caller):
                target = method_entities[(callee.file_path, callee.start_byte)]
                graph.add_relation(caller_eid, "calls_candidate", target,
                                   dict(extractor="tree_sitter_java", source=str(caller.file_path.relative_to(root)),
                                        resolution="lexical_receiver_and_name_arity_candidates",
                                        interpretation="Static candidate; execution and dynamic dispatch are not established."))
    graph.metadata.update(source_file_count=len(units), source_scope="all_project_java_sources",
                          excluded_directories=sorted(_EXCLUDED_DIRS))
    return tests_by_name, production_methods


def _failure_inputs(root: Path, selected_test: str | None):
    """Read only failure/test-selector fields, never patch or fixing-commit data."""
    failures = {}
    result_path = root / "VUL4J/testing_results.json"
    if result_path.is_file():
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        for failure in payload.get("tests", {}).get("failures", []):
            test_id = f"{failure['test_class']}::{failure['test_method']}"
            failures[test_id] = dict(
                report="\n".join(str(failure.get(key) or "") for key in ("failure_name", "detail")).strip(),
                artifact=str(result_path.relative_to(root)), observed_failure=True,
            )
    for report_path in sorted(root.rglob("TEST-*.xml")):
        if "VUL4J" in report_path.relative_to(root).parts:
            continue
        if not any(part in {"surefire-reports", "failsafe-reports", "test-results"} for part in report_path.parts):
            continue
        for case in parse_junit_xml_file(report_path).failing_tests:
            failures[case.test_id] = dict(report=case.stack_trace or case.failure_message,
                                         artifact=str(report_path.relative_to(root)), observed_failure=True)
    # Defects4J and Defects4J-Trans write this report during `defects4j test`.
    # Import the existing report without executing tests during preprocessing.
    defects4j_report = root / "failing_tests"
    if defects4j_report.is_file():
        for case in parse_defects4j_failing_tests(root):
            test_id = f"{case.class_name}::{case.method_name}"
            failures[test_id] = dict(
                report=case.stack_trace or case.failure_message,
                artifact=defects4j_report.name, observed_failure=True,
            )
    info_path = root / "VUL4J/vulnerability_info.json"
    if info_path.is_file():
        payload = json.loads(info_path.read_text(encoding="utf-8"))
        declared = set()
        for test_id in payload.get("failing_tests", []):
            class_name, method_name = split_test_id(str(test_id))
            declared.add(f"{class_name}::{method_name}")
            failures.setdefault(f"{class_name}::{method_name}", dict(
                report="Dataset-declared PoV test; no failure report was supplied for this test.",
                artifact=str(info_path.relative_to(root)), observed_failure=False))
        if declared:
            failures = {test_id: failure for test_id, failure in failures.items() if test_id in declared}
    if selected_test:
        cls, method = split_test_id(selected_test)
        if not cls or method == "*" or not method:
            raise ValueError("--test_case_id must select a single Java test method")
        test_id = f"{cls}::{method}"
        return {test_id: failures.get(test_id, dict(
            report="Explicitly selected test; no failure report was supplied.",
            artifact="--test_case_id", observed_failure=False))}
    return failures


def build_java_evidence(*, project, case_id: str, test_case_id: str | None = None,
                        failing_tests=None) -> EvidenceGraph:
    root = project.project_path.resolve()
    if not root.is_dir():
        raise ValueError(f"Java project root does not exist: {root}")
    graph = EvidenceGraph(case_id, metadata=dict(
        language="java", dataset=project.spec.dataset, source_root=str(root),
        collection_strategy="static_fault_context", instrumentation=False, coverage_imported=False,
    ))
    tests_by_name, methods = index_java_source(graph, root, project.discover_test_roots())
    if not methods:
        raise ValueError("No Java methods with source bodies were found")
    # Repair rounds use the latest regression result, never stale reports left
    # behind by a single-test validation or an earlier project version.
    failures = (_failure_inputs(root, test_case_id) if failing_tests is None else {
        test.test_id: dict(report=test.stack_trace or test.failure_message,
                           artifact="current_regression", observed_failure=True)
        for test in failing_tests
    })
    if not failures:
        raise ValueError(
            "No PoV/failing test inputs found; provide existing JUnit/VUL4J reports, "
            "Defects4J failing_tests, or --test_case_id"
        )
    records, crash_points = [], {}
    for test_id, failure in failures.items():
        class_name, method_name = split_test_id(test_id)
        source_id = tests_by_name.get(test_id)
        source_entity = graph.entities.get(source_id, {})
        location = source_entity.get("content", {}).get("location", {})
        records.append(dict(
            test_id=test_id, class_name=class_name, method_name=method_name,
            source_entity_id=source_id, file_path=location.get("file"),
            success=False if failure["observed_failure"] else None,
            metadata=dict(outcome_source="existing_failure_report" if failure["observed_failure"] else "test_selector"),
        ))
        report_id = "report:" + test_id
        graph.add_entity(report_id, "crash_report", name=f"failure report: {test_id}",
                         content=dict(text=failure["report"]),
                         provenance=dict(source=failure["artifact"], extractor="failure_report_import"))
        if source_id:
            graph.add_relation(report_id, "failure_of", source_id, dict(source=failure["artifact"]))
        else:
            graph.metadata.setdefault("diagnostics", []).append(dict(kind="test_source_unresolved", test_id=test_id))
        crash = resolve_crash_method_id(failure["report"], list(methods), source_methods_by_spectra=methods)
        if crash:
            crash_points[test_id] = crash
            graph.add_relation(report_id, "reports_frame", graph.aliases[crash],
                               dict(source=failure["artifact"], extractor="java_stack_frame",
                                    interpretation="Reported stack frame; not a root-cause label."))
    graph.metadata.update(test_records=records, crash_points=crash_points)
    return graph
