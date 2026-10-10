"""Project-only repair ingredients extracted once from Java syntax trees.

Source stays in the checkout. Records contain declarations, lexical scopes,
comments and verified byte spans, never copies of method bodies.
"""
from __future__ import annotations

from dataclasses import asdict
import re
from types import SimpleNamespace

from src.utils.java_source import CLASS_LIKE_TYPES, METHOD_TYPES


def walk(node):
    yield node
    for child in node.named_children:
        yield from walk(child)


def text(node, data):
    return data[node.start_byte:node.end_byte].decode("utf-8", errors="replace") if node else ""


def span(node):
    return dict(start_byte=node.start_byte, end_byte=node.end_byte,
                start_line=node.start_point.row + 1, end_line=node.end_point.row + 1)


def owner(node):
    parent = node.parent
    while parent is not None:
        if parent.type in CLASS_LIKE_TYPES or parent.type == "class_body" and parent.parent.type in {"object_creation_expression", "enum_constant"}:
            return parent
        parent = parent.parent
    return None


def type_name(node, package, data):
    names = []
    while node is not None:
        if node.type in CLASS_LIKE_TYPES:
            names.append(text(node.child_by_field_name("name"), data))
        node = node.parent
    local = "$".join(reversed(names))
    return f"{package}.{local}" if package else local


def modifiers(node, data, *, interface=False, field=False):
    mod = next((c for c in node.named_children if c.type == "modifiers"), None)
    keywords = {"public", "protected", "private", "static", "final", "abstract", "default", "synchronized", "native"}
    values = {child.type for child in mod.children if child.type in keywords} if mod else set()
    if interface:
        if not values & {"private", "protected"}:
            values.add("public")
        if field:
            values.update({"static", "final"})
    return dict(modifiers=sorted(values), static="static" in values,
                visibility=next((v for v in ("public", "protected", "private") if v in values), "package"))


def comment(node, data):
    parts = []
    previous = node.prev_named_sibling
    boundary = node.start_byte
    while previous is not None and previous.type in {"line_comment", "block_comment"}:
        gap = data[previous.end_byte:boundary]
        if gap.strip() or parts and (previous.type != "line_comment" or gap.count(b"\n") > 1):
            break
        parts.append(text(previous, data))
        if previous.type == "block_comment":
            break
        boundary = previous.start_byte
        previous = previous.prev_named_sibling
    value = "\n".join(reversed(parts))
    value = re.sub(r"(?m)^\s*(?:/\*\*?|\*/|\*|//)\s?", "", value).replace("*/", "").strip()
    return value


def erase_type(value):
    value = re.sub(r"@[\w.]+(?:\([^)]*\))?\s*", "", value)
    while re.search(r"<[^<>]*>", value):
        value = re.sub(r"<[^<>]*>", "", value)
    return re.sub(r"\s+", "", value).replace("...", "[]")


def resolve_type(value, context, known_types):
    """Resolve only unambiguous project types; retain all other spelling as known."""
    name = erase_type(value)
    if name.endswith("[]") or "|" in name or name in context.get("type_parameters", ()):
        return None
    normalized = context.get("normalized_types")
    if normalized is None:
        normalized = {known.replace("$", "."): known for known in sorted(known_types)}
    if name in known_types:
        return name
    if name in normalized:
        return normalized[name]
    # Lexical member types shadow compilation-unit imports.
    enclosing = context["owner_type"]
    while enclosing:
        candidate = enclosing + "$" + name.replace(".", "$")
        if candidate in known_types:
            return candidate
        if enclosing.rsplit(".", 1)[-1].rsplit("$", 1)[-1] == name:
            return enclosing
        enclosing = enclosing.rsplit("$", 1)[0] if "$" in enclosing else ""
    first, _, rest = name.partition(".")
    if first in context["imports"]:
        imported = context["imports"][first] + ("." + rest if rest else "")
        return normalized.get(imported)
    candidate = (context["package"] + "." if context["package"] else "") + name
    if candidate in normalized:
        return normalized[candidate]
    matches = [normalized[p + "." + name] for p in context["wildcards"] if p + "." + name in normalized]
    return matches[0] if len(set(matches)) == 1 else None


def _variable(name, declared_type, kind, declaration, scope, context, known_types, *, start=None):
    bounds = span(scope)
    if start is not None:
        bounds["start_byte"] = start.end_byte
        bounds["start_line"] = start.end_point.row + 1
    return dict(name=name, declared_type=declared_type,
                resolved_type=resolve_type(declared_type, context, known_types),
                kind=kind, declaration=span(declaration), scope=bounds)


def _method_variables(node, body, data, context, known_types):
    if body is None:
        return []
    variables = []
    params = node.child_by_field_name("parameters")
    for param in params.named_children if params else ():
        if param.type not in {"formal_parameter", "spread_parameter"}:
            continue
        name_node = param.child_by_field_name("name")
        type_node = param.child_by_field_name("type")
        if param.type == "spread_parameter":
            declarator = next((c for c in param.named_children if c.type == "variable_declarator"), None)
            name_node = declarator.child_by_field_name("name") if declarator else name_node
            type_node = next((c for c in param.named_children if c.type not in {"modifiers", "variable_declarator"}), None)
        declared = text(type_node, data) + ("[]" if param.type == "spread_parameter" else "")
        declared += "".join(text(c, data) for c in param.named_children if c.type == "dimensions")
        if name_node:
            variables.append(_variable(text(name_node, data), declared, "parameter", param, body, context, known_types))

    def visit(current):
        # Do not expose captures or members belonging to nested/anonymous classes.
        if current.type in CLASS_LIKE_TYPES | METHOD_TYPES or current.type == "class_body":
            return
        if current.type == "local_variable_declaration":
            scope = current.parent
            while scope and scope.type not in {"block", "constructor_body", "for_statement", "switch_block", "lambda_expression"}:
                scope = scope.parent
            for declarator in current.named_children:
                if declarator.type != "variable_declarator":
                    continue
                declared = text(current.child_by_field_name("type"), data)
                declared += "".join(text(c, data) for c in declarator.named_children if c.type == "dimensions")
                variables.append(_variable(text(declarator.child_by_field_name("name"), data), declared,
                                           "local", declarator, scope or body, context, known_types,
                                           start=declarator))
        elif current.type in {"enhanced_for_statement", "catch_formal_parameter", "resource"}:
            if current.type == "enhanced_for_statement":
                scope = current.child_by_field_name("body")
                type_node = current.child_by_field_name("type")
            elif current.type == "catch_formal_parameter":
                scope = current.parent.child_by_field_name("body")
                type_node = next((c for c in current.named_children if c.type == "catch_type"), None)
            else:
                scope = current.parent.parent.child_by_field_name("body")
                type_node = current.child_by_field_name("type")
            name_node = current.child_by_field_name("name")
            if scope is not None and name_node is not None:
                variables.append(_variable(text(name_node, data), text(type_node, data), "local", current,
                                           scope, context, known_types,
                                           start=current if current.type == "resource" else None))
        for child in current.named_children:
            visit(child)
    visit(body)
    return variables


def build_repair_index(units, method_entities, known_types, *, include_tests=False):
    """Reuse preprocessing ASTs, including abstract/interface declarations."""
    from src.fault_graph.java_evidence import _method_id
    result = dict(types=[], methods=[], fields=[], inheritance=[], files=[])
    normalized_types = {known.replace("$", "."): known for known in sorted(known_types)}
    for path, rel, data, tree, descriptors, test_source in units:
        if test_source and not include_tests:
            continue
        if tree.root_node.has_error:
            result["files"].append(dict(file=rel, status="parse_error"))
            continue
        package_node = next((c for c in tree.root_node.named_children if c.type == "package_declaration"), None)
        package = text(package_node, data).removeprefix("package ").rstrip(";").strip()
        imports, wildcards, static_imports = {}, [], []
        for c in tree.root_node.named_children:
            if c.type != "import_declaration":
                continue
            value = text(c, data).removeprefix("import ").removeprefix("static ").rstrip(";").strip()
            if text(c, data).startswith("import static "):
                static_imports.append(value)
            if value.endswith(".*"):
                wildcards.append(value[:-2])
            else:
                imports[value.rsplit(".", 1)[-1]] = value
        by_start = {m.start_byte: m for m in descriptors}
        anonymous = {}
        for method_node in walk(tree.root_node):
            descriptor = by_start.get(method_node.start_byte) if method_node.type in METHOD_TYPES else None
            enclosing = owner(method_node) if descriptor else None
            if enclosing is not None and enclosing.type == "class_body":
                anonymous[enclosing.start_byte] = descriptor.qualified_class_name
        result["files"].append(dict(file=rel, status="ok"))
        for n in walk(tree.root_node):
            is_anonymous = n.type == "class_body" and n.start_byte in anonymous
            if not is_anonymous and n.type not in CLASS_LIKE_TYPES | METHOD_TYPES | {"field_declaration", "constant_declaration"}:
                continue
            own = n if n.type in CLASS_LIKE_TYPES or is_anonymous else owner(n)
            if own is None or own.type not in CLASS_LIKE_TYPES and own.start_byte not in anonymous:
                continue
            declaring_type = anonymous.get(own.start_byte) or type_name(own, package, data)
            context = dict(package=package, imports=imports, wildcards=wildcards, static_imports=static_imports,
                           owner_type=declaring_type, normalized_types=normalized_types,
                           type_parameters=[text(p.named_children[0], data) for a in (own, n)
                                            for p in (a.child_by_field_name("type_parameters").named_children
                                                      if a.child_by_field_name("type_parameters") else ()) if p.named_children])
            interface = own.type == "interface_declaration"
            common = dict(file=rel, owner_type=declaring_type, package=package, **span(n))
            if n.type in CLASS_LIKE_TYPES or is_anonymous:
                enclosing_type = owner(n)
                result["types"].append(dict(type_id=declaring_type, kind="anonymous_class" if is_anonymous else n.type,
                                            context={k: v for k, v in context.items() if k != "normalized_types"},
                                            **modifiers(n, data, interface=enclosing_type is not None and enclosing_type.type == "interface_declaration"), **common))
                if is_anonymous:
                    parent_node = n.parent.child_by_field_name("type")
                    if parent_node:
                        declared = text(parent_node, data)
                        result["inheritance"].append(dict(child=declaring_type, parent=resolve_type(declared, context, known_types),
                                                          declared_type=declared, relation="anonymous_base", file=rel))
                for field, relation in (("superclass", "extends"), ("interfaces", "implements"), ("extends_interfaces", "extends")):
                    parent_node = n.child_by_field_name(field)
                    if parent_node is None and field == "extends_interfaces":
                        parent_node = next((c for c in n.named_children if c.type == "extends_interfaces"), None)
                    if parent_node is None:
                        continue
                    children = parent_node.named_children
                    if len(children) == 1 and children[0].type == "type_list":
                        children = children[0].named_children
                    for p in children:
                        declared = text(p, data)
                        result["inheritance"].append(dict(child=declaring_type, parent=resolve_type(declared, context, known_types),
                                                          declared_type=declared, relation=relation, file=rel))
            elif n.type in METHOD_TYPES:
                descriptor = by_start.get(n.start_byte)
                method_name = text(n.child_by_field_name("name"), data)
                return_type = text(n.child_by_field_name("type"), data) or "void"
                basis = descriptor or SimpleNamespace(qualified_class_name=declaring_type, package_name=package,
                                                       is_constructor=n.type == "constructor_declaration",
                                                       return_type=return_type, method_name=method_name)
                eid = method_entities.get((path, n.start_byte))
                mid = eid.split(":", 1)[1] if eid else _method_id(basis, n, data, imports, wildcards, known_types)
                body = n.child_by_field_name("body")
                signature_end = body.start_byte if body else n.end_byte
                signature = data[n.start_byte:signature_end].decode("utf-8", errors="replace").strip().rstrip(";")
                record = dict(method_id=mid, entity_id=eid or mid, name=method_name, signature=" ".join(signature.split()),
                              return_type=return_type, comment=comment(n, data), has_body=body is not None,
                              constructor=n.type == "constructor_declaration", **modifiers(n, data, interface=interface), **common)
                params = n.child_by_field_name("parameters")
                record["parameter_types"] = []
                record["varargs"] = False
                for param in params.named_children if params else ():
                    if param.type not in {"formal_parameter", "spread_parameter"}:
                        continue
                    type_node = param.child_by_field_name("type")
                    if type_node is None:
                        type_node = next((c for c in param.named_children if c.type not in
                                          {"modifiers", "variable_declarator", "identifier", "dimensions"}), None)
                    declared = text(type_node, data)
                    declared += "".join(text(c, data) for c in param.named_children if c.type == "dimensions")
                    if param.type == "spread_parameter":
                        declared += "[]"
                        record["varargs"] = True
                    record["parameter_types"].append(declared)
                record["variables"] = _method_variables(n, body, data, context, known_types)
                if descriptor:
                    record["descriptor"] = dict(asdict(descriptor), file_path=str(descriptor.file_path))
                result["methods"].append(record)
            else:
                for declarator in n.named_children:
                    if declarator.type != "variable_declarator":
                        continue
                    name = text(declarator.child_by_field_name("name"), data)
                    declared = text(n.child_by_field_name("type"), data)
                    declared += "".join(text(c, data) for c in declarator.named_children if c.type == "dimensions")
                    result["fields"].append(dict(entity_id=f"field:{declaring_type}#{name}", name=name,
                                                 declared_type=declared, resolved_type=resolve_type(declared, context, known_types),
                                                 comment=comment(n, data), **modifiers(n, data, interface=interface, field=True), **common))
    return result


def write_repair_tables(db, index, *, changed_files=None):
    """Persist independently queryable records, in the caller's transaction."""
    import json
    db.execute("CREATE TABLE IF NOT EXISTS repair_types (type_id TEXT PRIMARY KEY, file TEXT NOT NULL, payload TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS repair_methods (method_id TEXT PRIMARY KEY, file TEXT NOT NULL, owner_type TEXT NOT NULL, payload TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS repair_variables (method_id TEXT NOT NULL, name TEXT NOT NULL, payload TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS repair_fields (entity_id TEXT PRIMARY KEY, file TEXT NOT NULL, owner_type TEXT NOT NULL, payload TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS repair_inheritance (child TEXT NOT NULL, parent TEXT, file TEXT NOT NULL, payload TEXT NOT NULL)")
    if changed_files:
        for file in changed_files:
            db.execute("DELETE FROM repair_variables WHERE method_id IN (SELECT method_id FROM repair_methods WHERE file=?)", (file,))
            for table in ("repair_types", "repair_methods", "repair_fields", "repair_inheritance"):
                db.execute(f"DELETE FROM {table} WHERE file=?", (file,))
    dump = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    db.executemany("INSERT INTO repair_types VALUES (?,?,?)", ((r["type_id"], r["file"], dump(r)) for r in index["types"]))
    for record in index["methods"]:
        r = {k: v for k, v in record.items() if k != "variables"}
        db.execute("INSERT INTO repair_methods VALUES (?,?,?,?)", (r["method_id"], r["file"], r["owner_type"], dump(r)))
        db.executemany("INSERT INTO repair_variables VALUES (?,?,?)", ((r["method_id"], v["name"], dump(v)) for v in record["variables"]))
    db.executemany("INSERT INTO repair_fields VALUES (?,?,?,?)", ((r["entity_id"], r["file"], r["owner_type"], dump(r)) for r in index["fields"]))
    db.executemany("INSERT INTO repair_inheritance VALUES (?,?,?,?)", ((r["child"], r["parent"], r["file"], dump(r)) for r in index["inheritance"]))
    for table, column in (("repair_methods", "owner_type"), ("repair_fields", "owner_type"), ("repair_variables", "method_id"), ("repair_inheritance", "child")):
        db.execute(f"CREATE INDEX IF NOT EXISTS {table}_{column} ON {table}({column})")
