"""Bounded Orchestrator workflow for three causal localization stages."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.localization.semantic_agent.agent import MAX_FINAL_ATTEMPTS
from src.localization.semantic_agent.association_agent import AssociationAgent
from src.localization.semantic_agent.counterfactual_agent import CounterfactualAgent
from src.localization.semantic_agent.intervention_agent import InterventionAgent
from src.localization.semantic_agent.orchestrator_agent import OrchestratorAgent
from src.localization.semantic_agent.tools.runtime_experiments import RuntimeExperiments
from src.utils.agent_logging import append_log


MAX_REQUESTS_PER_STAGE = 3


class LocalizationWorkflow:
    """Run the causal chain with evidence-driven returns and per-specialist limits."""
    max_requests_per_stage = MAX_REQUESTS_PER_STAGE

    def __init__(
        self,
        llm_settings,
        preprocess_data,
        output_dir,
        test_id: str,
        *,
        project=None,
    ):
        self.llm_settings = llm_settings
        self.preprocess_data = preprocess_data
        self.output_dir = Path(output_dir)
        self.test_id = str(test_id)
        self.project = project
        self.runtime = RuntimeExperiments(project, preprocess_data, self.test_id)
        self.association = AssociationAgent(
            llm_settings, preprocess_data, self.output_dir, self.test_id,
            project=project, runtime=self.runtime,
        )
        self.intervention = InterventionAgent(
            llm_settings, self.output_dir, self.test_id, self.runtime,
        )
        self.counterfactual = CounterfactualAgent(
            llm_settings, self.output_dir, self.test_id,
        )
        self.records: list[dict[str, Any]] = []
        self.stage_request_counts = {
            "association_agent": 0,
            "intervention_agent": 0,
            "counterfactual_agent": 0,
        }
        self.current_method_id: str | None = None
        self.current_association_report: dict[str, Any] = {}
        self.orchestrator = OrchestratorAgent(
            llm_settings, self.output_dir, self.test_id, self,
        )

    def _encode(self, value: Any, limit: int = 12000) -> str:
        text = json.dumps(value, ensure_ascii=False, default=str, indent=2)
        if len(text) <= limit:
            return text
        return text[:limit].rstrip() + "\n...[context truncated]"

    def _request_text(self, task: str) -> str:
        """Keep an Orchestrator handoff concise without touching case evidence."""
        text = str(task or "").strip()
        if len(text) <= 6000:
            return text
        return text[:6000].rstrip() + "\n...[orchestrator request truncated]"

    def _association_task(self, request: str = "") -> str:
        task = f"<case>\n{self.association.case_input()}\n</case>\n"
        if request:
            task += f"<orchestrator_request>\n{self._request_text(request)}\n</orchestrator_request>\n"
        task += (
            "Return one preliminary suspect and a concrete hypothesis. "
            "Use probe_function or trace_functions only when they answer a specific question."
        )
        return task

    def _intervention_task(
        self, association_report: dict[str, Any], request: str = "", *,
        method_id: str, hypothesis: str = "",
    ) -> str:
        source = self.association.method_source(method_id)
        if len(source) > 16000:
            source = source[:16000].rstrip() + "\n...[source truncated]"
        task = (
            f"<case>\n{self.association.case_input()}\n</case>\n"
            f"<association_report>\n{self._encode(association_report)}\n</association_report>\n"
            f"<suspect_source>\n{source}\n</suspect_source>\n"
            f"<suspect_method_id>\n{method_id}\n</suspect_method_id>\n"
            f"<hypothesis>\n{hypothesis}\n</hypothesis>\n"
        )
        if request:
            task += f"<orchestrator_request>\n{self._request_text(request)}\n</orchestrator_request>\n"
        return task + (
            "Test the stated hypothesis with one local replacement when it is well-defined. "
            "Report the result or limitation to the Orchestrator."
        )

    def _counterfactual_task(
        self, association_report: dict[str, Any], request: str = "", *,
        method_id: str | None = None,
    ) -> str:
        source = self.association.method_source(method_id)
        if len(source) > 16000:
            source = source[:16000].rstrip() + "\n...[source truncated]"
        intervention_report = self._latest_report("intervention_agent", method_id=method_id)
        task = (
            f"<case>\n{self.association.case_input()}\n</case>\n"
            f"<association_report>\n{self._encode(association_report)}\n</association_report>\n"
            f"<suspect_method_id>\n{method_id or '(not selected)'}\n</suspect_method_id>\n"
            f"<suspect_source>\n{source}\n</suspect_source>\n"
            f"<intervention_report>\n{self._encode(intervention_report)}\n</intervention_report>\n"
        )
        if request:
            task += f"<orchestrator_request>\n{self._request_text(request)}\n</orchestrator_request>\n"
        return task + (
            "Assess the supplied evidence and the Orchestrator's question. "
            "If no intervention result is available, state that the counterfactual is untested."
        )

    def _latest_report(self, stage_name: str, *, method_id: str | None = None) -> dict[str, Any]:
        for record in reversed(self.records):
            if (record.get("stage") == stage_name
                    and (method_id is None or record.get("method_id") == method_id)):
                return dict(record.get("report") or {})
        return {}

    def can_request(self, stage_name: str) -> bool:
        """Whether this specialist has request budget left for the case."""
        return (
            stage_name in self.stage_request_counts
            and self.stage_request_counts[stage_name] < self.max_requests_per_stage
        )

    def request(self, stage_name: str, task: str = "", additional_args=None) -> dict[str, Any]:
        """Run the requested specialist; constrain request count and target validity."""
        if stage_name not in self.stage_request_counts:
            return {"stage": stage_name, "status": "orchestration_error",
                    "error": f"Unknown specialist: {stage_name}."}
        if self.stage_request_counts[stage_name] >= self.max_requests_per_stage:
            return {"stage": stage_name, "status": "orchestration_error",
                    "error": f"{stage_name} request budget ({self.max_requests_per_stage}) exhausted for this case."}
        self.stage_request_counts[stage_name] += 1
        args = additional_args if isinstance(additional_args, dict) else {}
        if stage_name == "association_agent":
            stage_result = self.association.run(self._association_task(task))
            self.records.append(stage_result)
            self.current_association_report = dict(stage_result.get("report") or {})
            self.current_method_id = self.association.resolve_method(self.current_association_report.get("method_id"))
        elif stage_name == "intervention_agent":
            if not self.runtime.can_run_runtime_experiment:
                result = {
                    "stage": stage_name,
                    "report": {
                        "explanation": (
                            "Intervention was not run because "
                            f"{self.runtime.runtime_unavailable_reason or 'runtime experiments are unavailable'}"
                        )
                    },
                }
                self.records.append(result)
                return result
            association_report = dict(self.current_association_report or self._latest_report("association_agent"))
            requested = str(args.get("method_id") or self.current_method_id or "").strip()
            resolved = self.association.resolve_method(requested)
            if not resolved:
                return {"stage": stage_name, "status": "orchestration_error", "error": "Intervention requires one indexed method_id."}
            hypothesis = str(args.get("hypothesis") or "").strip()
            if not hypothesis and resolved == self.association.resolve_method(association_report.get("method_id")):
                hypothesis = str(association_report.get("hypothesis") or "")
            self.current_method_id = resolved
            self.runtime.allowed_method_id = resolved
            stage_result = self.intervention.run(self._intervention_task(
                association_report, task, method_id=resolved, hypothesis=hypothesis,
            ))
            stage_result["method_id"] = resolved
            self.records.append(stage_result)
        else:
            association_report = dict(self.current_association_report or self._latest_report("association_agent"))
            requested = args.get("method_id") or self.current_method_id
            resolved = self.association.resolve_method(requested)
            if requested and not resolved:
                return {"stage": stage_name, "status": "orchestration_error",
                        "error": "Counterfactual method_id must identify an indexed function."}
            stage_result = self.counterfactual.run(self._counterfactual_task(
                association_report, task, method_id=resolved,
            ))
            self.records.append(stage_result)
        return {"stage": stage_name, "report": stage_result.get("report") or {}}

    def validate_final(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload.get("explanation"), str) or not payload["explanation"].strip():
            raise ValueError("Provide a nonempty final explanation.")
        methods = payload.get("ranked_methods")
        if not isinstance(methods, list) or not 1 <= len(methods) <= 10:
            raise ValueError("Return 1 to 10 ranked_methods.")
        resolved = [self.association.resolve_method(mid) if isinstance(mid, str) else None for mid in methods]
        if not all(resolved):
            raise ValueError("ranked_methods must contain exact indexed function IDs.")
        if len(set(resolved)) != len(resolved):
            raise ValueError("ranked_methods must contain distinct functions, without duplicate IDs or aliases.")

    def _fallback_candidates(self, association_report: dict[str, Any]) -> list[str]:
        reports = [association_report]
        # Preserve discovery order within the newest report, then add earlier
        # reports so a failed final turn does not discard useful candidates.
        for record in reversed(self.records):
            if record.get("stage") == "association_agent":
                report = dict(record.get("report") or {})
                if report not in reports:
                    reports.append(report)
        result: list[str] = []
        for report in reports:
            candidates = report.get("candidates") or []
            if not isinstance(candidates, list):
                candidates = []
            for candidate in candidates:
                resolved = self.association.resolve_method(str(candidate))
                if resolved and resolved not in result:
                    result.append(resolved)
                if len(result) == 10:
                    break
            method_id = self.association.resolve_method(report.get("method_id"))
            if method_id and method_id not in result:
                result.insert(0, method_id)
            if len(result) == 10:
                break
        if not result:
            crash_point = self.association.resolve_method(self.association._format_crash_point())
            if crash_point:
                result.append(crash_point)
        return result[:10]

    def run(self) -> tuple[list[str], str]:
        request_budget = self.orchestrator.agent.max_steps - MAX_FINAL_ATTEMPTS
        initial = (
            f"<case>\n{self.association.case_input()}\n</case>\n"
            "Use the causal chain as the default: establish a candidate with Association, test a selected "
            "candidate with Intervention, and compare the resulting explanations with Counterfactual. "
            "If an intervention weakens or rejects a candidate, return to Association for another hypothesis; "
            "skip a stage when its question is already settled and request any specialist again when needed. "
            f"Use each specialist's final report as the next input. You have at most {request_budget} specialist requests "
            f"in total, and at most {self.max_requests_per_stage} requests to each specialist in this case. "
            "Finish with the best supported ranking when evidence is sufficient or the request budget is exhausted."
        )
        record = self.orchestrator.run(initial)
        report = dict(record.get("report") or {})
        try:
            self.validate_final(report)
            ranked = [self.association.resolve_method(mid) for mid in report["ranked_methods"]]
            return [mid for mid in ranked if mid], report["explanation"].strip()
        except (ValueError, TypeError):
            association = self._latest_report("association_agent")
            reason = report.get("explanation") or "No valid final ranking was returned."
            fallback = {
                "ranked_methods": self._fallback_candidates(association),
                "explanation": (
                "Fallback ranking from Association candidates, or the crash point if no candidate was recovered. "
                f"The Orchestrator did not complete a valid final ranking: {reason}"
                ),
            }
            append_log(self.orchestrator.agent.log_path, "Fallback final ranking", fallback)
            return fallback["ranked_methods"], fallback["explanation"]
