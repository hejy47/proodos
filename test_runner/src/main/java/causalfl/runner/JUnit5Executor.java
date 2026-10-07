package causalfl.runner;

import java.io.PrintWriter;
import java.io.StringWriter;
import org.junit.platform.engine.discovery.DiscoverySelectors;
import org.junit.platform.launcher.Launcher;
import org.junit.platform.launcher.LauncherDiscoveryRequest;
import org.junit.platform.launcher.core.LauncherDiscoveryRequestBuilder;
import org.junit.platform.launcher.core.LauncherFactory;
import org.junit.platform.launcher.listeners.SummaryGeneratingListener;
import org.junit.platform.launcher.listeners.TestExecutionSummary;

/**
 * Executes a single JUnit 5 (Jupiter) test method via the platform launcher.
 *
 * <p>This class is only loaded when a {@code JUNIT5} test is requested; the
 * platform launcher plus an engine must be on the target project's classpath.
 */
final class JUnit5Executor {
    private JUnit5Executor() {
    }

    static TestExecutionResult run(TestSpec spec) {
        LauncherDiscoveryRequest request = LauncherDiscoveryRequestBuilder.request()
            .selectors(DiscoverySelectors.selectMethod(spec.className, spec.methodName))
            .build();
        Launcher launcher = LauncherFactory.create();
        SummaryGeneratingListener listener = new SummaryGeneratingListener();
        launcher.registerTestExecutionListeners(listener);
        launcher.execute(request);
        TestExecutionSummary summary = listener.getSummary();
        if (summary.getTotalFailureCount() == 0) {
            if (summary.getTestsStartedCount() == 0) {
                return TestExecutionResult.infrastructureError(
                    "No JUnit 5 test matched " + spec.className + "#" + spec.methodName
                );
            }
            return TestExecutionResult.pass();
        }
        TestExecutionSummary.Failure first = summary.getFailures().get(0);
        return TestExecutionResult.failure(stackTraceOf(first.getException()));
    }

    private static String stackTraceOf(Throwable exc) {
        if (exc == null) {
            return "Test failed without failure details";
        }
        StringWriter buffer = new StringWriter();
        exc.printStackTrace(new PrintWriter(buffer));
        return buffer.toString();
    }
}
