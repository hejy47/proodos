"""Collect one selected test's method coverage and deduplicated runtime calls."""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
import time

from src.java_runtime.tracing import JavaTraceCollector
from src.java_runtime.test_runner_builder import build_test_runner
from src.java_runtime.trace_agent_builder import build_trace_agent
from src.models import TestCase
from src.fault_graph.method_id_map import build_trace_method_map, has_unqualified_descriptor
from src.utils.java_source import discover_package_prefixes, discover_top_level_types, extract_package_name
from src.utils.java_util import split_test_id


@dataclass
class JavaExecutionTrace:
    test_id: str
    status: str = "unavailable"
    counts: dict[str, int] = field(default_factory=dict)
    calls: set[tuple[str, str]] = field(default_factory=set)
    include_prefixes: tuple[str, ...] = ()
    test_outcome: str | None = None
    failure_report: str = ""
    error: str = ""
    duration_seconds: float = 0.0
    incoming: dict[str, set[str]] = field(default_factory=dict)
    source_to_runtime: dict[str, str] = field(default_factory=dict)
    runtime_to_source: dict[str, str] = field(default_factory=dict)
    _ambiguous_ids: set[str] = field(default_factory=set, repr=False)
    _source_ids: frozenset[str] | None = field(default=None, repr=False)

    def bind_source_index(self, graph):
        """Associate runtime IDs without loading unrelated source bodies."""
        if self.status != "success" or graph is None:
            return
        db = getattr(graph, "connection", None)
        if db is not None and getattr(graph, "_schema_version", 1) >= 2:
            source_ids = frozenset(row[0] for row in db.execute(
                "SELECT method_id FROM entities WHERE kind IN ('function','test_method') "
                "AND method_id IS NOT NULL"))
        else:
            source_ids = frozenset(entity["content"]["method_id"] for entity in graph.entities.values()
                                   if entity["entity_type"] in {"function", "test_method"}
                                   and entity["content"].get("method_id"))
        if source_ids == self._source_ids:
            return
        self.source_to_runtime, self._ambiguous_ids = build_trace_method_map(source_ids, self.counts)
        self.runtime_to_source = {runtime: source for source, runtime in self.source_to_runtime.items()}
        self._source_ids = source_ids

    def runtime_method_id(self, method_id: str | None) -> str | None:
        if not method_id:
            return None
        if method_id in self.source_to_runtime:
            return self.source_to_runtime[method_id]
        if method_id in self.counts:
            return method_id
        if method_id in self._ambiguous_ids or "@" in method_id or has_unqualified_descriptor(method_id):
            return None
        return method_id

    def covers(self, method_id: str) -> bool | None:
        if self.status != "success":
            return None
        method_id = self.runtime_method_id(method_id)
        if method_id is None:
            return None
        if method_id in self.counts:
            return True
        owner = method_id.split("#", 1)[0]
        if any(owner.startswith(prefix) for prefix in self.include_prefixes):
            return False
        return None

    def coverage(self, method_id: str) -> dict:
        result = {"test_id": self.test_id, "covered": self.covers(method_id)}
        if result["covered"] is not None:
            result["call_count"] = self.counts.get(self.runtime_method_id(method_id), 0)
        return result

    def observes_call(self, caller: str, callee: str) -> bool | None:
        if self.covers(caller) is None or self.covers(callee) is None:
            return None
        return (self.runtime_method_id(caller), self.runtime_method_id(callee)) in self.calls

    def summary(self) -> dict:
        return {"test_id": self.test_id, "status": self.status,
                "test_outcome": self.test_outcome, "methods": len(self.counts),
                "calls": len(self.calls), "duration_seconds": round(self.duration_seconds, 3),
                "error": self.error}


def collect_test_trace(*, project, test_id: str, output_dir: Path,
                       deadline: float | None = None) -> JavaExecutionTrace:
    """Rerun the selected failure once; the same result serves FL and patching."""
    trace = JavaExecutionTrace(str(test_id))
    started = time.monotonic()
    try:
        if project is None or getattr(project, "spec", None) is None:
            raise ValueError("Java tracing requires a project")
        test_class, test_method = split_test_id(trace.test_id)
        if test_method == "*":
            raise ValueError("Tracing requires a single selected test method")
        runner_build, agent_build = build_test_runner(), build_trace_agent()
        for build in (runner_build, agent_build):
            if not build.success:
                raise RuntimeError(build.stderr.strip() or build.stdout.strip() or "Java tool build failed")
        roots = [*project.discover_source_roots(), *project.discover_test_roots()]
        prefixes = [package + "." for package in discover_package_prefixes(roots)]
        for root in roots:
            for file in root.glob("*.java"):
                if not extract_package_name(file):
                    prefixes.extend(discover_top_level_types(file))
        prefixes.append(test_class)
        trace.include_prefixes = tuple(dict.fromkeys(prefixes))
        stage = Path(output_dir)
        stage.mkdir(parents=True, exist_ok=True)
        spectra, tests, events = (stage / name for name in ("spectra.csv", "tests.csv", "trace.txt"))
        # A failed rerun must never reuse reports from a previous collection.
        for path in (spectra, tests, events):
            path.unlink(missing_ok=True)
        runner = JavaTraceCollector(project)
        errors = runner.collect_reports(
            selected_tests=[TestCase(trace.test_id, test_class, test_method, metadata={"framework": "JUNIT"})],
            agent_jar_path=agent_build.agent_jar_path,
            include_prefixes=list(trace.include_prefixes), stage_dir=stage,
            spectra_path=spectra, tests_report_path=tests, trace_report_path=events,
            deadline=deadline,
        )
        if errors:
            raise RuntimeError(errors[0].execution_error)
        methods = runner._read_trace_spectra(spectra)
        with tests.open(encoding="utf-8", newline="") as stream:
            rows = [row for row in csv.DictReader(stream)
                    if row["name"].replace("#", "::", 1) == trace.test_id.replace("#", "::", 1)]
        if len(rows) != 1:
            raise ValueError("Trace report must contain exactly one selected test")
        row = rows[0]
        trace.test_outcome = row["outcome"]
        trace.failure_report = row.get("stacktrace") or ""
        if trace.test_outcome not in {"FAIL", "ERROR"}:
            raise ValueError("Selected failing test did not fail during trace collection")
        matching = [line.split("\t") for line in events.read_text(encoding="utf-8").splitlines()
                    if line.split("\t", 1)[0] == row["id"]]
        if len(matching) != 1 or len(matching[0]) != 5:
            raise ValueError("Trace report is missing method counts or runtime call relations")
        _, _, _, counts, calls = matching[0]
        parsed_counts, parsed_calls = {}, set()
        for payload in filter(None, counts.split(",")):
            number, count = map(int, payload.split(":"))
            if count <= 0:
                raise ValueError("Invalid trace method count")
            parsed_counts[methods[number]] = count
        for payload in filter(None, calls.split(",")):
            caller, callee = map(int, payload.split(">"))
            pair = (methods[caller], methods[callee])
            if not all(method in parsed_counts for method in pair):
                raise ValueError("Runtime call endpoints must have been executed")
            parsed_calls.add(pair)
        if not parsed_counts:
            raise ValueError("Trace collection recorded no project methods")
        trace.counts, trace.calls = parsed_counts, parsed_calls
        for caller, callee in trace.calls:
            trace.incoming.setdefault(callee, set()).add(caller)
        trace.status = "success"
    except Exception as exc:
        trace.error = f"{type(exc).__name__}: {exc}"
    finally:
        trace.duration_seconds = time.monotonic() - started
    return trace
