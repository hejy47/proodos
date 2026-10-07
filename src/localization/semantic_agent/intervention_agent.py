from src.localization.semantic_agent.tools.function_tool import function_tool
from src.localization.semantic_agent.agent import Agent, validate_explanation
from src.localization.semantic_agent.prompt import (
    intervention_agent_system_prompt, intervention_agent_few_shot,
)


class InterventionAgent:
    """Test the selected behavioral hypothesis; report the observed outcome."""
    name = "intervention_agent"
    instructions = intervention_agent_system_prompt + "\n" + intervention_agent_few_shot
    max_steps = 5

    def __init__(self, llm_settings, output_dir, test_id, runtime):
        self.runtime = runtime
        self.test_id = str(test_id)
        self.agent = Agent(
            tools=self._build_tools(), system_prompt=self.instructions,
            settings=llm_settings, name=self.name, output_dir=output_dir,
            test_id=self.test_id, max_steps=self.max_steps, validate=self.validate,
            tools_available=lambda: self.runtime.can_run_runtime_experiment,
        )

    def run(self, task):
        return {"stage": self.name, "report": self.agent.run(task)}

    def validate(self, payload):
        validate_explanation(payload)

    def _build_tools(self):
        @function_tool
        def execute_intervention(method_id: str, replacement_function: str) -> str:
            """Test a temporary behavioral change to the suspect against the current failure.

            The backend builds the changed code and reruns the selected test or
            reproducer. Inspect the original and modified outcomes and any build or
            runtime errors. A passing run alone does not establish a correct repair.
            Preserve the target's signature and behavior unrelated to the hypothesis.

            Args:
                method_id: Exact indexed function/method ID of the suspect in the task.
                replacement_function: One complete function or method definition in
                    the target language, including its declaration and modified body.
                    Copy the original declaration, preserving its name, parameters,
                    return type, modifiers, annotations, and exception declarations.
                    Include no enclosing class or additional functions/methods.
            """
            return self.runtime.execute_intervention(method_id, replacement_function)
        return [execute_intervention]
