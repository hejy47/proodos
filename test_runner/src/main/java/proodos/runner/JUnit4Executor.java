package proodos.runner;

import java.io.PrintWriter;
import java.io.StringWriter;
import org.junit.runner.JUnitCore;
import org.junit.runner.Request;
import org.junit.runner.Result;
import org.junit.runner.notification.Failure;

/**
 * Executes a single JUnit 3/4 test method with {@link JUnitCore}.
 * JUnit 3 {@code TestCase} classes are handled transparently by JUnit 4's
 * {@code JUnit38ClassRunner}.
 */
final class JUnit4Executor {
    private JUnit4Executor() {
    }

    static TestExecutionResult run(TestSpec spec) {
        Class<?> testClass;
        try {
            testClass = Class.forName(spec.className, true, Thread.currentThread().getContextClassLoader());
        } catch (Throwable exc) {
            return TestExecutionResult.infrastructureError(stackTraceOf(exc));
        }
        Result result = new JUnitCore().run(Request.method(testClass, spec.methodName));
        if (result.wasSuccessful()) {
            return TestExecutionResult.pass();
        }
        Failure first = result.getFailures().isEmpty() ? null : result.getFailures().get(0);
        String trace = first == null ? "Test failed without failure details" : first.getTrace();
        return TestExecutionResult.failure(trace);
    }

    private static String stackTraceOf(Throwable exc) {
        StringWriter buffer = new StringWriter();
        exc.printStackTrace(new PrintWriter(buffer));
        return buffer.toString();
    }
}
