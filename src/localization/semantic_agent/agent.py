"""Bounded tool-use loop using OpenAI chat completions directly.

Each invocation owns its conversation. Only its validated JSON report is
returned to the caller; intermediate messages stay in the agent's log.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import time

from src.utils import llm_util
from src.utils.agent_logging import append_log, log_completion


MAX_FINAL_ATTEMPTS = 2


def parse_json_response(content: str | None) -> object:
    """Decode a final JSON object, accepting an optional Markdown code fence.

    JSON mode normally returns the object directly. A few OpenAI-compatible
    endpoints still wrap it in prose or a `````json`` block, so final-output
    parsing handles those presentation wrappers without treating prose as a
    tool call.
    """
    text = str(content or "").strip()
    if not text:
        raise ValueError("The response was empty")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fenced = re.findall(r"```(?:json)?\s*\n?(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
    candidates = fenced or [text]
    decoder = json.JSONDecoder()
    decoded = []
    for candidate in candidates:
        for match in re.finditer(r"\{", candidate):
            try:
                value, _ = decoder.raw_decode(candidate[match.start():])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                decoded.append(value)
    if decoded:
        return decoded[-1]
    raise ValueError("No JSON object found in the response")


def validate_explanation(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("explanation"), str) or not payload["explanation"].strip():
        raise ValueError("Provide a JSON object with a nonempty explanation grounded in the available evidence.")


class Agent:
    def __init__(self, *, tools, system_prompt, settings, name, output_dir,
                 test_id, max_steps, validate, tools_available=None,
                 tool_available=None):
        if max_steps < 2:
            raise ValueError("max_steps must reserve at least two completion requests")
        self.settings = settings
        self.name = name
        self.max_steps = max_steps
        self.validate = validate
        self.tools_available = tools_available or (lambda: True)
        self.tool_available = tool_available or (lambda _name: True)
        self.tools = {tool.name: tool for tool in tools}
        if len(self.tools) != len(tools):
            raise ValueError("Duplicate tool name")
        self.system_prompt = system_prompt
        safe_id = re.sub(r"[^\w.\-]+", "_", str(test_id))
        self.log_path = Path(output_dir) / f"{name}_{safe_id}.log"
        self.last_usage = {}

    def run(self, task: str) -> dict:
        messages = [{"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": task}]
        append_log(self.log_path, "Task", task)
        usage = llm_util.UsageTotals()
        started = time.monotonic()
        finalizing = not self.tools
        final_attempts = 0
        error = "Completion budget exhausted before a valid JSON report was returned."
        try:
            with llm_util.create_client(self.settings) as client:
                for step in range(1, self.max_steps + 1):
                    if not finalizing and (step > self.max_steps - MAX_FINAL_ATTEMPTS or not self.tools_available()):
                        finalizing = True
                        messages.append({"role": "user", "content":
                            "The tool request budget is exhausted. Return your final JSON report now, "
                            "using the evidence already collected. State any remaining uncertainty."})
                    kwargs = {"model": self.settings.model, "messages": messages,
                              "max_tokens": self.settings.max_tokens,
                              "temperature": self.settings.temperature}
                    if self.settings.provider == "deepseek" and self.settings.thinking is not None:
                        kwargs["extra_body"] = {"thinking": {"type": "enabled" if self.settings.thinking else "disabled"}}
                    if finalizing:
                        kwargs["response_format"] = {"type": "json_object"}
                        final_attempts += 1
                    else:
                        kwargs["tools"] = [
                            tool.schema for name, tool in self.tools.items()
                            if self.tool_available(name)
                        ]
                    response = client.chat.completions.create(**kwargs)
                    usage.record(response.usage)
                    llm_util.record_usage(response.usage)
                    if not response.choices:
                        raise ValueError("Provider returned no completion choices")
                    choice = response.choices[0]
                    message = choice.message
                    # Preserve the SDK message, including reasoning_content.
                    # DeepSeek requires it on subsequent tool-use requests.
                    messages.append(message.model_dump(exclude_none=True))
                    log_completion(self.log_path, step, message)
                    if message.tool_calls:
                        self._execute_calls(message.tool_calls, messages, step,
                                            disabled=finalizing or choice.finish_reason == "length")
                        if not finalizing:
                            continue
                        error = "Provider returned tool calls when a final JSON report was requested."
                    else:
                        try:
                            if choice.finish_reason == "length":
                                raise ValueError("Completion was truncated at the output token limit")
                            payload = parse_json_response(message.content)
                            self.validate(payload)
                            if isinstance(payload, dict) and payload.get("status") == "incomplete":
                                raise ValueError("Return the requested report fields and explain uncertainty")
                            append_log(self.log_path, "Final report", payload)
                            return payload
                        except (ValueError, TypeError) as exc:
                            error = f"Invalid final JSON report: {exc}"
                    append_log(self.log_path, "Output correction", error)
                    if final_attempts >= MAX_FINAL_ATTEMPTS:
                        break
                    finalizing = True
                    messages.append({"role": "user", "content":
                        f"{error}. Return only the final JSON object described in the system prompt, "
                        "based on the evidence you have. Include unresolved facts in explanation."})
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            append_log(self.log_path, "Run error", error)
        finally:
            self.last_usage = {"agent": self.name, "requests": usage.requests,
                               "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
                               "duration_seconds": round(time.monotonic() - started, 2)}
            with (self.log_path.parent / "agent_runs.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(self.last_usage) + "\n")
        report = {"status": "incomplete", "explanation": error}
        append_log(self.log_path, "Incomplete report", report)
        return report

    def _execute_calls(self, calls, messages, step, *, disabled):
        # Run calls synchronously in response order, including runtime experiments.
        for call in calls:
            name = call.function.name
            append_log(self.log_path, f"Step {step} tool {name} ({call.id})", call.function.arguments)
            try:
                if disabled:
                    result = {"status": "skipped", "error": "No tool executed: finalization or truncated response."}
                elif not self.tool_available(name):
                    result = {"status": "skipped", "error": "This tool is unavailable for the remainder of the case. Continue with the evidence already collected."}
                else:
                    tool = self.tools.get(name)
                    if tool is None:
                        raise ValueError(f"Unknown tool: {name}")
                    arguments = json.loads(call.function.arguments)
                    if not isinstance(arguments, dict):
                        raise ValueError("Tool arguments must be a JSON object")
                    result = tool(**arguments)
            except Exception as exc:
                result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
            content = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
            # Pair every native tool_call_id, including errors and skipped calls.
            messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
            append_log(self.log_path, f"Tool result {name} ({call.id})", content)
