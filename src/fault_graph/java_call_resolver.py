"""Resolve project Java calls using lexical types and the class hierarchy.

Tree-sitter supplies syntax; declarations supply receiver types. An unknown
receiver never falls back to every project method with the same name.
"""
from __future__ import annotations

from collections import defaultdict, deque
import json
from pathlib import Path

from src.fault_graph.java_repair_index import build_repair_index, erase_type, resolve_type, text, type_name, walk
from src.utils.java_source import CLASS_LIKE_TYPES, JavaMethodDescriptor, _extract_package_name, _java_parser


def source_symbols(methods, source_files=()):
    """Support the standalone call-graph API without an existing source index."""
    by_file = defaultdict(list)
    for path in source_files:
        by_file[path] = []
    for method in methods:
        by_file[method.file_path].append(method)
    units = [(path, str(path), data, _java_parser().parse(data), descriptors, False)
             for path, descriptors in by_file.items() for data in (path.read_bytes(),)]
    known = {type_name(node, _extract_package_name(tree.root_node, data), data)
             for _, _, data, tree, _, _ in units for node in walk(tree.root_node) if node.type in CLASS_LIKE_TYPES}
    return build_repair_index(units, {}, known)


def symbol_descriptors(symbols):
    for record in symbols["methods"]:
        if "descriptor" in record:
            values = dict(record["descriptor"])
            values["file_path"] = Path(values["file_path"])
            values["enclosing_classes"] = tuple(values["enclosing_classes"])
            yield JavaMethodDescriptor(**values), record["entity_id"]


def write_call_symbols(db, symbols, *, changed_files=None):
    """Keep test declarations available to incremental call resolution too.

    Repair tools still use their production-only tables. This compact catalog
    contains declarations/scopes, not source bodies or comments.
    """
    db.execute("CREATE TABLE IF NOT EXISTS java_call_symbols (file TEXT PRIMARY KEY, payload TEXT NOT NULL)")
    if changed_files:
        db.executemany("DELETE FROM java_call_symbols WHERE file=?", ((f,) for f in changed_files))
    by_file = {}
    for kind in ("types", "methods", "fields", "inheritance"):
        for record in symbols[kind]:
            unit = by_file.setdefault(record["file"], dict(types=[], methods=[], fields=[], inheritance=[]))
            unit[kind].append({k: v for k, v in record.items() if k not in
                               {"comment", "signature", "start_line", "end_line", "end_byte"}})
    db.executemany("INSERT INTO java_call_symbols VALUES (?,?)",
                   ((file, json.dumps(unit, ensure_ascii=False, separators=(",", ":")))
                    for file, unit in by_file.items()))


def read_call_symbols(db, *, excluded_files=()):
    result = dict(types=[], methods=[], fields=[], inheritance=[])
    for file, raw in db.execute("SELECT file,payload FROM java_call_symbols"):
        if file in excluded_files:
            continue
        for kind, records in json.loads(raw).items():
            result[kind].extend(records)
    return result


class JavaCallResolver:
    def __init__(self, methods, symbols=None):
        symbols = symbols if symbols is not None else source_symbols(methods)
        self.types = {r["type_id"]: r for r in symbols["types"]}
        self.known_types = set(self.types)
        self.normalized_types = {t.replace("$", "."): t for t in self.types}
        self.parents = defaultdict(list)
        self.children = defaultdict(list)
        for edge in symbols["inheritance"]:
            if edge["parent"]:
                self.parents[edge["child"]].append(edge["parent"])
                self.children[edge["parent"]].append(edge["child"])
        self.methods = defaultdict(list)
        self.fields = defaultdict(dict)
        self.records = {}
        self.descriptors = {(m.file_path, m.start_byte): m for m in methods}
        for record in symbols["methods"]:
            name = "<init>" if record["constructor"] else record["name"]
            self.methods[record["owner_type"], name].append(record)
            descriptor = record.get("descriptor")
            if descriptor:
                self.records[Path(descriptor["file_path"]), descriptor["start_byte"]] = record
        for record in symbols["fields"]:
            self.fields[record["owner_type"]][record["name"]] = record
        self._lineages = {}
        self._descendants = {}
        self._lookups = {}
        self._contexts = {}
        self._resolved_types = {}

    def _hierarchy(self, owner, *, descendants=False):
        cache = self._descendants if descendants else self._lineages
        if owner not in cache:
            edges = self.children if descendants else self.parents
            pending, seen, result = deque([owner]), set(), []
            while pending:
                current = pending.popleft()
                if current in seen:
                    continue
                seen.add(current)
                result.append(current)
                pending.extend(edges[current])
            cache[owner] = result
        return cache[owner]

    def _context(self, owner):
        if owner in self._contexts:
            return self._contexts[owner]
        context = dict(self.types.get(owner, {}).get("context", {}))
        context.update(owner_type=owner, normalized_types=self.normalized_types)
        context.setdefault("imports", {})
        context.setdefault("wildcards", [])
        context.setdefault("package", owner.rpartition(".")[0])
        self._contexts[owner] = context
        return context

    def _type(self, declared, owner):
        if not declared:
            return None
        key = (declared, owner)
        if key in self._resolved_types:
            return self._resolved_types[key]
        name = erase_type(declared)
        dimensions = len(name) - len(name.rstrip("[]"))
        base = name[:-dimensions] if dimensions else name
        context = self._context(owner)
        if base in context.get("type_parameters", ()):
            return None
        resolved = resolve_type(base, context, self.known_types)
        if not resolved:
            first, _, rest = base.partition(".")
            resolved = context["imports"].get(first, first) + ("." + rest if rest else "")
        result = resolved + "[]" * (dimensions // 2)
        self._resolved_types[key] = result
        return result

    def _lookup(self, owner, name, arity, *, constructor=False):
        key = (owner, name, arity, constructor)
        if key not in self._lookups:
            result, seen = [], set()
            lineage = [owner] if constructor else sorted(
                self._hierarchy(owner), key=lambda t: self.types.get(t, {}).get("kind") == "interface_declaration"
            )
            for current in lineage:
                for record in self.methods[current, name]:
                    params = record["parameter_types"]
                    if len(params) != arity and not (record["varargs"] and arity >= len(params) - 1):
                        continue
                    if current != owner and record["visibility"] == "private":
                        continue
                    if current != owner and record["visibility"] == "package" and any(
                        self.types.get(t, {}).get("package") != record["package"]
                        for t in self._hierarchy(owner) if current in self._hierarchy(t)
                    ):
                        continue
                    signature = tuple(self._type(p, current) for p in params)
                    if signature not in seen:
                        seen.add(signature)
                        result.append(record)
            self._lookups[key] = result
        return self._lookups[key]

    def _field(self, owner, name):
        for current in self._hierarchy(owner):
            record = self.fields[current].get(name)
            if record:
                if current != owner and record["visibility"] == "private":
                    return None
                if current != owner and record["visibility"] == "package" and record["package"] != self.types.get(owner, {}).get("package"):
                    return None
                return self._type(record["declared_type"], current)
        return None

    def _identifier(self, name, caller, position):
        record = self.records.get((caller.file_path, caller.start_byte), {})
        variables = [v for v in record.get("variables", ()) if v["name"] == name
                     and v["scope"]["start_byte"] <= position < v["scope"]["end_byte"]]
        if variables:
            variable = max(variables, key=lambda v: (v["scope"]["start_byte"], v["declaration"]["start_byte"]))
            return self._type(variable["declared_type"], caller.qualified_class_name), False
        owner = caller.qualified_class_name
        while owner:
            declared = self._field(owner, name)
            if declared:
                return declared, False
            if "$" not in owner:
                break
            owner = owner.rsplit("$", 1)[0]
        return self._type(name, caller.qualified_class_name), True

    def _expression_type(self, node, data, caller, depth=0):
        if node is None or depth > 8:
            return None, False
        owner = caller.qualified_class_name
        value = text(node, data)
        if node.type == "identifier":
            return self._identifier(value, caller, node.start_byte)
        if node.type == "this":
            return owner, False
        if node.type == "super":
            return next(iter(self.parents[owner]), None), False
        if node.type in {"object_creation_expression", "cast_expression"}:
            return self._type(text(node.child_by_field_name("type"), data), owner), False
        if node.type == "parenthesized_expression":
            return self._expression_type(node.named_children[0], data, caller, depth + 1)
        if node.type == "array_access":
            array, _ = self._expression_type(node.child_by_field_name("array"), data, caller, depth + 1)
            return (array[:-2] if array and array.endswith("[]") else None), False
        if node.type in {"field_access", "scoped_identifier"}:
            obj = node.child_by_field_name("object") or node.child_by_field_name("scope")
            field = node.child_by_field_name("field") or node.child_by_field_name("name")
            receiver, _ = self._expression_type(obj, data, caller, depth + 1)
            if receiver in self.types:
                return self._field(receiver, text(field, data)), False
            # A qualified type name can also be represented as field_access.
            resolved = resolve_type(value, self._context(owner), self.known_types)
            return resolved, resolved is not None
        if node.type == "method_invocation":
            records = self._invocation(node, data, caller, depth + 1)
            types = {self._type(r["return_type"], r["owner_type"]) for r in records}
            return (next(iter(types)) if len(types) == 1 else None), False
        if node.type == "string_literal":
            return "java.lang.String", False
        if node.type == "character_literal":
            return "char", False
        if node.type in {"true", "false"}:
            return "boolean", False
        if node.type.endswith("integer_literal"):
            return ("long" if value.lower().endswith("l") else "int"), False
        if node.type.endswith("floating_point_literal"):
            return ("float" if value.lower().endswith("f") else "double"), False
        if node.type == "null_literal":
            return "<null>", False
        return None, False

    def _overloads(self, records, arguments, data, caller, depth):
        if len(records) < 2:
            return records
        actual = [self._expression_type(a, data, caller, depth + 1)[0] for a in arguments]
        scored = []
        for record in records:
            params = [self._type(p, record["owner_type"]) for p in record["parameter_types"]]
            score = 0
            for i, value in enumerate(actual):
                expected = params[min(i, len(params) - 1)] if params else None
                if record["varargs"] and i >= len(params) - 1 and expected:
                    expected = expected[:-2]
                if value and expected:
                    if value == expected or value == "java.lang." + expected:
                        score += 2
                    elif value in self.types and expected in self._hierarchy(value):
                        score += 1
            scored.append((score, record))
        best = max(score for score, _ in scored)
        return [r for score, r in scored if score == best]

    def _dispatch(self, owner, name, arity, arguments, data, caller, *, virtual, static_only=False, depth=0):
        if owner not in self.types:
            return []
        base = self._lookup(owner, name, arity, constructor=name == "<init>")
        if static_only:
            base = [r for r in base if r["static"]]
        base = self._overloads(base, arguments, data, caller, depth)
        result = list(base)
        if virtual and name != "<init>":
            virtual_signatures = {tuple(self._type(p, r["owner_type"]) for p in r["parameter_types"])
                                  for r in base if not r["static"] and r["visibility"] != "private" and "final" not in r["modifiers"]}
            if virtual_signatures:
                for child in self._hierarchy(owner, descendants=True)[1:]:
                    result.extend(r for r in self._lookup(child, name, arity)
                                  if not r["static"] and r["visibility"] != "private"
                                  and tuple(self._type(p, r["owner_type"]) for p in r["parameter_types"]) in virtual_signatures)
        return result

    def _invocation(self, node, data, caller, depth=0):
        name = text(node.child_by_field_name("name"), data)
        args = node.child_by_field_name("arguments")
        arguments = args.named_children if args else []
        obj = node.child_by_field_name("object")
        if obj is not None:
            owner, static = self._expression_type(obj, data, caller, depth + 1)
            return self._dispatch(owner, name, len(arguments), arguments, data, caller,
                                  virtual=not static and obj.type != "super", static_only=static, depth=depth)
        owner = caller.qualified_class_name
        while owner:
            records = self._dispatch(owner, name, len(arguments), arguments, data, caller, virtual=True, depth=depth)
            if records:
                return records
            owner = owner.rsplit("$", 1)[0] if "$" in owner else ""
        records = []
        for imported in self._context(caller.qualified_class_name).get("static_imports", ()):
            qualifier, _, member = imported.rpartition(".")
            if member in {name, "*"}:
                target = resolve_type(qualifier, self._context(caller.qualified_class_name), self.known_types)
                records.extend(self._dispatch(target, name, len(arguments), arguments, data, caller,
                                               virtual=False, static_only=True, depth=depth))
        return records

    def resolve(self, site, *, enclosing):
        if site.node.type == "method_invocation":
            records = self._invocation(site.node, site.source_bytes, enclosing)
        else:
            owner = enclosing.qualified_class_name
            if site.receiver_type_hint == "this":
                target = owner
            elif site.receiver_type_hint == "super":
                target = next(iter(self.parents[owner]), None)
            else:
                target = resolve_type(site.receiver_type_hint or "", self._context(owner), self.known_types)
            args = site.node.child_by_field_name("arguments")
            records = self._dispatch(target, "<init>", site.arity, args.named_children if args else [],
                                      site.source_bytes, enclosing, virtual=False)
        result = {}
        for record in records:
            descriptor = record.get("descriptor")
            if descriptor:
                key = (Path(descriptor["file_path"]), descriptor["start_byte"])
                if key in self.descriptors:
                    result[key] = self.descriptors[key]
        return list(result.values())
