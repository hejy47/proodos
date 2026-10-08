# Java patching backend

This package owns complete-method source replacement, temporary compilation,
selected-test validation, and acceptance of a source patch. Production and test
sources are only modified by the explicit patch-validation controller; temporary
interventions compile an override without changing the working tree.

Use `apply_intervention(...)` or `run_intervention(InterventionRequest(...), ...)`
for temporary validation. `replacement_function` must be one complete Java method
whose declaration matches the target; only its body may change.
