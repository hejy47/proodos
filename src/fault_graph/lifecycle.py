"""Run report-only Coccinelle rules and import their source evidence."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from collections.abc import Sequence

from src.fault_graph.evidence_graph import EvidenceGraph


DEFAULT_RULE = Path(__file__).resolve().parents[2] / "rules/coccinelle/memory_lifecycle.cocci"


def run_lifecycle_rules(source_root: Path, output: Path, *, rule: Path = DEFAULT_RULE,
                        scope: str | Sequence[str] = ".", timeout: int = 600,
                        spatch: str = "spatch") -> dict:
    root = source_root.resolve()
    if isinstance(scope, (str, Path)):
        scopes = [str(scope)]
    else:
        scopes = [str(item) for item in scope]
    if not scopes:
        raise ValueError("Lifecycle scope must contain at least one source path")
    targets = []
    for item in scopes:
        target = (root / item).resolve()
        if not target.is_relative_to(root) or not target.exists():
            raise ValueError("Lifecycle scope must exist inside source_root")
        targets.append(target)
    command = [spatch, "--sp-file", str(rule.resolve()), "--no-includes", "--include-headers",
               "--very-quiet"]
    for target in targets:
        command.extend(["--dir", str(target)] if target.is_dir() else [str(target)])
    executable = shutil.which(spatch)
    if not executable:
        raise FileNotFoundError(f"Coccinelle executable not found: {spatch}")
    env = dict(os.environ)
    python_library = Path(executable).resolve().parent.parent / "lib/coccinelle/python"
    if python_library.is_dir():
        env["PYTHONPATH"] = str(python_library) + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False, env=env)
    if result.returncode:
        raise RuntimeError(f"Coccinelle failed ({result.returncode}): {result.stderr[-4000:]}")
    records = []
    for line in result.stdout.splitlines():
        if not line.strip().startswith("{"):
            continue
        record = json.loads(line)
        path = Path(record["file"])
        if not path.is_absolute():
            path = root / path
        record["file"] = path.resolve().relative_to(root).as_posix()
        records.append(record)
    payload = dict(schema_version=1, source_root=str(root), scope=scopes if len(scopes) > 1 else scopes[0], command=command,
                   rule_file=str(rule.resolve()), rule_sha256=hashlib.sha256(rule.read_bytes()).hexdigest(),
                   source_sha256={name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                                  for name in sorted({record["file"] for record in records})},
                   diagnostics=result.stderr[-8000:], records=records)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def add_lifecycle_matches(graph: EvidenceGraph, path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("Unsupported lifecycle schema")
    source_root = graph.metadata.get("source_root")
    if source_root and Path(payload["source_root"]).resolve() != Path(source_root).resolve():
        raise ValueError("Lifecycle source_root does not match indexed source_root")
    for name, digest in payload.get("source_sha256", {}).items():
        indexed_digest = graph.metadata.get("source_sha256", {}).get(name)
        if indexed_digest is not None and indexed_digest != digest:
            raise ValueError(f"Lifecycle source content does not match indexed source: {name}")
    graph.metadata["lifecycle"] = {key: value for key, value in payload.items() if key != "records"}
    by_file = {}
    for eid, entity in graph.entities.items():
        if entity["entity_type"] == "function":
            location = entity["content"]["location"]
            by_file.setdefault(location["file"], []).append((eid, location))
    operation_sites = {}
    for record in payload["records"]:
        if record["record_type"] != "operation":
            continue
        location = dict(file=record["file"], line=record["line"], column=record["column"])
        functions = [eid for eid, loc in by_file.get(record["file"], [])
                     if loc["line"] <= record["line"] <= loc["end_line"]]
        if len(functions) != 1:
            graph.metadata.setdefault("diagnostics", []).append(
                dict(kind="unresolved_lifecycle_function", match=record))
            continue
        function_id = functions[0]
        eid = f"op:{record['file']}:{record['line']}:{record['column']}:{record['operation_kind']}"
        provenance = dict(extractor="coccinelle", source=str(path), rule_id=record["rule_id"],
                          rule_file=payload["rule_file"], rule_sha256=payload["rule_sha256"], match=record)
        function = graph.entities[function_id]["content"]
        # Kernel graphs keep only source spans in SQLite.  Read the function
        # body on demand while creating the small lifecycle snippet.
        function_source = graph.source_text(function_id) or ""
        lines = function_source.splitlines()
        relative_line = record["line"] - function["location"]["line"]
        snippet = "\n".join(lines[max(0, relative_line - 2):relative_line + 3])
        if eid not in graph.entities:
            graph.add_entity(eid, "lifecycle_operation", name=record["api"],
                             content=dict(operation_kind=record["operation_kind"], api=record["api"],
                                          function_id=function_id, location=location, source_code=snippet,
                                          expression=record.get("expression")), provenance=provenance)
            graph.add_relation(function_id, "contains_operation", eid, provenance)
        operation_sites[(record["file"], record["line"], record["column"])] = eid
    for record in payload["records"]:
        if record["record_type"] != "relation":
            continue
        start = operation_sites.get((record["file"], record["from_line"], record["from_column"]))
        end = operation_sites.get((record["file"], record["to_line"], record["to_column"]))
        if start and end and graph.entities[start]["content"]["function_id"] == graph.entities[end]["content"]["function_id"]:
            graph.add_relation(start, "local_lifecycle_relation", end,
                               dict(extractor="coccinelle", source=str(path), rule_id=record["rule_id"],
                                    rule_sha256=payload["rule_sha256"], match=record),
                               relation_kind=record["relation_kind"])
