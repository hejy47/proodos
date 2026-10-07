"""Agent-facing tool result formatting.

Evidence-first observations for the three-stage localization tools:
- ``probe_function`` / ``trace_functions`` return evidence summaries without
  coaching the agent on causal roles.
- Source-navigation tools may still use the ``format_tool_result`` envelope
  (status / summary / body).
"""

from __future__ import annotations

import re
from typing import Any, Sequence

DEFAULT_TRUNCATE = 1200
CODE_TRUNCATE = 3500
STDOUT_TRUNCATE = 800
OBSERVED_TRUNCATE = 400
MAX_OBSERVED_SAMPLES = 3


def truncate(text: str | None, limit: int = DEFAULT_TRUNCATE) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}\n...[truncated {len(text) - limit} chars]"


def format_tool_result(
    tool: str,
    *,
    status: str,
    summary: str,
    fields: Sequence[str] | None = None,
) -> str:
    """Uniform observation envelope for agent tools.

    Layout::

        ## <tool>
        status: success|empty|error|ambiguous
        summary: <one-line diagnostic takeaway>

        <key fields / payload>
    """
    lines = [
        f"## {tool}",
        f"status: {status}",
        f"summary: {summary}",
    ]
    body = [line for line in (fields or []) if line is not None]
    if body:
        lines.append("")
        lines.extend(body)
    return "\n".join(lines)


def format_observation_result(payload: dict[str, Any]) -> str:
    """Evidence-style probe_function output for the association agent.

    Emphasizes what was seen at each call site; leaves correct/wrong judgment
    to the agent (no propagator coaching).
    """
    method_id = str(payload.get("method_id") or "?")
    tool_status = payload.get("status")
    samples = list(payload.get("samples") or [])
    expressions = [str(e) for e in (payload.get("expressions") or []) if e]
    call_count = int(payload.get("call_count") or 0)
    label_by_key = _observation_label_map(expressions)

    lines = [
        "## Input Observation",
        "",
        "Method:",
        method_id,
        "",
    ]

    if expressions:
        lines.extend(["Requested expressions:", *[f"- {expr}" for expr in expressions], ""])

    if payload.get("probe"):
        lines.extend(["Probe:", str(payload["probe"])])
        if payload.get("probe_type"):
            lines.extend(["Probe type:", str(payload["probe_type"])])
        probe_spec = payload.get("probe_spec")
        if probe_spec:
            lines.extend(["Probe specification:", str(probe_spec)])
        if payload.get("stacktrace"):
            lines.append("Capture: function stack trace enabled")
        fetch_args = payload.get("kprobe_fetch_args") or payload.get("fetch_args")
        if fetch_args:
            lines.extend(["", "Fetch arguments:", str(fetch_args)])
        lines.append("")

    if tool_status != "success":
        lines.append("Observation failed.")
        if payload.get("validation_error"):
            lines.append(truncate(str(payload["validation_error"]), 500))
        if payload.get("error"):
            lines.append(truncate(str(payload["error"]), 800))
        if payload.get("setup_error"):
            lines.append("")
            lines.append("Probe setup diagnostics:")
            lines.append(truncate(str(payload["setup_error"]), 1200))
        stderr = payload.get("stderr")
        if stderr and str(stderr).strip():
            lines.append("")
            lines.append(f"stderr:\n{truncate(str(stderr), 400)}")
        return "\n".join(lines)

    if call_count <= 0:
        lines.append("Observed 0 calls at method entry.")
        return "\n".join(lines)

    shown = samples[:8]
    count_label = f"at least {call_count}" if payload.get("call_count_is_lower_bound") else str(call_count)
    header = f"Observed {count_label} call(s) at method entry:"
    if payload.get("truncated") or len(samples) > len(shown):
        header = (
            f"Observed {count_label} call(s) at method entry "
            f"(showing {len(shown)}):"
        )
    lines.append(header)

    for sample in shown:
        call = sample.get("call")
        lines.append("")
        lines.append(f"Call {call}:")
        values = sample.get("values") or {}
        if not values:
            lines.append("- (no values captured)")
        else:
            for key, value in values.items():
                label = str(key) if str(key) in expressions else label_by_key.get(str(key), str(key))
                lines.append(f"- {label}: {_simplify_observed_value(value)}")
        context = sample.get("context") or {}
        if context:
            lines.append(
                "- context: "
                + ", ".join(
                    f"{key}={_simplify_observed_value(value)}"
                    for key, value in context.items()
                )
            )
        raw = str(sample.get("raw") or "").strip()
        if raw:
            lines.append(f"- raw event: {_simplify_observed_value(raw)}")

    if len(samples) > len(shown):
        lines.append("")
        lines.append(f"...[{len(samples) - len(shown)} more calls omitted]")

    excerpt = payload.get("trace_excerpt") or []
    if excerpt:
        lines.extend(["", "Trace excerpt:", *[truncate(str(line), 240) for line in excerpt[:24]]])

    return "\n".join(lines)


def _short_expression_label(expr: str) -> str:
    """Prefer a short label for long getter chains (e.g. getDataset(...) → dataset)."""
    text = (expr or "").strip()
    if not text:
        return "expr"
    # Outermost method call in the expression (handles nested args).
    matches = list(re.finditer(r"(?:^|\.)([A-Za-z_][A-Za-z0-9_]*)\s*\(", text))
    if matches:
        name = matches[0].group(1)
        for prefix in ("get", "is", "has"):
            if name.startswith(prefix) and len(name) > len(prefix):
                rest = name[len(prefix) :]
                return rest[0].lower() + rest[1:] if rest else name
        return name
    return text


def _simplify_observed_value(value: Any) -> str:
    text = str(value).strip() if value is not None else "null"
    if text == "<unprintable>":
        return "unprintable"
    # Java Object.toString() style: pkg.Class@hex → Class
    obj_match = re.match(r"^([\w.$]+)@([0-9a-fA-F]+)$", text)
    if obj_match:
        return obj_match.group(1).rsplit(".", 1)[-1]
    return truncate(text, 120)


def format_method_cards(
    query: str,
    cards: list[dict[str, Any]],
    *,
    total_matched: int,
) -> str:
    """Candidate cards wrapped as a tool result (legacy helper)."""
    body = format_method_cards_body(query, cards, total_matched=total_matched)
    if not cards:
        return format_tool_result(
            "search_code",
            status="empty",
            summary=f'No method matched query "{query}".',
        )
    status = "success"
    if len(cards) == 1 and cards[0].get("code"):
        summary = (
            f'Found exactly 1 method for query "{query}" — '
            "full source included, no get_method_code call needed."
        )
    else:
        count_word = "method" if len(cards) == 1 else "methods"
        summary = f'Found {len(cards)} candidate {count_word} for query "{query}".'
    return format_tool_result(
        "search_code",
        status=status,
        summary=summary,
        fields=[body],
    )


def format_method_cards_body(
    query: str,
    cards: list[dict[str, Any]],
    *,
    total_matched: int,
) -> str:
    """Method-card body for embedding under ``### Found method``."""
    if not cards:
        return f'No method matched query "{query}".'

    if len(cards) == 1 and cards[0].get("code"):
        card = cards[0]
        fields = [
            f'Found exactly 1 method for query "{query}" — '
            "full source included, no get_method_code call needed.",
            _format_method_card(card, index=1, include_excerpt=False),
            "",
            f"Source:\n```java\n{truncate(str(card['code']), CODE_TRUNCATE)}\n```",
        ]
        observed = card.get("observed") or []
        if observed:
            fields.append(_format_observed_samples(observed))
        return "\n".join(fields)

    count_word = "method" if len(cards) == 1 else "methods"
    fields = [
        f'Found {len(cards)} candidate {count_word} for query "{query}".',
        "",
    ]
    fields.extend(
        _format_method_card(card, index=index)
        for index, card in enumerate(cards, start=1)
    )
    if total_matched > len(cards):
        fields.append(f"Showing {len(cards)} of {total_matched} matched methods.")
    return "\n".join(fields)


def _format_method_card(
    card: dict[str, Any],
    *,
    index: int,
    include_excerpt: bool = True,
) -> str:
    lines = [f"[{index}] method_id: {card.get('method_id', '?')}"]
    location = _format_location(card)
    if location:
        lines.append(f"    location: {location}")
    if card.get("signature"):
        lines.append(f"    signature: {card['signature']}")
    if card.get("match_reason"):
        lines.append(f"    match_reason: {card['match_reason']}")
    if include_excerpt and card.get("excerpt"):
        excerpt = str(card["excerpt"]).replace("\n", "\n    | ")
        lines.append(f"    excerpt:\n    | {excerpt}")
    return "\n".join(lines)


def _format_observed_samples(observed: list[Any]) -> str:
    lines = [
        f"Observed runtime samples ({len(observed)} total, "
        f"showing up to {MAX_OBSERVED_SAMPLES}):"
    ]
    for sample in observed[:MAX_OBSERVED_SAMPLES]:
        lines.append(f"- {truncate(str(sample), OBSERVED_TRUNCATE)}")
    if len(observed) > MAX_OBSERVED_SAMPLES:
        lines.append(f"...[{len(observed) - MAX_OBSERVED_SAMPLES} more samples omitted]")
    return "\n".join(lines)


def format_method_code_result(item: dict[str, Any]) -> str:
    """Complete implementation for get_method_code."""
    fields = [f"method_id: {item.get('method_id', '?')}"]
    location = _format_location(item)
    if location:
        fields.append(f"location: {location}")

    code = item.get("code")
    if code:
        fields.append(f"Source:\n```java\n{truncate(str(code), CODE_TRUNCATE)}\n```")
    else:
        fields.append("Source: (not available for this method)")

    observed = item.get("observed") or []
    if observed:
        fields.append(_format_observed_samples(observed))

    return format_tool_result(
        "get_method_code",
        status="success" if code else "empty",
        summary=f"Full source for {item.get('method_id', '?')}.",
        fields=fields,
    )


def _format_location(item: dict[str, Any]) -> str | None:
    file_path = item.get("file_path")
    if not file_path:
        return None
    start_line = item.get("start_line")
    end_line = item.get("end_line")
    if start_line is not None and end_line is not None:
        return f"{file_path}:{start_line}-{end_line}"
    if start_line is not None:
        return f"{file_path}:{start_line}"
    return str(file_path)
def _observation_label_map(expressions: Sequence[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for expr in expressions:
        key = _observation_sample_key(expr)
        mapping[key] = _short_expression_label(expr)
    return mapping


def _observation_sample_key(expr: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", (expr or "").strip()).strip("_")
    if not cleaned:
        return "expr"
    if cleaned[0].isdigit():
        cleaned = "e_" + cleaned
    return cleaned[:60]
