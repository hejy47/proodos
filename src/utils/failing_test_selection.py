from __future__ import annotations

import random
from collections.abc import Callable
from typing import TypeVar

MAX_INSTRUMENTATION_FAILING_TESTS = 5

T = TypeVar("T")


def is_instrumentable_test_id(test_id: str, *, method_name: str = "") -> bool:
    cleaned = test_id.strip()
    if not cleaned or "::" not in cleaned:
        return False
    if cleaned.endswith("::*") or "::*" in cleaned:
        return False
    method = method_name.strip() or cleaned.split("::", 1)[-1]
    return bool(method) and method != "*"


def selection_seed_from_key(key: str) -> int:
    return hash(key) & 0xFFFFFFFF


def select_failing_tests(
    failing_tests: list[T],
    *,
    max_count: int = MAX_INSTRUMENTATION_FAILING_TESTS,
    class_name_fn: Callable[[T], str],
    test_id_fn: Callable[[T], str],
    seed: int | None = None,
) -> list[T]:
    if len(failing_tests) <= max_count:
        return list(failing_tests)

    rng = random.Random(seed)
    by_class: dict[str, list[T]] = {}
    for test in failing_tests:
        by_class.setdefault(class_name_fn(test), []).append(test)

    selected: list[T] = []
    selected_test_ids: set[str] = set()
    while len(selected) < max_count:
        added_this_round = False
        class_names = list(by_class.keys())
        rng.shuffle(class_names)
        for class_name in class_names:
            if len(selected) >= max_count:
                break
            available = [
                test
                for test in by_class[class_name]
                if test_id_fn(test) not in selected_test_ids
            ]
            if not available:
                continue
            test = rng.choice(available)
            selected.append(test)
            selected_test_ids.add(test_id_fn(test))
            added_this_round = True
        if not added_this_round:
            break
    return selected
