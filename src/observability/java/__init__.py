"""Java tracing and runtime probes used by the debug stage."""

from src.observability.java.bridge import apply_observation
from src.observability.java.tracing import collect_java_trace

__all__ = ["apply_observation", "collect_java_trace"]
