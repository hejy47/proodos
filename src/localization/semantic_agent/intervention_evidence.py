"""Intervention evidence taxonomy for causal localization.

Tool-side fields (compile_error / unsupported / test results) are derived from the
Mockito bridge payload. Agent-side fields (valid vs inert, repairing vs masking,
local_fault_evidence) are judged in the Counterfactual Agent report.
"""

from __future__ import annotations

from typing import Any


INTERVENTION_STATUSES = frozenset({"valid", "inert", "compile_error", "unsupported"})
INTERVENTION_TYPES = frozenset({"repairing", "masking", "none"})


def derive_tool_intervention_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """Map a Mockito bridge payload into tool-derived evidence fields.

    Does not decide ``valid`` vs ``inert`` for successful runs — that requires
    judging whether the stub actually modified and executed the target method.
    """
    status = str(payload.get("status") or "")
    error = str(payload.get("error") or payload.get("validation_error") or "")
    error_l = error.lower()

    original = _pass_fail_label(
        payload.get("original_passed"),
        default="unknown",
    )

    if status == "validation_error":
        return {
            "intervention_status": "unsupported",
            "original_test_result": original,
            "intervention_test_result": "not_run",
            "outcome": None,
            "causal_usable": False,
        }

    if status == "execution_error":
        derived = "compile_error" if _looks_like_compile_error(error_l) else "unsupported"
        return {
            "intervention_status": derived,
            "original_test_result": original,
            "intervention_test_result": "not_run",
            "outcome": None,
            "causal_usable": False,
        }

    if status != "success":
        return {
            "intervention_status": "unsupported",
            "original_test_result": original,
            "intervention_test_result": "not_run",
            "outcome": None,
            "causal_usable": False,
        }

    intervened = _pass_fail_label(payload.get("test_passed"), default="not_run")
    # Successful compile+run: leave status unset for the agent (valid|inert).
    outcome = "crash_disappeared" if payload.get("test_passed") else "still_failing"
    return {
        "intervention_status": None,
        "original_test_result": original if original != "unknown" else "fail",
        "intervention_test_result": intervened,
        "outcome": outcome,
        "causal_usable": None,
    }


def supports_root_cause(evidence: dict[str, Any]) -> bool:
    """True only when intervention evidence may support a root-cause claim."""
    local = evidence.get("local_fault_evidence")
    has_local = isinstance(local, str) and bool(local.strip())
    input_evidence = str(evidence.get("input_evidence") or "unknown").strip().lower()
    if input_evidence == "wrong":
        return False
    return (
        evidence.get("intervention_status") == "valid"
        and evidence.get("intervention_type") == "repairing"
        and evidence.get("target_executed") is True
        and evidence.get("intervention_test_result") == "pass"
        and has_local
    )


def _pass_fail_label(value: Any, *, default: str) -> str:
    if value is True:
        return "pass"
    if value is False:
        return "fail"
    return default


def _looks_like_compile_error(error_l: str) -> bool:
    markers = (
        "compile",
        "javac",
        "cannot find symbol",
        "error:",
        "failed to compile",
    )
    return any(marker in error_l for marker in markers)
