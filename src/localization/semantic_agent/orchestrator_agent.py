"""Case-scoped orchestration through three input/output request tools."""
from __future__ import annotations

import json

from src.localization.semantic_agent.tools.function_tool import function_tool

from src.localization.semantic_agent.prompt import (
    orchestrator_agent_system_prompt, orchestrator_agent_few_shot,
)
from src.localization.semantic_agent.agent import Agent, validate_explanation


REQUEST_STAGES = {
    "request_association": "association_agent",
    "request_intervention": "intervention_agent",
    "request_counterfactual": "counterfactual_agent",
}


class OrchestratorAgent:
    """Coordinate specialists through explicit input/output request tools.

    A specialist's private messages remain in its own log. The
    only value returned to this agent is the specialist's final report. This
    makes the boundary an ordinary tool call and avoids private worker memory
    being copied into the Orchestrator prompt.
    """

    name = "orchestrator_agent"
    instructions = orchestrator_agent_system_prompt + "\n" + orchestrator_agent_few_shot
    # Reserve two completion requests for a validated final JSON report.
    max_steps = 7

    def __init__(self, llm_settings, output_dir, test_id, workflow):
        self.workflow = workflow
        self.test_id = str(test_id)
        self.agent = Agent(
            tools=self._build_tools(), system_prompt=self.instructions,
            settings=llm_settings, name=self.name, output_dir=output_dir,
            test_id=self.test_id, max_steps=self.max_steps, validate=self.validate,
            tools_available=lambda: any(self._tool_available(name) for name in REQUEST_STAGES),
            tool_available=self._tool_available,
        )

    def _tool_available(self, name: str) -> bool:
        return (
            self.workflow.can_request(REQUEST_STAGES.get(name, ""))
            and (name != "request_intervention" or self.workflow.runtime.can_run_runtime_experiment)
        )

    def _handoff(self, stage_name: str, task: str = "", additional_args=None) -> str:
        result = self.workflow.request(stage_name, task, additional_args)
        return json.dumps(result, ensure_ascii=False, default=str)

    def _build_tools(self):
        @function_tool
        def request_association(question: str = "") -> str:
            """Request one Association report for the current case.

            Args:
                question: The specific missing fact or candidate question to investigate.
            """
            return self._handoff("association_agent", question)

        @function_tool
        def request_intervention(method_id: str, hypothesis: str = "", question: str = "") -> str:
            """Request one Intervention report for one indexed function.

            Args:
                method_id: Exact indexed function ID to test.
                hypothesis: Concrete behavior change and expected effect to test.
                question: The result or uncertainty the test should resolve.
            """
            return self._handoff(
                "intervention_agent", question,
                {"method_id": method_id, "hypothesis": hypothesis},
            )

        @function_tool
        def request_counterfactual(question: str = "", method_id: str = "") -> str:
            """Request one Counterfactual comparison of the current reports.

            Args:
                question: The competing explanation or missing causal fact to compare.
                method_id: Optional exact indexed function associated with the comparison.
            """
            value = str(method_id or "").strip()
            args = {"method_id": value} if value else None
            return self._handoff("counterfactual_agent", question, args)

        return [request_association, request_intervention, request_counterfactual]

    def run(self, task):
        return {"stage": self.name, "report": self.agent.run(task)}

    def validate(self, payload):
        validate_explanation(payload)
        self.workflow.validate_final(payload)
