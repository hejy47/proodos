"""OpenAI SDK client construction and localization token accounting."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock

from openai import OpenAI

from config import LLMSettings


@dataclass
class UsageTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0
    _lock: Lock = field(default_factory=Lock, repr=False)

    @property
    def total_tokens(self):
        return self.input_tokens + self.output_tokens

    def record(self, usage):
        with self._lock:
            self.requests += 1
            if usage is not None:
                self.input_tokens += usage.prompt_tokens
                self.output_tokens += usage.completion_tokens


_usage: ContextVar[UsageTotals | None] = ContextVar("localization_usage", default=None)


@contextmanager
def collect_usage():
    totals = UsageTotals()
    token = _usage.set(totals)
    try:
        yield totals
    finally:
        _usage.reset(token)


def record_usage(usage):
    totals = _usage.get()
    if totals is not None:
        totals.record(usage)


def create_client(settings: LLMSettings) -> OpenAI:
    missing = [name for name, value in (("api_key", settings.api_key), ("model", settings.model))
               if not isinstance(value, str) or not value.strip()]
    if missing:
        raise ValueError(f"LLM disabled: missing {', '.join(missing)}")
    return OpenAI(api_key=settings.api_key, base_url=settings.base_url,
                  timeout=settings.request_timeout, max_retries=0)
