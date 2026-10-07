"""Typed, provenance-bearing fault-context navigation for one failure case."""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re


class EvidenceGraph:
    def __init__(self, case_id: str, *, metadata: dict | None = None):
        self.case_id = case_id
        self.metadata = metadata or {}
        self.entities: dict[str, dict] = {}
        self.relations: dict[str, dict] = {}
        self.outgoing: dict[str, list[str]] = defaultdict(list)
        self.incoming: dict[str, list[str]] = defaultdict(list)
        self.aliases: dict[str, str] = {}
        self._documents: dict[str, str] = {}
        self._queried: dict[str, dict] = {}

    def add_entity(self, entity_id: str, entity_type: str, *, content: dict,
                   provenance: dict, **attributes) -> str:
        if entity_id in self.entities:
            raise ValueError(f"Duplicate entity ID: {entity_id}")
        entity = dict(entity_id=entity_id, entity_type=entity_type, content=content,
                      provenance=provenance, **attributes)
        self.entities[entity_id] = entity
        self._documents[entity_id] = " ".join([
            entity_id, str(attributes.get("name", "")),
            json.dumps(content, ensure_ascii=False),
        ]).lower()
        if entity_type == "function":
            method_id = content["method_id"]
            if method_id in self.aliases:
                raise ValueError(f"Ambiguous function ID: {method_id}")
            self.aliases[method_id] = entity_id
        return entity_id

    def add_relation(self, source: str, relation_type: str, target: str,
                     provenance: dict, **attributes) -> str:
        if source not in self.entities or target not in self.entities:
            raise ValueError(f"Unknown relation endpoint: {source} -> {target}")
        record = dict(source=source, relation_type=relation_type, target=target,
                      provenance=provenance, **attributes)
        relation_id = "edge:" + hashlib.sha256(
            json.dumps(record, sort_keys=True).encode()
        ).hexdigest()[:24]
        if relation_id not in self.relations:
            self.relations[relation_id] = dict(relation_id=relation_id, **record)
            self.outgoing[source].append(relation_id)
            self.incoming[target].append(relation_id)
        return relation_id

    def resolve(self, entity_id: str) -> str:
        resolved = self.aliases.get(entity_id, entity_id)
        if resolved not in self.entities:
            raise ValueError(f"Unknown entity ID: {entity_id}")
        return resolved

    def function_id(self, entity_id: str) -> str | None:
        entity = self.entities.get(self.aliases.get(entity_id, entity_id), {})
        if entity.get("entity_type") != "function":
            return None
        # Kernel preprocessing stores source spans rather than duplicating the
        # function body in SQLite.  A function remains a valid indexed target
        # even when its source is materialized only by a later read operation.
        return entity["content"].get("method_id")

    def source_text(self, entity_id: str) -> str | None:
        """Materialize an entity's source from its stored span when needed."""
        entity_id = self.resolve(entity_id)
        content = self.entities[entity_id]["content"]
        source = content.get("source_code")
        if source is not None:
            return str(source)
        span = content.get("source_span") or {}
        source_root = self.metadata.get("source_root")
        rel_path = span.get("file")
        if not source_root or not rel_path:
            return None
        try:
            path = Path(str(source_root)) / str(rel_path)
            data = path.read_bytes()
            start = int(span.get("start_byte", 0))
            end = int(span.get("end_byte", len(data)))
            if start < 0 or end < start or end > len(data):
                return None
            return data[start:end].decode(self.metadata.get("source_encodings", {}).get(str(rel_path), "utf-8"), errors="replace")
        except (OSError, TypeError, ValueError):
            return None

    @staticmethod
    def _page(items: list, limit: int, cursor: str | None, scope: str) -> dict:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        key = hashlib.sha256(scope.encode()).hexdigest()[:12]
        offset = 0
        if cursor is not None:
            parts = cursor.split(":")
            if len(parts) != 2 or parts[0] != key or not parts[1].isdigit():
                raise ValueError("Invalid cursor for this query")
            offset = int(parts[1])
            if offset > len(items):
                raise ValueError("Cursor is outside the result set")
        stop = offset + limit
        return dict(items=items[offset:stop], total=len(items),
                    next_cursor=f"{key}:{stop}" if stop < len(items) else None)

    def card(self, entity_id: str) -> dict:
        entity = self.entities[entity_id]
        content = entity["content"]
        return dict(entity_id=entity_id, entity_type=entity["entity_type"],
                    name=entity.get("name", ""), method_id=content.get("method_id"),
                    location=content.get("location"),
                    operation_kind=content.get("operation_kind"),
                    coverage=entity.get("coverage", {"status": "not_applicable"}))

    def search_code(self, query: str, entity_type: str | None = None,
                    operation_kind: str | None = None, limit: int = 20,
                    cursor: str | None = None) -> dict:
        terms = set(re.findall(r"[a-z0-9_]+", query.lower()))
        matches = []
        for entity_id, entity in self.entities.items():
            if entity_type and entity["entity_type"] != entity_type:
                continue
            if operation_kind and entity["content"].get("operation_kind") != operation_kind:
                continue
            document = self._documents[entity_id]
            matched = sorted(term for term in terms if term in document)
            if terms and not matched:
                continue
            if not terms and not operation_kind and not entity_type:
                continue
            exact = query.lower() in {entity_id.lower(), str(entity.get("name", "")).lower(),
                                       str(entity["content"].get("method_id", "")).lower()}
            score = (int(exact), len(matched),
                     int(entity.get("coverage", {}).get("status") == "observed"))
            matches.append((score, entity_id, matched))
        matches.sort(key=lambda item: (tuple(-v for v in item[0]), item[1]))
        page = self._page(matches, limit, cursor, repr((query, entity_type, operation_kind)))
        page["items"] = [dict(self.card(eid), match_reason={"matched_terms": matched})
                         for _, eid, matched in page["items"]]
        page["index_scope"] = self.metadata.get("source_scope", "provided_records")
        for item in page["items"]:
            self._queried[item["entity_id"]] = dict(item, provenance=self.entities[item["entity_id"]]["provenance"])
        return page

    def get_context(self, entity_id: str, offset: int = 0, limit: int = 12000) -> dict:
        entity_id = self.resolve(entity_id)
        if offset < 0 or not 1 <= limit <= 30000:
            raise ValueError("offset must be nonnegative; limit must be between 1 and 30000")
        entity = self.entities[entity_id]
        # One serialized content stream gives all entity readers the same paging contract.
        content = json.dumps(entity["content"], ensure_ascii=False, indent=2)
        result = dict(entity_id=entity_id, entity_type=entity["entity_type"],
                      content=content[offset:offset + limit], content_format="json_text",
                      provenance=entity["provenance"], total_chars=len(content),
                      next_offset=offset + limit if offset + limit < len(content) else None)
        self._queried[f"{entity_id}@{offset}"] = result
        return result

    def get_relations(self, entity_id: str, relation_type: str | None = None,
                      direction: str = "both", limit: int = 20,
                      cursor: str | None = None) -> dict:
        entity_id = self.resolve(entity_id)
        if direction not in {"incoming", "outgoing", "both"}:
            raise ValueError("direction must be incoming, outgoing, or both")
        ids = []
        if direction in {"incoming", "both"}:
            ids.extend(self.incoming[entity_id])
        if direction in {"outgoing", "both"}:
            ids.extend(self.outgoing[entity_id])
        edges = [self.relations[rid] for rid in sorted(set(ids))]
        if relation_type is None:
            kinds = defaultdict(int)
            for edge in edges:
                if edge["source"] == entity_id and direction != "incoming":
                    kinds[(edge["relation_type"], "outgoing")] += 1
                if edge["target"] == entity_id and direction != "outgoing":
                    kinds[(edge["relation_type"], "incoming")] += 1
            return {"entity_id": entity_id, "available_relations": [
                dict(relation_type=kind, direction=way, count=count)
                for (kind, way), count in sorted(kinds.items())]}
        edges = [edge for edge in edges if edge["relation_type"] == relation_type]
        page = self._page(edges, limit, cursor, repr((entity_id, relation_type, direction)))
        page["items"] = [dict(edge, neighbor=self.card(
            edge["target"] if edge["source"] == entity_id else edge["source"]))
            for edge in page["items"]]
        for edge in page["items"]:
            self._queried[edge["relation_id"]] = edge
        return page

    def verification_evidence(self, method_id: str, *, max_chars: int = 16000) -> dict:
        entity_id = self.resolve(method_id)
        related = []
        for record in self._queried.values():
            if entity_id in {record.get("entity_id"), record.get("source"), record.get("target")}:
                related.append(record)
            elif record.get("entity_type") == "lifecycle_operation":
                operation = self.entities.get(record.get("entity_id"), {})
                if operation.get("content", {}).get("function_id") == entity_id:
                    related.append(record)
        selected, size = [], 0
        for record in related:
            length = len(json.dumps(record))
            if size + length <= max_chars:
                selected.append(record)
                size += length
        return dict(records=selected, omitted_records=len(related) - len(selected),
                    interpretation="Navigation evidence; causal effects are not established.")

    def reset_queries(self) -> None:
        self._queried.clear()

    def save(self, path: Path) -> None:
        if path.suffix == ".sqlite":
            from src.fault_graph.evidence_sqlite import save_sqlite
            save_sqlite(self, path)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(dict(schema_version=1, case_id=self.case_id,
                           metadata=self.metadata, entities=list(self.entities.values()),
                           relations=list(self.relations.values())), handle)

    @classmethod
    def load(cls, path: Path) -> EvidenceGraph:
        if path.suffix == ".sqlite":
            from src.fault_graph.evidence_sqlite import SQLiteEvidenceGraph
            return SQLiteEvidenceGraph(path)
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        if payload.get("schema_version") != 1:
            raise ValueError("Unsupported fault-context schema")
        graph = cls(payload["case_id"], metadata=payload["metadata"])
        # Reuse loaded records to avoid a second full graph in memory.
        for entity in payload["entities"]:
            eid = entity["entity_id"]
            graph.entities[eid] = entity
            graph._documents[eid] = " ".join([
                eid, str(entity.get("name", "")), json.dumps(entity["content"], ensure_ascii=False),
            ]).lower()
            if entity["entity_type"] == "function":
                graph.aliases[entity["content"]["method_id"]] = eid
        for edge in payload["relations"]:
            rid = edge["relation_id"]
            if edge["source"] not in graph.entities or edge["target"] not in graph.entities:
                raise ValueError("Unknown endpoint in stored fault context")
            graph.relations[rid] = edge
            graph.outgoing[edge["source"]].append(rid)
            graph.incoming[edge["target"]].append(rid)
        return graph
