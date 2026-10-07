from src.localization.semantic_agent.agent import Agent, validate_explanation
from src.localization.semantic_agent.prompt import patch_generation_system_prompt


class PatchGenerationAgent:
    """Return one replacement function; the controller owns validation and retries."""

    def __init__(self, llm_settings, output_dir, test_id, method_id):
        self.method_id = method_id
        self.agent = Agent(
            tools=[], system_prompt=patch_generation_system_prompt,
            settings=llm_settings, name="patch_generation_agent", output_dir=output_dir,
            test_id=test_id, max_steps=2, validate=self.validate,
        )

    def validate(self, payload):
        validate_explanation(payload)
        if payload.get("method_id") != self.method_id:
            raise ValueError("method_id must match the supplied suspect exactly")
        if not isinstance(payload.get("replacement_function"), str) or not payload["replacement_function"].strip():
            raise ValueError("Return one complete replacement_function")

    def run(self, task):
        return self.agent.run(task)
