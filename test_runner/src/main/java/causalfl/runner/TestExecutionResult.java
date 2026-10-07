package causalfl.runner;

/** Outcome of one executed test method. */
final class TestExecutionResult {
    final boolean failed;
    final boolean error;
    final String stackTrace;

    TestExecutionResult(boolean failed, boolean error, String stackTrace) {
        this.failed = failed;
        this.error = error;
        this.stackTrace = stackTrace == null ? "" : stackTrace;
    }

    static TestExecutionResult pass() {
        return new TestExecutionResult(false, false, "");
    }

    static TestExecutionResult failure(String stackTrace) {
        return new TestExecutionResult(true, false, stackTrace);
    }

    static TestExecutionResult infrastructureError(String stackTrace) {
        return new TestExecutionResult(false, true, stackTrace);
    }

    String outcomeLabel() {
        if (error) {
            return "ERROR";
        }
        return failed ? "FAIL" : "PASS";
    }
}
