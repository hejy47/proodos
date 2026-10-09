from src.debug.semantic_agent.agent import Agent, validate_explanation
from src.debug.semantic_agent.prompt import patch_generation_system_prompt
from src.debug.semantic_agent.repair_context import build_repair_context
from src.debug.semantic_agent.tools.repair_ingredients import RepairIngredients


class PatchGenerationAgent:
    """Return one replacement function; the controller owns validation and retries."""

    def __init__(self, llm_settings, output_dir, test_id, method_id, *, preprocess_data=None):
        self.method_id = method_id
        self.test_id = test_id
        self.preprocess_data = preprocess_data
        self.ingredients = RepairIngredients(preprocess_data)
        self.agent = Agent(
            tools=self.ingredients.tools(), system_prompt=patch_generation_system_prompt,
            settings=llm_settings, name="patch_generation_agent", output_dir=output_dir,
            test_id=test_id, max_steps=10, validate=self.validate,
        )

    def validate(self, payload):
        validate_explanation(payload)
        kind = str(payload.get("type") or "patch").strip().lower()
        if kind == "request_diagnosis":
            if not str(payload.get("reason") or "").strip():
                raise ValueError("Return a reason for requesting further diagnosis")
            return
        if kind != "patch":
            raise ValueError("type must be patch or request_diagnosis")
        if payload.get("method_id") != self.method_id:
            raise ValueError("method_id must match the supplied suspect exactly")
        if not isinstance(payload.get("expected_behavior"), str) or not payload["expected_behavior"].strip():
            raise ValueError("Provide a concise expected_behavior grounded in the assertion and available source evidence")
        if not isinstance(payload.get("replacement_function"), str) or not payload["replacement_function"].strip():
            raise ValueError("Return one complete replacement_function")

    def run(self, task):
        context = build_repair_context(self.preprocess_data, self.test_id, self.method_id)
        return self.agent.run(f"{task}\n\n<repair_context>\n{context}\n</repair_context>")
