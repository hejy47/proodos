# Java observability backend

This package owns on-demand Java tracing and explicit method-entry probes used
by the debug stage. Probe expressions are validated before a temporary source
override is compiled; tracing uses the built Java trace agent.
