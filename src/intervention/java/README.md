# Java intervention and observation

This backend supports two operations:

- `apply_intervention(project_root=..., test_id=..., method_id=..., replacement_function=...)`
  compares the original test outcome with one complete replacement Java method.
- `apply_observation(project_root=..., test_id=..., method_id=..., probe_spec=...)`
  records explicitly selected method-entry expressions while running the test.

Both operations compile a temporary copy of production source and prepend its
classes to the test classpath. Production and test source files are not rewritten
in the working tree.

`replacement_function` includes the original declaration and the modified body,
for example `public int count() { return 1; }`. Preserve the original name,
parameters, return type, modifiers, annotations, generics, and `throws` clause.
Whitespace and comments may differ. Body-only input, extra members, and changed
declarations are rejected before test execution. The definition replaces the
target directly; `super` retains its normal Java meaning. The size limit is
50,000 characters and 1,000 nonempty lines to accommodate complete methods.

`probe_spec` is required and accepts a JSON object or JSON string:

```json
{"type":"entry","expressions":["in","this.repository","this.repository == null"],"max_calls":20}
```

Only the listed expressions are evaluated; parameters and `this` are not added
automatically. Expressions can reference entry parameters and accessible fields.
Method calls must be known to have no side effects; assignments, increments,
allocations, and statements are rejected. Java compilation reports expressions
that reference unavailable names. Return/line probes are currently unsupported.
The list must contain 1–32 expressions, each at most 500 characters.

Samples use exact expression labels and textual values (up to 200 characters).
An expression that throws produces `<unprintable>`. Object rendering uses
`String.valueOf`, so it can invoke application `toString()` methods. `max_calls`
defaults to 20 and accepts 1–100; when more calls occur, the result marks truncation
and reports a lower bound on the call count rather than an exact total.

Use `run_intervention(InterventionRequest(...), project_root=...)` for the lower-level
API. There is no mode selector: all interventions use source replacement, including
those targeting static, private, or ordinary instance methods.

This package replaces `src/intervention/mockito`. The Mockito modes, test-rewriting
implementation, and legacy mock/JSON APIs have been removed. Callers should use
`replacement_function` with a complete method definition; result snippets are named
`generated_source_code` and `generated_source_path`.
