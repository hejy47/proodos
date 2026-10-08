"""Local functions exposed as OpenAI function schemas with typed arguments."""
from __future__ import annotations

from dataclasses import dataclass
import inspect
from typing import Callable, get_type_hints

from pydantic import BaseModel, ConfigDict, create_model


@dataclass(frozen=True)
class FunctionTool:
    handler: Callable
    arguments: type[BaseModel]

    @property
    def name(self):
        return self.handler.__name__

    @property
    def schema(self):
        return {"type": "function", "function": {
            "name": self.name,
            "description": inspect.getdoc(self.handler) or self.name,
            "parameters": self.arguments.model_json_schema(),
        }}

    def __call__(self, **kwargs):
        values = self.arguments.model_validate(kwargs)
        return self.handler(**values.model_dump())


def function_tool(handler: Callable) -> FunctionTool:
    """Derive the public JSON schema from the function's annotated signature."""
    hints = get_type_hints(handler)
    fields = {
        name: (hints[name], ... if param.default is inspect.Parameter.empty else param.default)
        for name, param in inspect.signature(handler).parameters.items()
    }
    arguments = create_model(handler.__name__ + "Arguments",
                             __config__=ConfigDict(extra="forbid", strict=True), **fields)
    return FunctionTool(handler, arguments)
