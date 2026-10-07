from src.localization.semantic_agent.agent import Agent, validate_explanation
from src.localization.semantic_agent.prompt import (
    counterfactual_agent_system_prompt, counterfactual_agent_few_shot,
)


class CounterfactualAgent:
    """Assess alternatives; leave routing and ranking to the orchestrator."""
    name = "counterfactual_agent"
    instructions = counterfactual_agent_system_prompt + "\n" + counterfactual_agent_few_shot
    max_steps = 3

    def __init__(self, llm_settings, output_dir, test_id):
        self.test_id = str(test_id)
        self.agent = Agent(
            tools=[], system_prompt=self.instructions, settings=llm_settings,
            name=self.name, output_dir=output_dir, test_id=self.test_id,
            max_steps=self.max_steps, validate=self.validate,
        )

    def run(self, task):
        return {"stage": self.name, "report": self.agent.run(task)}

    def validate(self, payload):
        validate_explanation(payload)
        if any(key in payload for key in ("ranked_methods", "next_action", "question")):
            raise ValueError("Return counterfactual analysis in explanation; the orchestrator owns routing and ranking.")
