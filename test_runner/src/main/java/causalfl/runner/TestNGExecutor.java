package causalfl.runner;

import java.io.PrintWriter;
import java.io.StringWriter;
import java.util.Collections;
import org.testng.ITestResult;
import org.testng.TestListenerAdapter;
import org.testng.TestNG;
import org.testng.xml.XmlClass;
import org.testng.xml.XmlInclude;
import org.testng.xml.XmlSuite;
import org.testng.xml.XmlTest;

/**
 * Executes a single TestNG test method.
 *
 * <p>This class is only loaded when a {@code TESTNG} test is requested; TestNG
 * must be on the target project's classpath.
 */
final class TestNGExecutor {
    private TestNGExecutor() {
    }

    static TestExecutionResult run(TestSpec spec) {
        XmlSuite suite = new XmlSuite();
        suite.setName("causalfl");
        XmlTest test = new XmlTest(suite);
        test.setName(spec.testId());
        XmlClass xmlClass = new XmlClass(spec.className);
        xmlClass.setIncludedMethods(Collections.singletonList(new XmlInclude(spec.methodName)));
        test.setXmlClasses(Collections.singletonList(xmlClass));

        TestNG testng = new TestNG();
        testng.setUseDefaultListeners(false);
        testng.setVerbose(0);
        testng.setXmlSuites(Collections.singletonList(suite));
        TestListenerAdapter listener = new TestListenerAdapter();
        testng.addListener(listener);
        testng.run();

        if (!listener.getFailedTests().isEmpty()) {
            ITestResult first = listener.getFailedTests().get(0);
            return TestExecutionResult.failure(stackTraceOf(first.getThrowable()));
        }
        if (listener.getPassedTests().isEmpty() && listener.getSkippedTests().isEmpty()) {
            return TestExecutionResult.infrastructureError(
                "No TestNG test matched " + spec.className + "#" + spec.methodName
            );
        }
        return TestExecutionResult.pass();
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
