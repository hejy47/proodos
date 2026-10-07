from __future__ import annotations

import importlib

from .context import PreprocessContext, load_preprocess_context

__all__ = [
    "BatchPreprocessCase",
    "BatchPreprocessRunner",
    "BatchPreprocessSummary",
    "PreprocessContext",
    "PreprocessRunSummary",
    "PreprocessStageRunner",
    "load_preprocess_context",
    "load_batch_cases",
]


def __getattr__(name: str):
    # Use importlib.import_module instead of "from . import runner" so importing
    # runner does not re-enter __getattr__("runner") while the submodule loads.
    if name in {"BatchPreprocessCase", "BatchPreprocessRunner", "BatchPreprocessSummary", "load_batch_cases"}:
        batch_runner = importlib.import_module(".batch_runner", __package__)
        return getattr(batch_runner, name)
    if name in {"PreprocessRunSummary", "PreprocessStageRunner"}:
        runner_mod = importlib.import_module(".runner", __package__)
        return getattr(runner_mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
