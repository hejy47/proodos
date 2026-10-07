"""On-demand kernel tracing used by the localization agents.

The expensive part of kernel tracing is deliberately kept behind an explicit
tool call.  A caller supplies a small set of symbols selected from the static
fault context; the reproducer is then run once in the case VM and the trace
buffer is returned as navigation evidence.
"""

from __future__ import annotations

from collections import Counter
import json
import os
from pathlib import Path
import re
from typing import Iterable
from src.utils.output_paths import kernel_runtime_dir

from src.intervention.c_kernel.qemu import run_qemu_case


_SYMBOL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.$]*$")
_KPROBE_LINE_RE = re.compile(r"\bcfl_input:\s*(.*)$")
_TRACE_CONTEXT_RE = re.compile(
    r"^(?P<comm>.+)-(?P<pid>\d+)\s+\[(?P<cpu>\d+)\]\s+.*?\s(?P<timestamp>\d+\.\d+):"
)

# Runtime tools have independent limits.  The localization stage itself has
# no wall-clock deadline; a slow or stuck experiment is reported to the agent
# as a tool error and the agent can continue with static evidence.
TRACE_REPRO_TIMEOUT_SECONDS = int(os.environ.get("CAUSALFL_TRACE_REPRO_TIMEOUT_SECONDS", "120"))
TRACE_TOOL_TIMEOUT_SECONDS = int(os.environ.get("CAUSALFL_TRACE_TOOL_TIMEOUT_SECONDS", "600"))
DEFAULT_TRACE_MAX_EVENTS = 1000
MAX_TRACE_EVENTS = 5000
TRACE_OUTPUT_EVENT_LIMIT = 200


def _qemu_failure(vm_output: str) -> str | None:
    """Return a stable runtime error when the case VM never became usable.

    ``run_qemu_case`` deliberately returns captured shell output so callers can
    inspect the serial log.  The explicit status marker lets tracing tools
    distinguish a missing event from a failed boot or a disconnected guest.
    """
    text = str(vm_output or "")
    match = re.search(r"CFL_QEMU_STATUS=([A-Za-z0-9_.-]+)", text)
    if match is None:
        return None
    status = match.group(1)
    if status != "booted":
        return {
            "guest_crash": "guest kernel crashed before SSH became available",
            "boot_timeout": "guest did not become reachable over SSH before the boot timeout",
            "execution_timeout": "QEMU execution exceeded the tool timeout",
        }.get(status, f"QEMU run failed before observation ({status})")
    exec_match = re.search(r"CFL_EXEC_STATUS=(\d+)", text)
    if exec_match is None:
        return None
    exec_status = int(exec_match.group(1))
    if exec_status == 0:
        return None
    if exec_status == 124:
        return "guest reproducer timed out before the probe result was collected"
    return f"guest reproducer/SSH command exited with status {exec_status}"


def _probe_setup_failure(vm_output: str, probe: str) -> str | None:
    """Explain probe registration failures recorded in the guest log."""
    text = str(vm_output or "")
    if probe == "kprobe":
        if "CFL_KPROBE_DEFINE_FAILED" in text:
            return "kprobe event could not be registered for the selected function"
        if "CFL_KPROBE_EVENT_MISSING" in text:
            return "kprobe event is unavailable for the selected function (possibly inlined or not emitted)"
        if "CFL_KPROBE_ENABLE_FAILED" in text:
            return "kprobe event was registered but could not be enabled"
    return None


def parse_probe_spec(spec: str | dict | None) -> dict:
    """Normalize the agent's declarative kprobe definition.

    The specification is intentionally data-only; it is translated to a
    tracefs kprobe event and never compiled as kernel code.  JSON is preferred
    so an agent can request richer observations without inventing shell
    syntax::

        {"type": "entry", "fetch": ["arg1=$arg1:x64"],
         "stacktrace": true}

    A plain fetch-argument string remains accepted for small/manual calls.
    """
    if isinstance(spec, dict):
        value = dict(spec)
    else:
        text = str(spec or "").strip()
        if not text:
            value = {}
        else:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                value = {"fetch": text}
            else:
                if not isinstance(parsed, dict):
                    raise ValueError("probe_spec JSON must be an object")
                value = parsed
    probe_type = str(value.get("type", value.get("kind", "entry"))).strip().lower()
    if probe_type not in {"entry", "return"}:
        raise ValueError("probe_spec type must be entry or return")
    fetch = value.get("fetch", value.get("fetch_args", ""))
    if isinstance(fetch, (list, tuple)):
        fetch = " ".join(str(item).strip() for item in fetch if str(item).strip())
    elif fetch is None:
        fetch = ""
    else:
        fetch = str(fetch).strip()
    if "->" in fetch or "<-" in fetch:
        raise ValueError(
            "probe_spec does not support C-style field access (->); use tracefs "
            "offset syntax such as +8($arg2):x8 or +16(%si):x64"
        )
    if not fetch and probe_type == "return":
        fetch = "retval=$retval:x64"
    stacktrace = value.get("stacktrace", value.get("capture_stack", False))
    if isinstance(stacktrace, str):
        stacktrace = stacktrace.strip().lower() in {"1", "true", "yes", "on"}
    return {
        "type": probe_type,
        "fetch": fetch,
        "stacktrace": bool(stacktrace),
    }


def _symbol_from_method_id(method_id: str) -> str:
    symbol = str(method_id or "").rsplit("#", 1)[-1].strip()
    if not _SYMBOL_RE.fullmatch(symbol):
        raise ValueError(f"invalid kernel function id: {method_id}")
    return symbol


def _read_trace(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _trace_event_signature(line: str) -> str:
    """Return the stable portion of an ftrace event for deduplication."""
    match = re.search(r"\d+(?:\.\d+)?:\s+(.*)$", line.strip())
    return match.group(1) if match else line.strip()


def collect_ftrace(
    *,
    project,
    test_id: str,
    method_ids: Iterable[str],
    max_events: int = DEFAULT_TRACE_MAX_EVENTS,
) -> dict:
    """Run the case reproducer with ftrace filtered to selected functions."""
    if project is None or not getattr(project, "kernel", None):
        return {"status": "execution_error", "error": "kernel project is unavailable"}
    symbols: list[str] = []
    ids: list[str] = []
    for method_id in method_ids:
        symbol = _symbol_from_method_id(method_id)
        if symbol not in symbols:
            symbols.append(symbol)
            ids.append(str(method_id))
    if not symbols:
        return {"status": "validation_error", "error": "at least one method_id is required"}
    if len(symbols) > 16:
        return {"status": "validation_error", "error": "at most 16 functions may be traced per call"}
    if not 1 <= int(max_events) <= MAX_TRACE_EVENTS:
        return {
            "status": "validation_error",
            "error": f"max_events must be between 1 and {MAX_TRACE_EVENTS}",
        }

    try:
        vm_output = run_qemu_case(
            getattr(project, "docker_container", None),
            str(test_id),
            repro_timeout=TRACE_REPRO_TIMEOUT_SECONDS,
            scan_wait=10,
            ftrace_functions=symbols,
            qemu_timeout=TRACE_TOOL_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # pragma: no cover - runtime-specific failure
        return {"status": "execution_error", "error": f"ftrace run failed: {exc}", "requested": ids}
    runtime_error = _qemu_failure(vm_output)
    if runtime_error:
        return {
            "status": "execution_error",
            "error": runtime_error,
            "probe": "ftrace",
            "requested": ids,
        }
    probe_error = _probe_setup_failure(vm_output, "ftrace")
    if probe_error:
        return {
            "status": "execution_error",
            "error": probe_error,
            "probe": "ftrace",
            "requested": ids,
        }

    repro_dir = kernel_runtime_dir(str(test_id))
    trace_path = repro_dir / "ftrace.log"
    trace = _read_trace(trace_path)
    counts = Counter()
    raw_event_count = 0
    # Keep one representative for each stable event signature. Counts are
    # accumulated over the complete trace, so noisy early events cannot hide
    # a selected function that appears later in the buffer.
    representatives: dict[str, list[object]] = {}
    for line in trace.splitlines():
        if any(re.search(rf"\b{re.escape(symbol)}(?:\+0x[0-9a-f]+)?\b", line) for symbol in symbols):
            raw_event_count += 1
            for symbol in symbols:
                if re.search(rf"\b{re.escape(symbol)}(?:\+0x[0-9a-f]+)?\b", line):
                    counts[symbol] += 1
            signature = _trace_event_signature(line)
            if signature in representatives:
                representatives[signature][1] = int(representatives[signature][1]) + 1
            elif len(representatives) < int(max_events):
                representatives[signature] = [line.strip(), 1]
    events = [
        first if count == 1 else f"{first} [repeated {count} times]"
        for first, count in representatives.values()
    ]
    # A trace buffer normally contains headers even when none of the selected
    # functions ran.  Treat that case as inconclusive rather than claiming a
    # successful observation; the agent must distinguish "trace available"
    # from "selected event observed".
    return {
        "status": "success" if raw_event_count else "incomplete",
        "probe": "ftrace",
        "requested": ids,
        "symbols": symbols,
        "observed": [symbol for symbol in symbols if counts[symbol]],
        "counts": dict(counts),
        "events": events,
        "event_count": raw_event_count,
        "returned_event_count": len(events),
        "compacted": len(events) < raw_event_count,
        "trace_available": bool(trace),
        "trace_path": str(trace_path),
    }


def collect_kprobe_trace(
    *,
    project,
    test_id: str,
    method_id: str,
    fetch_args: str = "",
    probe_spec: str | dict | None = None,
    max_samples: int = 8,
) -> dict:
    """Run an agent-defined kprobe and return captured samples."""
    try:
        symbol = _symbol_from_method_id(method_id)
    except ValueError as exc:
        return {"status": "validation_error", "error": str(exc), "method_id": method_id}
    if not 1 <= int(max_samples) <= 64:
        return {"status": "validation_error", "error": "max_samples must be between 1 and 64", "method_id": method_id}
    try:
        spec = parse_probe_spec(probe_spec if probe_spec is not None else fetch_args)
    except ValueError as exc:
        return {"status": "validation_error", "error": str(exc), "method_id": method_id}
    try:
        vm_output = run_qemu_case(
            getattr(project, "docker_container", None),
            str(test_id),
            repro_timeout=TRACE_REPRO_TIMEOUT_SECONDS,
            scan_wait=10,
            kprobe_function=symbol,
            kprobe_fetch_args=spec["fetch"],
            kprobe_type=spec["type"],
            kprobe_stacktrace=spec["stacktrace"],
            qemu_timeout=TRACE_TOOL_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # pragma: no cover - runtime-specific failure
        return {"status": "execution_error", "error": f"kprobe run failed: {exc}", "method_id": method_id}
    runtime_error = _qemu_failure(vm_output)
    if runtime_error:
        return {
            "status": "execution_error",
            "error": runtime_error,
            "probe": "kprobe",
            "method_id": method_id,
            "probe_spec": spec,
        }
    probe_error = _probe_setup_failure(vm_output, "kprobe")
    if probe_error:
        return {
            "status": "execution_error",
            "error": probe_error,
            "probe": "kprobe",
            "method_id": method_id,
            "probe_spec": spec,
        }

    repro_dir = kernel_runtime_dir(str(test_id))
    setup_error = _read_trace(repro_dir / "kprobe_setup.log").strip()
    if setup_error:
        return {
            "status": "execution_error",
            "error": f"kprobe registration/setup failed: {setup_error[:1000]}",
            "probe": "kprobe",
            "method_id": method_id,
            "probe_spec": spec,
            "setup_error": setup_error[:4000],
        }
    trace_path = repro_dir / "kprobe.log"
    trace = _read_trace(trace_path)
    samples = []
    for line in trace.splitlines():
        match = _KPROBE_LINE_RE.search(line)
        if match is None:
            continue
        values: dict[str, str] = {}
        for pair in match.group(1).split():
            key, sep, value = pair.partition("=")
            if sep and key:
                values[key] = value
        context = _TRACE_CONTEXT_RE.search(line)
        samples.append({
            "call": len(samples) + 1,
            "method": symbol,
            "values": values,
            "context": context.groupdict() if context else {},
            "raw": line.strip(),
        })
        if len(samples) >= int(max_samples):
            break
    return {
        "status": "success" if samples else "incomplete",
        "probe": "kprobe",
        "method_id": method_id,
        "symbol": symbol,
        "probe_spec": spec,
        "probe_type": spec["type"],
        "fetch_args": spec["fetch"] or "default six register arguments",
        "stacktrace": spec["stacktrace"],
        "samples": samples,
        "call_count": len(samples),
        "trace_available": bool(trace),
        "trace_path": str(trace_path),
        "trace_excerpt": [
            line.strip() for line in trace.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ][:64],
    }


def format_ftrace_result(result: dict) -> str:
    lines = ["## Kernel Function Trace", f"status: {result.get('status', 'unknown')}", "probe: ftrace"]
    if result.get("error"):
        lines.extend(["", f"error: {result['error']}"])
        return "\n".join(lines)
    returned = int(result.get("returned_event_count", len(result.get("events") or [])))
    shown = min(returned, TRACE_OUTPUT_EVENT_LIMIT)
    lines.extend([
        "",
        "Requested functions:",
        *[f"- {item}" for item in result.get("requested", [])],
        "",
        f"Observed events: {result.get('event_count', 0)} "
        f"(showing {shown} of {returned} compact patterns)",
        "Observed function counts:",
    ])
    counts = result.get("counts") or {}
    lines.extend(f"- {name}: {count}" for name, count in sorted(counts.items()))
    events = result.get("events") or []
    if events:
        lines.extend(["", "Trace events (compacted):", *events[:TRACE_OUTPUT_EVENT_LIMIT]])
        if len(events) > TRACE_OUTPUT_EVENT_LIMIT:
            lines.append(f"... {len(events) - TRACE_OUTPUT_EVENT_LIMIT} compact patterns omitted; counts include them")
    if not result.get("trace_available"):
        lines.extend(["", "The trace buffer was unavailable; absence is inconclusive."])
    return "\n".join(lines)


def format_kprobe_result(result: dict) -> str:
    lines = ["## Kernel Input Observation", f"status: {result.get('status', 'unknown')}", "probe: kprobe"]
    if result.get("error"):
        lines.extend(["", f"error: {result['error']}"])
        return "\n".join(lines)
    lines.extend(["", "Method:", str(result.get("method_id") or "?"),
                  "", f"Probe type: {result.get('probe_type', 'entry')}"])
    if result.get("stacktrace"):
        lines.append("Capture: function stack trace enabled")
    lines.extend(["", f"Observed {result.get('call_count', 0)} call(s):"])
    for sample in result.get("samples") or []:
        lines.extend(["", f"Call {sample.get('call')}:"])
        values = sample.get("values") or {}
        lines.extend(f"- {key}: {value}" for key, value in values.items())
        if not values:
            lines.append("- (no values captured)")
        context = sample.get("context") or {}
        if context:
            lines.append("- context: " + ", ".join(f"{key}={value}" for key, value in context.items()))
        raw = str(sample.get("raw") or "").strip()
        if raw:
            lines.append(f"- raw event: {raw[:320]}")
    if not result.get("trace_available"):
        lines.extend(["", "The kprobe trace buffer was unavailable; absence is inconclusive."])
    elif result.get("trace_excerpt"):
        lines.extend(["", "Trace excerpt:", *result["trace_excerpt"][:24]])
    return "\n".join(lines)
