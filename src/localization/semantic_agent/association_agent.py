from __future__ import annotations

from pathlib import Path
from typing import Sequence
from src.localization.semantic_agent.tools.function_tool import function_tool
from src.localization.semantic_agent.agent import Agent, validate_explanation
from src.localization.semantic_agent.prompt import (
    association_agent_system_prompt, association_agent_few_shot,
)
from src.localization.semantic_agent.tools.evidence_tools import (
    load_case_graph, read_entity_text, search_entities_text, tool_json,
)
from src.fault_graph.kernel_oops import resolve_crash_method_id_from_kernel_oops
from src.fault_graph.method_id_map import resolve_known_method_id
from src.localization.semantic_agent.tools.runtime_experiments import RuntimeExperiments


class AssociationAgent:
    """Search source, observe execution, and propose a testable suspect."""
    name = "association_agent"
    instructions = association_agent_system_prompt + "\n" + association_agent_few_shot
    max_steps = 10

    def __init__(self, llm_settings, preprocess_data, output_dir, test_id, *, project=None, runtime=None):
        self.preprocess_data = preprocess_data
        self.test_id = str(test_id)
        self._fault_context = load_case_graph(preprocess_data, self.test_id)
        self.runtime = runtime or RuntimeExperiments(project, preprocess_data, self.test_id)
        self.agent = Agent(
            tools=self._build_tools(), system_prompt=self.instructions,
            settings=llm_settings, name=self.name, output_dir=output_dir,
            test_id=self.test_id, max_steps=self.max_steps, validate=self.validate,
            tool_available=lambda name: (
                name not in {"trace_functions", "probe_function"}
                or self.runtime.can_run_runtime_experiment
            ),
        )

    def run(self, task):
        return {"stage": self.name, "report": self.agent.run(task)}

    def _build_tools(self) -> Sequence[object]:
        @function_tool
        def search_entities(query: str, entity_type: str | None = None,
                        operation_kind: str | None = None, limit: int = 10,
                        cursor: str | None = None) -> str:
            """Find source declarations and failure evidence indexed for the current case.

            Functions include both standalone functions and class methods.
            Available entity kinds depend on the case's source and imported evidence.
            Leave filters unset when exploring; use kinds returned by the tools.
            Returns readable entity cards with IDs, briefs, and pagination.
            Each page contains at most 10 entities, even if a larger limit is requested.

            Args:
                query: Keywords, a symbol name, or an indexed function/method ID.
                entity_type: Optional entity kind, such as function or crash_report.
                operation_kind: Optional lifecycle filter, such as acquire or release;
                    use only when the case contains lifecycle entities.
                limit: Maximum number of entities in this page.
                cursor: Continuation cursor returned by the previous search.
            """
            return search_entities_text(self._fault_context, query, entity_type,
                                        operation_kind, limit, cursor)

        @function_tool
        def read_entity(entity_id: str, offset: int = 0, limit: int = 12000) -> str:
            """Read source or case evidence using an ID returned by the tools.

            Function/method entities expose source; failure-report entities expose
            report text; other entities expose their recorded attributes and evidence.
            Discover report IDs through search instead of assuming a fixed ID.
            Returns readable metadata and source/report text, without JSON encoding.
            offset and limit count content characters; follow next_offset when present.

            Args:
                entity_id: Exact entity ID returned by search or relations.
                offset: Starting character offset.
                limit: Maximum characters to read.
            """
            return read_entity_text(self._fault_context, entity_id, offset, limit)

        @function_tool
        def get_relations(entity_id: str, relation_type: str | None = None,
                          direction: str = "both", limit: int = 20,
                          cursor: str | None = None) -> str:
            """Discover relation types, or read one-hop neighbors with provenance.

            Omit relation_type to discover available types and directions.
            Specify incoming, outgoing, or both. Follow next_cursor for more.
            Available relations depend on the current case. Read their provenance
            to distinguish static candidates, report references, and observations.
            A relation alone does not establish execution or a causal effect.

            Args:
                entity_id: Exact entity ID returned by search or relations.
                relation_type: Relation kind; omit to discover available kinds.
                direction: incoming, outgoing, or both.
                limit: Maximum neighbors per page.
                cursor: Continuation cursor from the preceding query.
            """
            return tool_json(self._fault_context.get_relations, entity_id, relation_type,
                             direction, limit, cursor)

        @function_tool
        def trace_functions(method_ids: list[str], max_events: int = 1000) -> str:
            """Rerun the current test or reproducer and trace selected functions/methods.

            Collect execution evidence on demand for a small set of indexed suspects.
            Inspect the returned events, counts, collection status, and truncation.
            Missing events can reflect collection limits; they do not by themselves
            prove that a function did not execute. No preprocessing trace is required.

            Args:
                method_ids: Exact indexed function/method IDs returned by source tools.
                max_events: Maximum events or compact event patterns to return;
                    the runtime backend determines the event format.
            """
            if not isinstance(method_ids, list) or not method_ids:
                return "status: validation_error\nerror: method_ids must contain at least one indexed function ID."
            resolved = []
            invalid = []
            for method_id in method_ids:
                value = self.resolve_method(str(method_id))
                if value is None:
                    invalid.append(str(method_id))
                elif value not in resolved:
                    resolved.append(value)
            if invalid:
                return "status: validation_error\nerror: unknown method_id(s): " + ", ".join(invalid)
            return self.runtime.trace_functions(resolved, max_events)

        @function_tool
        def probe_function(method_id: str, probe_spec: str | dict[str, object] = "") -> str:
            """Rerun the current test or reproducer and sample values at one function/method.

            Use this to investigate a concrete question about the suspect's runtime
            inputs or state. Supported observation points and values depend on the
            backend. Returned values are samples, not a complete execution history;
            missing samples or setup errors leave the requested fact unresolved.

            Args:
                method_id: Exact indexed function/method ID returned by source tools.
                probe_spec: A JSON object (preferred) or JSON string selecting values.
                    For Java/Vul4J, provide type entry and a nonempty expressions list,
                    e.g. {"type":"entry","expressions":["length","this.repository"]}.
                    Only the specified expressions are sampled, as text, at method entry.
                    Use parameters, accessible fields, or side-effect-free expressions;
                    method calls must be known to have no side effects. Locals declared
                    in the body and return/line probes are unavailable at this point.
                    Optional max_calls defaults to 20 (range 1..100). Invalid or missing
                    specifications return an error; values are not chosen automatically.
                    For Linux kernel cases, supply a JSON object (preferred) or JSON
                    string with type entry or return, fetch expressions, and optional
                    stacktrace. Fetch expressions use tracefs syntax, for example
                    {"type":"entry","fetch":["arg1=$arg1:x64", "field=+8($arg2):x8"]}.
                    C-style expressions such as $arg2->field are not supported.
            """
            resolved = self.resolve_method(method_id)
            if resolved is None:
                return "status: validation_error\nerror: method_id is not an indexed function."
            return self.runtime.probe_function(resolved, probe_spec)

        return [search_entities, read_entity, get_relations, trace_functions, probe_function]

    def _format_crash_point(self) -> str:
        """Source-level failure site from the imported report."""
        evidence_crash = (self._fault_context.metadata.get("crash_points", {}).get(self.test_id)
                          or self._fault_context.metadata.get("crash_point"))
        if evidence_crash:
            return str(evidence_crash)
        if self.preprocess_data.metadata.get("language") == "c":
            test = self._get_test_record_for_test(self.test_id)
            resolved = resolve_crash_method_id_from_kernel_oops(
                self._failure_report(test), self.preprocess_data.method_ids,
            )
            if resolved:
                return resolved
        return "(unavailable — no source frame resolved from the failure report)"

    def _get_test_record_for_test(self, test_id: str) -> dict[str, object]:
        for test_record in self.preprocess_data.test_records:
            if str(test_record["test_id"]) == test_id:
                return test_record
        raise ValueError(f"No test record found for test_id {test_id}")

    def _known_method_catalog(self) -> list[str]:
        """Candidate IDs from the static source index."""
        return list(self._fault_context.aliases)

    def resolve_method(self, method_id: str | None) -> str | None:
        """Map candidate method_id onto a known catalog id; None if it does not exist."""
        if not method_id:
            return None
        resolved = self._fault_context.function_id(method_id)
        if resolved is not None:
            return resolved
        return resolve_known_method_id(method_id, self._known_method_catalog())

    def method_source(self, method_id: str | None) -> str:
        """Return source for a known function, materializing span-backed code."""
        resolved = self.resolve_method(method_id)
        if resolved is None:
            return "(source unavailable)"
        try:
            entity_id = self._fault_context.resolve(resolved)
            return self._fault_context.source_text(entity_id) or "(source unavailable)"
        except (KeyError, ValueError):
            return "(source unavailable)"

    def _failure_report(self, test: dict[str, object]) -> str:
        metadata = test.get("metadata") if isinstance(test.get("metadata"), dict) else {}
        failure_message = metadata.get("test_failure_message", "") or "No failure message available"
        report = (self._fault_context.entities.get("report")
                  or self._fault_context.entities.get(f"report:{test['test_id']}"))
        if report and report["content"].get("text"):
            failure_message = report["content"]["text"]
        text = str(failure_message)
        # Hung-task console logs can be multiple megabytes; sending them whole
        # overflows the model context and collapses localization to a fallback.
        limit = 100_000
        if len(text) <= limit:
            return text
        head, tail = limit * 2 // 3, limit - limit * 2 // 3
        return f"{text[:head].rstrip()}\n...[crash report truncated]...\n{text[-tail:]}"

    def _format_single_test_failure(self, test: dict[str, object]) -> str:
        test_id = str(test["test_id"])
        return "\n".join(
            [
                f"Test ID: {test_id}",
                f"Failure Message: {self._failure_report(test)}",
                "",
            ]
        )

    def _format_test_code(self, test: dict[str, object]) -> str:
        test_code = test.get("source_code")
        if not test_code:
            path = self._fault_context.metadata.get("syz_path")
            if path and Path(path).is_file():
                test_code = Path(path).read_text(encoding="utf-8", errors="replace")
        test_code = test_code or "No source code available"
        return self._truncate_text(str(test_code), limit=4000)

    def _truncate_text(self, text: str, limit: int = 1000) -> str:
        if len(text) <= limit:
            return text
        return f"{text[:limit].rstrip()}\n...[truncated]"

    def case_input(self) -> str:
        test = self._get_test_record_for_test(self.test_id)
        return (f"<failure>\n{self._format_single_test_failure(test)}\n</failure>\n"
                f"<test_code>\n{self._format_test_code(test)}\n</test_code>\n"
                f"<crash_point>\n{self._format_crash_point()}\n</crash_point>")

    def validate(self, payload):
        validate_explanation(payload)
        if not isinstance(payload.get("candidates"), list) or not payload["candidates"]:
            raise ValueError("Return a nonempty candidates list of exact function IDs.")
        if any(not isinstance(mid, str) or not self.resolve_method(mid) for mid in payload["candidates"]):
            raise ValueError("Every candidate must resolve to an indexed function.")
        if not self.resolve_method(payload.get("method_id")):
            raise ValueError("method_id must identify one indexed suspect.")
        if not str(payload.get("hypothesis") or "").strip():
            raise ValueError("Provide a concrete hypothesis for the suspect.")
