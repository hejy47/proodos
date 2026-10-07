"""Readable agent transcripts; SDK response fields are logged separately."""
from __future__ import annotations

import json
from pathlib import Path


def append_log(path: Path, title: str, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n[{title}]\n{text}\n")


def log_completion(path: Path, step: int, message) -> None:
    parts = []
    reasoning = getattr(message, "reasoning_content", None)
    if reasoning:
        parts.append(f"Reasoning:\n{reasoning}")
    if message.content:
        parts.append(f"Response:\n{message.content}")
    if parts:
        append_log(path, f"Step {step}", "\n\n".join(parts))
