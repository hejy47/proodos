"""Read-only, Markdown repair tools backed by the preprocessing SQLite index."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from src.debug.semantic_agent.tools.function_tool import function_tool
from src.fault_graph.java_repair_index import erase_type


def _cell(value):
    return str(value if value is not None else "unknown").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines.extend("| " + " | ".join(_cell(value) for value in row) + " |" for row in rows)
    return "\n".join(lines)


def _status(value, message=""):
    return f"status: {value}\n" + (f"\n{message}\n" if message else "")


class RepairIngredients:
    def __init__(self, context):
        self.graph = getattr(context, "graph", None)
        if self.graph is None:
            self.graph = getattr(getattr(context, "method_records", None), "graph", None)
        self.db = getattr(self.graph, "connection", None)
        self.index = getattr(self.graph, "repair_index", None)
        self.available = bool(self.index is not None or self.db is not None and self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='repair_methods'").fetchone())

    def _records(self, kind, **filters):
        if not self.available:
            return []
        if self.db is not None:
            where = " AND ".join(f"{key}=?" for key in filters)
            rows = self.db.execute(f"SELECT payload FROM repair_{kind}" + (f" WHERE {where}" if where else ""), tuple(filters.values()))
            return [json.loads(row[0]) for row in rows]
        if kind == "variables":
            method_id = filters.pop("method_id")
            records = next((r["variables"] for r in self.index["methods"] if r["method_id"] == method_id), [])
        else:
            records = self.index[kind]
        return [r for r in records if all(r.get(k) == v for k, v in filters.items())]

    def _method(self, method_id):
        return next(iter(self._records("methods", method_id=method_id.removeprefix("func:"))), None)

    def _type(self, type_id):
        return next(iter(self._records("types", type_id=type_id)), None)

    def _lineage(self, type_id):
        pending, seen, result = [type_id], set(), []
        while pending:
            current = pending.pop(0)
            if current in seen:
                continue
            seen.add(current)
            result.append(current)
            pending.extend(r["parent"] for r in self._records("inheritance", child=current) if r["parent"])
        return result

    def _type_accessible(self, record, method):
        current = method["owner_type"]
        type_id = record["type_id"]
        if record["kind"] == "anonymous_class":
            return current == type_id
        visibility = record["visibility"]
        if visibility == "private" and type_id.split("$", 1)[0] != current.split("$", 1)[0]:
            return False
        if visibility in {"package", "protected"} and record["package"] != method["package"]:
            if visibility != "protected" or "$" not in type_id or type_id.rsplit("$", 1)[0] not in self._lineage(current):
                return False
        enclosing = self._type(type_id.rsplit("$", 1)[0]) if "$" in type_id else None
        return enclosing is None or self._type_accessible(enclosing, method)

    def _visible(self, record, method, *, receiver_type=None):
        visibility = record["visibility"]
        declaring = record["owner_type"]
        current = method["owner_type"]
        if visibility == "public" or declaring == current:
            return True
        if visibility == "private":
            return declaring.split("$", 1)[0] == current.split("$", 1)[0]
        if record["package"] == method["package"]:
            return True
        if visibility == "protected" and declaring in self._lineage(current):
            # Across packages a protected instance member requires a subtype receiver.
            return record["static"] or receiver_type is None or current in self._lineage(receiver_type)
        return False

    def _inherited(self, record, receiver_type):
        """Private members and package members lost across packages are not inherited."""
        if record["owner_type"] == receiver_type:
            return True
        if record["visibility"] == "private":
            return False
        if record["visibility"] != "package":
            return True
        pending, seen = [receiver_type], set()
        while pending:
            current = pending.pop(0)
            if current in seen:
                continue
            seen.add(current)
            type_record = self._type(current)
            if not type_record or type_record["package"] != record["package"]:
                continue
            if current == record["owner_type"]:
                return True
            pending.extend(r["parent"] for r in self._records("inheritance", child=current) if r["parent"])
        return False

    def _variables(self, method, source_line=None):
        variables = []
        for record in self._records("variables", method_id=method["method_id"]):
            if source_line is None or record["scope"]["start_line"] <= source_line <= record["scope"]["end_line"]:
                variables.append(dict(record, receiver=record["name"], inherited=False))
        seen_fields = set()
        for type_id in self._lineage(method["owner_type"]):
            for field in self._records("fields", owner_type=type_id):
                if field["name"] in seen_fields:
                    continue
                # A hidden private field also hides a same-named ancestor declaration.
                seen_fields.add(field["name"])
                if (method["static"] and not field["static"] or not self._visible(field, method)
                        or not self._inherited(field, method["owner_type"])):
                    continue
                inherited = field["owner_type"] != method["owner_type"]
                receiver = f"{method['owner_type'].replace('$', '.')}.{field['name']}" if field["static"] else f"this.{field['name']}"
                declaring_type = self._type(method["owner_type"])
                if field["static"] and declaring_type and declaring_type["kind"] == "anonymous_class":
                    receiver = field["name"] if method["static"] else f"this.{field['name']}"
                variables.append(dict(field, kind="static_field" if field["static"] else "instance_field",
                                      receiver=receiver, inherited=inherited, scope=None))
        return variables

    def list_accessible_variables(self, method_id: str, source_line: int | None = None) -> str:
        """List project variables and their lexical scopes; an optional line filters visibility."""
        if not self.available:
            return _status("index_unavailable", "Run Java preprocessing to build the repair ingredients index.")
        method = self._method(method_id)
        if method is None:
            return _status("method_unavailable", "Use an exact indexed production method ID.")
        if source_line is not None and not method["start_line"] <= source_line <= method["end_line"]:
            return _status("invalid_position", "source_line must be an absolute line inside the target method.")
        rows = []
        for v in self._variables(method, source_line):
            scope = v["scope"]
            bounds = (f"lines {scope['start_line']}–{scope['end_line']}; bytes [{scope['start_byte']}, {scope['end_byte']})"
                      if scope else "class member")
            rows.append((v["receiver"], v["kind"], v["declared_type"], v["resolved_type"], bounds,
                         v["entity_id"] if "entity_id" in v else f"{v['name']}@{v['declaration']['start_line']}"))
        return (_status("ok") + f"\nmethod: `{method['method_id']}`\n\n"
                "Scope is lexical, not definite-assignment proof. Without source_line these are method-level candidates; "
                "observe declaration scopes when inserting statements. Fields use explicit receivers to avoid shadowing.\n\n"
                + _table(["Variable / receiver", "Kind", "Declared type", "Project type", "Scope", "Code ID / selector"], rows))

    def list_callable_methods(self, method_id: str, variable_name: str, query: str | None = None, limit: int = 20) -> str:
        """List project methods on a variable's static type. Use this.field, a class name for static calls, or name@declaration_line to disambiguate. Query matches name, return type or comment; no pagination."""
        if not self.available:
            return _status("index_unavailable")
        if not 1 <= limit <= 100:
            return _status("invalid_limit", "limit must be between 1 and 100.")
        method = self._method(method_id)
        if method is None:
            return _status("method_unavailable")
        candidates = self._variables(method)
        matches = [v for v in candidates if v["receiver"] == variable_name]
        if not matches:
            matches = [v for v in candidates if v["name"] == variable_name and v["kind"] in {"parameter", "local"}]
        if not matches:
            matches = [v for v in candidates if v["name"] == variable_name]
        if "@" in variable_name:
            name, line = variable_name.rsplit("@", 1)
            matches = [v for v in candidates if v["name"] == name and str(v.get("declaration", {}).get("start_line")) == line]
        static_only = False
        if matches:
            types = {(v["declared_type"], v["resolved_type"]) for v in matches}
            if len(types) != 1:
                return _status("ambiguous_variable", "Several lexical declarations match. Use name@declaration_line or an explicit field receiver.")
            declared, resolved = next(iter(types))
        elif variable_name == "this" and not method["static"]:
            declared = resolved = method["owner_type"]
        else:
            types = [r for r in self._records("types") if (r["type_id"] == variable_name or r["type_id"].replace("$", ".") == variable_name or
                     r["type_id"].rsplit(".", 1)[-1].replace("$", ".") == variable_name or
                     r["type_id"].rsplit(".", 1)[-1].rsplit("$", 1)[-1] == variable_name)
                     and self._type_accessible(r, method)]
            if len(types) != 1:
                return _status("variable_unavailable", "Select a listed variable, this receiver, or an unambiguous indexed project class.")
            declared = resolved = types[0]["type_id"]
            static_only = True
        if not resolved or self._type(resolved) is None:
            return _status("type_known_methods_unavailable", f"Declared type: `{declared}`. Only project source methods are indexed. "
                           "JDK/dependency types, arrays, type variables and unresolved types have no method directory here; "
                           "this does not mean they have no callable methods.")
        if not self._type_accessible(self._type(resolved), method):
            return _status("type_known_methods_unavailable", f"Project type `{resolved}` is inaccessible from the target method.")
        collected, seen = [], set()
        for owner_type in self._lineage(resolved):
            for record in self._records("methods", owner_type=owner_type):
                if record["constructor"] or static_only and not record["static"]:
                    continue
                if not self._inherited(record, resolved):
                    continue
                owner_record = self._type(owner_type)
                if record["static"] and owner_record and owner_record["kind"] == "interface_declaration":
                    if owner_type != resolved or not static_only:
                        continue
                if not self._visible(record, method, receiver_type=resolved):
                    continue
                params = record["method_id"].split("#", 1)[-1].split(")", 1)[0]
                if params in seen:
                    continue
                seen.add(params)
                if query and query.casefold() not in " ".join((record["name"], record["return_type"], record["comment"])).casefold():
                    continue
                collected.append(dict(record, inherited=owner_type != resolved))
        collected.sort(key=lambda r: (r["inherited"], r["name"], r["method_id"]))
        rows = [(r["method_id"], r["signature"], r["owner_type"], r["static"], r["inherited"],
                 "yes", r["comment"][:300] + ("…" if len(r["comment"]) > 300 else "")) for r in collected[:limit]]
        unresolved = [r["declared_type"] for t in self._lineage(resolved) for r in self._records("inheritance", child=t) if not r["parent"]]
        return (_status("ok") + f"\ndeclared_type: `{declared}`\nproject_type: `{resolved}`\n"
                f"total: {len(collected)}\ntruncated: {str(len(collected) > limit).lower()}\n"
                + ("Refine query or increase limit to see omitted methods.\n" if len(collected) > limit else "")
                + (f"Unindexed parent types: {', '.join(unresolved)}. Their methods are unknown.\n" if unresolved else "")
                + "Runtime overrides are not inferred. Generic signatures retain their declaration spelling.\n\n"
                + _table(["Code ID", "Signature", "Declaring type", "Static", "Inherited", "Has source", "Comment"], rows))

    def read_code(self, entity_id: str) -> str:
        """Read the complete current project method/field by indexed ID and verified source span."""
        if not self.available:
            return _status("index_unavailable")
        record = self._method(entity_id)
        if record is None:
            record = next(iter(self._records("fields", entity_id=entity_id)), None)
        if record is None:
            return _status("source_unavailable", "Use a method or field Code ID returned by the ingredients tools.")
        file = record["file"]
        root = Path(self.graph.metadata["source_root"]).resolve()
        path = (root / file).resolve()
        try:
            if not path.is_relative_to(root):
                raise ValueError("Indexed path is outside the project")
            if self.db is not None:
                expected = self.db.execute("SELECT sha256 FROM source_files WHERE path=?", (file,)).fetchone()
                expected = expected[0] if expected else None
            else:
                expected = self.graph.metadata.get("source_sha256", {}).get(file)
            data = path.read_bytes()
            if expected is None or hashlib.sha256(data).hexdigest() != expected:
                raise ValueError("Source hash differs from the preprocessing index")
            start, end = record["start_byte"], record["end_byte"]
            if not 0 <= start < end <= len(data):
                raise ValueError("Indexed source span is invalid")
            encoding = self.graph.metadata.get("source_encodings", {}).get(file, "utf-8")
            code = data[start:end].decode(encoding)
        except (OSError, ValueError, LookupError) as exc:
            return _status("source_unavailable", str(exc))
        fence = "`" * max(3, max((len(part) for part in re.findall(r"`+", code)), default=0) + 1)
        return (_status("ok") + f"\nentity: `{entity_id}`\nlocation: `{file}:{record['start_line']}–{record['end_line']}`\n\n"
                + (f"Comment:\n{record['comment']}\n\n" if record.get("comment") else "")
                + f"{fence}java\n{code}\n{fence}\n")

    def tools(self):
        return [function_tool(self.list_accessible_variables), function_tool(self.list_callable_methods), function_tool(self.read_code)]
