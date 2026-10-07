from __future__ import annotations

from pathlib import Path
from typing import Iterable

from src.utils.json_utils import load_jsonl


def candidate_payload(payload: dict[str, object]) -> dict[str, object]:
    candidate = payload.get("candidate")
    return candidate if isinstance(candidate, dict) else {}


def candidate_id_from_payload(payload: dict[str, object]) -> str | None:
    candidate_id = str(candidate_payload(payload).get("candidate_id", "")).strip()
    return candidate_id or None


def candidate_kind_from_payload(payload: dict[str, object]) -> str:
    kind = str(candidate_payload(payload).get("kind", "method"))
    if "." in kind:
        kind = kind.rsplit(".", 1)[-1]
    return kind.lower()


def candidate_method_ids_from_payload(payload: dict[str, object]) -> tuple[str, ...]:
    method_ids = candidate_payload(payload).get("method_ids", [])
    if not isinstance(method_ids, (list, tuple)):
        return ()
    return tuple(str(method_id) for method_id in method_ids if str(method_id).strip())


def candidate_source_from_payload(payload: dict[str, object]) -> str:
    return str(candidate_payload(payload).get("source", "unknown"))


def causal_estimate_payload(payload: dict[str, object]) -> dict[str, object]:
    estimate = payload.get("causal_estimate")
    return estimate if isinstance(estimate, dict) else {}


def explanation_payload(payload: dict[str, object]) -> dict[str, object]:
    explanation = payload.get("explanation")
    return explanation if isinstance(explanation, dict) else {}


def load_ranked_candidate_payloads(
    paths: Iterable[Path],
    *,
    limit_per_path: int | None = None,
) -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = []
    for path in paths:
        payloads.extend(load_jsonl(path, limit=limit_per_path))
    return payloads


def build_ranked_candidate_index(
    paths: Iterable[Path],
    *,
    limit_per_path: int | None = None,
) -> dict[str, dict[str, object]]:
    index: dict[str, dict[str, object]] = {}
    for payload in load_ranked_candidate_payloads(paths, limit_per_path=limit_per_path):
        candidate_id = candidate_id_from_payload(payload)
        if candidate_id is None or candidate_id in index:
            continue
        index[candidate_id] = payload
    return index
