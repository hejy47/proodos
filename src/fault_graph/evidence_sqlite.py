"""Disk-backed fault-context queries for case-scoped source indexes.

Schema 3 references verified project files and keeps byte spans on source entities.
Long public IDs remain part of the API, while relations use integer node IDs.
Earlier graph schemas remain readable for existing databases.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from src.fault_graph.evidence_graph import EvidenceGraph

_REPO_ROOT = Path(__file__).resolve().parents[2]


def resolve_indexed_source_root(stored: str | None) -> Path:
    """Map a preprocess-time source root onto the current checkout.

    Indexing inside the dataset container stores ``/data/...``. Debugging may
    run on the host, where that path is missing but ``COHIKER_LINUX_DIR`` or the
    repo-mounted tree is present.
    """
    text = str(stored or "").strip()
    stored_path = Path(text) if text else Path()
    if text and stored_path.is_dir():
        return stored_path.resolve()
    env = os.environ.get("COHIKER_LINUX_DIR", "").strip()
    if env:
        env_path = Path(env)
        if env_path.is_dir():
            return env_path.resolve()
    if text.startswith("/data/"):
        candidate = _REPO_ROOT / text[len("/data/"):]
        if candidate.is_dir():
            return candidate.resolve()
    return stored_path.resolve() if text else stored_path


class _Records(Mapping):
    def __init__(self, connection, table, key, value, loader=None):
        self.connection, self.table, self.key, self.value = connection, table, key, value
        self.loader = loader

    def __getitem__(self, key):
        if self.loader is not None:
            return self.loader(key)
        row = self.connection.execute(
            f"SELECT {self.value} FROM {self.table} WHERE {self.key} = ?", (key,)
        ).fetchone()
        if row is None:
            raise KeyError(key)
        return json.loads(row[0]) if self.value == "payload" else row[0]

    def __iter__(self):
        for row in self.connection.execute(f"SELECT {self.key} FROM {self.table}"):
            yield row[0]

    def __len__(self):
        return self.connection.execute(f"SELECT count(*) FROM {self.table}").fetchone()[0]


def _compact_entity(entity: dict) -> tuple[str, str | None, int | None, int | None]:
    content = dict(entity.get("content", {}))
    span = content.get("source_span")
    file_name = start_byte = end_byte = None
    provenance = entity.get("provenance", {})
    if (isinstance(span, dict) and set(span) == {"file", "start_byte", "end_byte"}
            and isinstance(span["file"], str) and isinstance(span["start_byte"], int)
            and isinstance(span["end_byte"], int)):
        file_name = span["file"]
        start_byte, end_byte = span["start_byte"], span["end_byte"]
        # Null placeholders preserve content key order for paginated readers.
        if "source_code" in content:
            content["source_code"] = None
        content["source_span"] = None
        location = content.get("location")
        if isinstance(location, dict) and location.get("file") == file_name:
            if provenance == dict(extractor="tree_sitter_c", source=file_name, location=location):
                provenance = None  # Fully recoverable from source file and location.
                content["location"] = dict(location, file=None)
    for key in ("method_id", "operation_kind"):
        if key in content:
            content[key] = None  # Values are stored in indexed columns.
    attributes = {key: value for key, value in entity.items()
                  if key not in {"entity_id", "entity_type", "name", "content", "provenance"}}
    return json.dumps(dict(content=content, provenance=provenance,
                           attributes=attributes), ensure_ascii=False, separators=(",", ":")), \
        file_name, start_byte, end_byte


def _compact_relation(edge: dict) -> str:
    payload = {key: value for key, value in edge.items()
               if key not in {"relation_id", "source", "target", "relation_type"}}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def save_sqlite(graph: EvidenceGraph, path: Path) -> None:
    """Write the compact schema atomically, retaining every logical record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".sqlite", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        with closing(sqlite3.connect(temporary)) as db, db:
            db.execute("PRAGMA page_size=32768")
            db.execute("PRAGMA journal_mode=OFF")
            db.executescript("""
                CREATE TABLE metadata (payload TEXT NOT NULL);
                CREATE TABLE source_files (
                    file_id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE,
                    sha256 TEXT NOT NULL);
                CREATE TABLE entities (
                    node_id INTEGER PRIMARY KEY, public_id TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL, name TEXT NOT NULL, method_id TEXT,
                    operation_kind TEXT, observed INTEGER NOT NULL,
                    file_id INTEGER, start_byte INTEGER, end_byte INTEGER,
                    payload TEXT NOT NULL);
                CREATE TABLE aliases (method_id TEXT PRIMARY KEY, entity_id TEXT NOT NULL);
                CREATE TABLE relations (
                    edge_id TEXT PRIMARY KEY, source_node INTEGER NOT NULL,
                    target_node INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
            """)
            db.execute("INSERT INTO metadata VALUES (?)", (json.dumps(dict(
                schema_version=3, case_id=graph.case_id, metadata=graph.metadata),
                ensure_ascii=False, separators=(",", ":")),))

            source_root = Path(graph.metadata.get("source_root", ""))
            source_ids, node_ids = {}, {}

            def entity_rows():
                # Stream rows instead of duplicating millions of entity payloads in memory.
                cached_file, data = None, b""
                for node_id, (eid, entity) in enumerate(graph.entities.items(), 1):
                    node_ids[eid] = node_id
                    compact, file_name, start, end = _compact_entity(entity)
                    file_id = None
                    content = entity.get("content", {})
                    if file_name is not None:
                        if cached_file != file_name:
                            source_path = (source_root / file_name).resolve()
                            if not source_path.is_relative_to(source_root.resolve()):
                                raise ValueError(f"Source span escapes source root: {file_name}")
                            data = source_path.read_bytes()
                            cached_file = file_name
                        if not 0 <= start <= end <= len(data):
                            raise ValueError(f"Source changed since indexing: {eid}")
                        source_code = content.get("source_code")
                        if source_code is not None and data[start:end].decode("utf-8", errors="replace") != source_code:
                            raise ValueError(f"Source changed since indexing: {eid}")
                        if file_name not in source_ids:
                            digest = hashlib.sha256(data).hexdigest()
                            expected = graph.metadata.get("source_sha256", {}).get(file_name)
                            if expected and digest != expected:
                                raise ValueError(f"Source changed since indexing: {file_name}")
                            source_ids[file_name] = len(source_ids) + 1
                            db.execute("INSERT INTO source_files VALUES (?, ?, ?)",
                                       (source_ids[file_name], file_name, digest))
                        file_id = source_ids[file_name]
                    yield (node_id, eid, entity["entity_type"], str(entity.get("name", "")),
                           content.get("method_id"), content.get("operation_kind"),
                           int(entity.get("coverage", {}).get("status") == "observed"),
                           file_id, start, end, compact)

            db.executemany("INSERT INTO entities VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", entity_rows())
            db.executemany("INSERT INTO aliases VALUES (?, ?)", graph.aliases.items())
            db.executemany("INSERT INTO relations VALUES (?, ?, ?, ?, ?)", (
                (rid, node_ids[edge["source"]], node_ids[edge["target"]],
                 edge["relation_type"], _compact_relation(edge))
                for rid, edge in graph.relations.items()))
            db.executescript("""
                CREATE INDEX entity_kind ON entities(kind, operation_kind);
                CREATE INDEX entity_method ON entities(method_id) WHERE method_id IS NOT NULL;
                CREATE INDEX relation_source ON relations(source_node, kind);
                CREATE INDEX relation_target ON relations(target_node, kind);
            """)
            build_search_index(db, graph._documents, node_ids)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def build_search_index(connection: sqlite3.Connection, documents: dict[str, str] | None = None,
                       node_ids: dict[str, int] | None = None) -> None:
    """Build a contentless FTS index; full project source stays in the checkout."""
    connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS entity_search USING fts5(document, content='')")
    if documents is not None and node_ids is not None:
        connection.executemany("INSERT INTO entity_search(rowid, document) VALUES (?, ?)",
                               ((node_ids[eid], document) for eid, document in documents.items()))
    else:
        connection.execute("INSERT INTO entity_search(entity_search) VALUES ('rebuild')")
    connection.commit()


class SQLiteEvidenceGraph(EvidenceGraph):
    def __init__(self, path: Path):
        self.connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True,
                                          check_same_thread=False)
        payload = json.loads(self.connection.execute("SELECT payload FROM metadata").fetchone()[0])
        self._schema_version = payload.get("schema_version", 1)
        if self._schema_version not in {1, 2, 3}:
            raise ValueError("Unsupported SQLite evidence schema")
        metadata = dict(payload.get("metadata") or {})
        if metadata.get("source_root"):
            metadata["source_root"] = str(resolve_indexed_source_root(metadata["source_root"]))
        super().__init__(payload["case_id"], metadata=metadata)
        if self._schema_version == 1:
            self.entities = _Records(self.connection, "entities", "id", "payload")
            self.aliases = _Records(self.connection, "aliases", "method_id", "entity_id")
            self.relations = _Records(self.connection, "relations", "id", "payload")
        else:
            self.entities = _Records(self.connection, "entities", "public_id", "payload", self._load_entity)
            self.aliases = _Records(self.connection, "aliases", "method_id", "entity_id")
            self.relations = _Records(self.connection, "relations", "edge_id", "payload", self._load_relation)
        self._has_search_index = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'entity_search'").fetchone() is not None
        self._source_cache: dict = {}
        self._entity_cache: dict[str, dict] = {}

    def function_id(self, entity_id: str) -> str | None:
        """Resolve a catalog function without opening the checkout."""
        key = str(entity_id or "")
        if not key or self._schema_version == 1:
            return super().function_id(entity_id)
        if self.connection.execute(
            "SELECT 1 FROM aliases WHERE method_id = ?", (key,)
        ).fetchone():
            return key
        row = self.connection.execute(
            "SELECT method_id FROM entities WHERE public_id = ? AND kind = 'function'",
            (key,),
        ).fetchone()
        return row[0] if row and row[0] else None

    def _load_entity(self, entity_id: str) -> dict:
        if self._schema_version != 3 and entity_id in self._entity_cache:
            return self._entity_cache[entity_id]
        row = self.connection.execute(
            "SELECT node_id, public_id, kind, name, method_id, operation_kind, file_id, "
            "start_byte, end_byte, payload FROM entities WHERE public_id = ?", (entity_id,)).fetchone()
        if row is None:
            raise KeyError(entity_id)
        _, eid, kind, name, method_id, operation_kind, file_id, start, end, raw = row
        if raw is None:
            raise KeyError(entity_id)
        payload = json.loads(raw)
        if self._schema_version == 3:
            content = payload["content"]
            if "method_id" in content:
                content["method_id"] = method_id
            if "operation_kind" in content:
                content["operation_kind"] = operation_kind
            if file_id is not None:
                file_name, data = self._read_project_source(file_id)
                content["source_code"] = data[start:end].decode(
                    self.metadata.get("source_encodings", {}).get(file_name, "utf-8"), errors="replace")
                content["source_span"] = dict(file=file_name, start_byte=start, end_byte=end)
                if payload["provenance"] is None:
                    content["location"]["file"] = file_name
                    payload["provenance"] = dict(extractor="tree_sitter_c", source=file_name,
                                                 location=dict(content["location"]))
            entity = dict(entity_id=eid, entity_type=kind, name=name, content=content,
                          provenance=payload["provenance"], **payload["attributes"])
            if len(self._entity_cache) >= 256:
                self._entity_cache.pop(next(iter(self._entity_cache)))
            self._entity_cache[entity_id] = entity
            return entity
        stored_content = payload.get("content", {})
        # Keep the stable ordering used by the in-memory graph when tools
        # serialize content (method_id/source_code precede location metadata).
        content = {}
        if method_id is not None: content["method_id"] = method_id
        if operation_kind is not None: content["operation_kind"] = operation_kind
        if file_id is not None and start is not None and end is not None:
            if file_id not in self._source_cache:
                self._source_cache[file_id] = self.connection.execute(
                    "SELECT source FROM source_files WHERE file_id = ?", (file_id,)).fetchone()[0]
            content["source_code"] = self._source_cache[file_id][start:end].decode("utf-8", errors="replace")
        content.update(stored_content)
        entity = dict(entity_id=eid, entity_type=kind, name=name, content=content,
                      provenance=payload.get("provenance", {}), **payload.get("attributes", {}))
        self._entity_cache[entity_id] = entity
        return entity

    def _read_project_source(self, file_id: int) -> tuple[str, bytes]:
        file_name, expected = self.connection.execute(
            "SELECT path, sha256 FROM source_files WHERE file_id = ?", (file_id,)).fetchone()
        root = Path(self.metadata["source_root"]).resolve()
        path = (root / file_name).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"Source path escapes project directory: {file_name}")
        try:
            stat = path.stat()
            signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            cached = self._source_cache.get(file_id)
            if cached is not None and cached[0] == signature:
                return file_name, cached[1]
            data = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"Source file unavailable: {path}. Restore the indexed source checkout.") from exc
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError(f"Source version mismatch: {path}. Restore the indexed source checkout.")
        if len(self._source_cache) >= 16:
            self._source_cache.pop(next(iter(self._source_cache)))
        self._source_cache[file_id] = (signature, data)
        return file_name, data

    def _load_relation(self, relation_id: str) -> dict:
        row = self.connection.execute(
            "SELECT edge_id, source_node, target_node, kind, payload FROM relations WHERE edge_id = ?",
            (relation_id,)).fetchone()
        if row is None: raise KeyError(relation_id)
        rid, source_node, target_node, kind, raw = row
        source = self.connection.execute("SELECT public_id FROM entities WHERE node_id = ?", (source_node,)).fetchone()[0]
        target = self.connection.execute("SELECT public_id FROM entities WHERE node_id = ?", (target_node,)).fetchone()[0]
        return dict(relation_id=rid, source=source, relation_type=kind, target=target, **json.loads(raw))

    def _node_id(self, entity_id: str) -> int:
        row = self.connection.execute("SELECT node_id FROM entities WHERE public_id = ?", (entity_id,)).fetchone()
        if row is None: raise ValueError(f"Unknown entity ID: {entity_id}")
        return row[0]

    def _document(self, entity_id: str, entity: dict) -> str:
        return " ".join((entity_id, str(entity.get("name", "")),
                         json.dumps(entity.get("content", {}), ensure_ascii=False))).lower()

    def search_code(self, query: str, entity_type: str | None = None,
                    operation_kind: str | None = None, limit: int = 20,
                    cursor: str | None = None) -> dict:
        if self._schema_version == 1:
            return self._search_v1(query, entity_type, operation_kind, limit, cursor)
        terms = sorted(set(re.findall(r"[a-z0-9_]+", query.lower())))
        where, parameters, join = [], [], ""
        if entity_type: where.append("e.kind = ?"); parameters.append(entity_type)
        if operation_kind: where.append("e.operation_kind = ?"); parameters.append(operation_kind)
        if terms:
            join = " JOIN entity_search s ON s.rowid = e.node_id"
            where.append("s.entity_search MATCH ?")
            parameters.append(" OR ".join('"' + term + '"*' for term in terms))
        elif not entity_type and not operation_kind: where.append("0")
        condition = " AND ".join(where) or "1"
        scope = repr((query, entity_type, operation_kind)); key = hashlib.sha256(scope.encode()).hexdigest()[:12]
        if not 1 <= limit <= 100: raise ValueError("limit must be between 1 and 100")
        offset = 0
        if cursor is not None:
            parts = cursor.split(":")
            if len(parts) != 2 or parts[0] != key or not parts[1].isdigit(): raise ValueError("Invalid cursor for this query")
            offset = int(parts[1])
        total = self.connection.execute(f"SELECT count(*) FROM entities e{join} WHERE {condition}", parameters).fetchone()[0]
        if offset > total: raise ValueError("Cursor is outside the result set")
        exact = "(lower(e.public_id) = ? OR lower(e.name) = ? OR lower(COALESCE(e.method_id, '')) = ?) DESC"
        order = exact + (", bm25(entity_search)" if terms else "") + ", e.observed DESC, e.public_id"
        rows = self.connection.execute(
            f"SELECT e.public_id FROM entities e{join} WHERE {condition} ORDER BY {order} LIMIT ? OFFSET ?",
            [*parameters, query.lower(), query.lower(), query.lower(), limit, offset]).fetchall()
        items = []
        for (eid,) in rows:
            entity = self.entities[eid]
            item = dict(self.card(eid), match_reason={"matched_terms": [term for term in terms if term in self._document(eid, entity)]})
            items.append(item); self._queried[eid] = dict(item, provenance=entity["provenance"])
        return dict(items=items, total=total,
                    next_cursor=f"{key}:{offset + limit}" if offset + limit < total else None,
                    index_scope=self.metadata.get("source_scope", "provided_records"))

    def get_relations(self, entity_id: str, relation_type: str | None = None,
                      direction: str = "both", limit: int = 20,
                      cursor: str | None = None) -> dict:
        if self._schema_version == 1:
            return self._relations_v1(entity_id, relation_type, direction, limit, cursor)
        entity_id = self.resolve(entity_id)
        if direction not in {"incoming", "outgoing", "both"}: raise ValueError("direction must be incoming, outgoing, or both")
        node_id = self._node_id(entity_id); ways = [("incoming", "target_node"), ("outgoing", "source_node")]
        if relation_type is None:
            available = []
            for way, column in ways:
                if direction not in {way, "both"}: continue
                rows = self.connection.execute(f"SELECT kind, count(*) FROM relations WHERE {column} = ? GROUP BY kind", (node_id,))
                available.extend(dict(relation_type=kind, direction=way, count=count) for kind, count in rows)
            return dict(entity_id=entity_id, available_relations=sorted(available, key=lambda r: (r["relation_type"], r["direction"])))
        columns = [column for way, column in ways if direction in {way, "both"}]
        condition = " OR ".join(f"{column} = ?" for column in columns)
        scope = repr((entity_id, relation_type, direction)); key = hashlib.sha256(scope.encode()).hexdigest()[:12]
        if not 1 <= limit <= 100: raise ValueError("limit must be between 1 and 100")
        offset = 0
        if cursor is not None:
            parts = cursor.split(":")
            if len(parts) != 2 or parts[0] != key or not parts[1].isdigit(): raise ValueError("Invalid cursor for this query")
            offset = int(parts[1])
        params = [node_id] * len(columns) + [relation_type]
        total = self.connection.execute(f"SELECT count(*) FROM relations WHERE ({condition}) AND kind = ?", params).fetchone()[0]
        if offset > total: raise ValueError("Cursor is outside the result set")
        rows = self.connection.execute(f"SELECT edge_id FROM relations WHERE ({condition}) AND kind = ? ORDER BY edge_id LIMIT ? OFFSET ?",
                                       [*params, limit, offset]).fetchall()
        edges = [self.relations[rid] for (rid,) in rows]
        items = [dict(edge, neighbor=self.card(edge["target"] if edge["source"] == entity_id else edge["source"])) for edge in edges]
        for edge in items: self._queried[edge["relation_id"]] = edge
        return dict(items=items, total=total,
                    next_cursor=f"{key}:{offset + limit}" if offset + limit < total else None)

    def _search_v1(self, query, entity_type, operation_kind, limit, cursor):
        terms = sorted(set(re.findall(r"[a-z0-9_]+", query.lower())))
        where, parameters = [], []
        if entity_type: where.append("kind = ?"); parameters.append(entity_type)
        if operation_kind: where.append("operation_kind = ?"); parameters.append(operation_kind)
        if terms:
            where.append("rowid IN (SELECT rowid FROM entity_search WHERE entity_search MATCH ?)")
            parameters.append(" OR ".join('"' + term + '"*' for term in terms))
        elif not entity_type and not operation_kind: where.append("0")
        condition = " AND ".join(where) or "1"; scope = repr((query, entity_type, operation_kind))
        key = hashlib.sha256(scope.encode()).hexdigest()[:12]
        if not 1 <= limit <= 100: raise ValueError("limit must be between 1 and 100")
        offset = 0
        if cursor is not None:
            parts = cursor.split(":")
            if len(parts) != 2 or parts[0] != key or not parts[1].isdigit(): raise ValueError("Invalid cursor for this query")
            offset = int(parts[1])
        total = self.connection.execute("SELECT count(*) FROM entities WHERE " + condition, parameters).fetchone()[0]
        if offset > total: raise ValueError("Cursor is outside the result set")
        lexical = " + ".join("(instr(document, ?) > 0)" for _ in terms) or "(0 + 0)"
        ranking = "(lower(id) = ? OR lower(name) = ? OR lower(COALESCE(method_id, '')) = ?) DESC, " + lexical + " DESC, observed DESC, id"
        rows = self.connection.execute("SELECT id, document FROM entities WHERE " + condition + " ORDER BY " + ranking + " LIMIT ? OFFSET ?",
                                       [*parameters, query.lower(), query.lower(), query.lower(), *terms, limit, offset]).fetchall()
        items = []
        for eid, document in rows:
            item = dict(self.card(eid), match_reason={"matched_terms": [term for term in terms if term in document]})
            items.append(item); self._queried[eid] = dict(item, provenance=self.entities[eid]["provenance"])
        return dict(items=items, total=total, next_cursor=f"{key}:{offset + limit}" if offset + limit < total else None,
                    index_scope=self.metadata.get("source_scope", "provided_records"))

    def _relations_v1(self, entity_id, relation_type, direction, limit, cursor):
        entity_id = self.resolve(entity_id)
        if direction not in {"incoming", "outgoing", "both"}: raise ValueError("direction must be incoming, outgoing, or both")
        ways = [("incoming", "target"), ("outgoing", "source")]
        if relation_type is None:
            available = []
            for way, column in ways:
                if direction not in {way, "both"}: continue
                rows = self.connection.execute(f"SELECT kind, count(*) FROM relations WHERE {column} = ? GROUP BY kind", (entity_id,))
                available.extend(dict(relation_type=kind, direction=way, count=count) for kind, count in rows)
            return dict(entity_id=entity_id, available_relations=sorted(available, key=lambda r: (r["relation_type"], r["direction"])))
        columns = [column for way, column in ways if direction in {way, "both"}]
        condition = " OR ".join(f"{column} = ?" for column in columns)
        rows = self.connection.execute(f"SELECT payload FROM relations WHERE ({condition}) AND kind = ? ORDER BY id",
                                       [*[entity_id] * len(columns), relation_type])
        edges = [json.loads(row[0]) for row in rows]
        page = self._page(edges, limit, cursor, repr((entity_id, relation_type, direction)))
        page["items"] = [dict(edge, neighbor=self.card(edge["target"] if edge["source"] == entity_id else edge["source"])) for edge in page["items"]]
        for edge in page["items"]: self._queried[edge["relation_id"]] = edge
        return page
