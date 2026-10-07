package causalfl.runner;

import java.io.File;
import java.io.PrintWriter;
import java.io.StringWriter;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.FutureTask;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.regex.Pattern;

/**
 * CausalFL test runner CLI. Replaces the GZoltar CLI for test execution and
 * discovery while keeping the same on-disk contracts.
 *
 * <pre>
 * runTests --testMethods tests.txt [--perTestTimeout seconds]
 * discoverTests classesRoot --outputFile out.txt [--includes glob[:glob...]]
 * </pre>
 *
 * <p>Test method file lines follow {@code FRAMEWORK,Class#method} with
 * framework one of {@code JUNIT} (JUnit 3/4), {@code JUNIT5}, {@code TESTNG}.
 * Test outcomes are reported to the trace agent (when attached) which persists
 * spectra/tests/trace/contract artifacts; failing tests do not affect the exit
 * code, only runner infrastructure errors do.
 */
public final class TestRunnerMain {
    static final long DEFAULT_PER_TEST_TIMEOUT_SECONDS = 300L;

    private TestRunnerMain() {
    }

    public static void main(String[] args) throws Exception {
        if (args.length == 0) {
            printUsage();
            System.exit(2);
        }
        String subcommand = args[0];
        if ("runTests".equals(subcommand)) {
            System.exit(runTests(args));
        } else if ("discoverTests".equals(subcommand)) {
            System.exit(discoverTests(args));
        } else {
            printUsage();
            System.exit(2);
        }
    }

    private static int runTests(String[] args) throws Exception {
        Path testsFile = null;
        long perTestTimeoutSeconds = DEFAULT_PER_TEST_TIMEOUT_SECONDS;
        for (int index = 1; index < args.length; index++) {
            if ("--testMethods".equals(args[index]) && index + 1 < args.length) {
                testsFile = Paths.get(args[++index]);
            } else if ("--perTestTimeout".equals(args[index]) && index + 1 < args.length) {
                perTestTimeoutSeconds = Long.parseLong(args[++index]);
            }
        }
        if (testsFile == null || !Files.isRegularFile(testsFile)) {
            System.err.println("[causalfl-runner] missing or unreadable --testMethods file: " + testsFile);
            return 2;
        }

        List<TestSpec> specs = new ArrayList<TestSpec>();
        for (String rawLine : Files.readAllLines(testsFile, StandardCharsets.UTF_8)) {
            TestSpec spec = TestSpec.parse(rawLine);
            if (spec != null) {
                specs.add(spec);
            }
        }
        if (specs.isEmpty()) {
            System.err.println("[causalfl-runner] no test methods parsed from " + testsFile);
            return 2;
        }
        if (!TraceBridge.isAgentPresent()) {
            System.err.println("[causalfl-runner] trace agent not detected; outcomes will only be printed");
        }

        int executed = 0;
        for (TestSpec spec : specs) {
            executeOne(spec, perTestTimeoutSeconds);
            executed++;
        }
        System.out.println("[causalfl-runner] executed " + executed + " test(s)");
        return 0;
    }

    private static void executeOne(TestSpec spec, long perTestTimeoutSeconds) {
        final String testId = spec.testId();
        System.out.println("[causalfl-runner] running " + spec);
        long startedAtNanos = System.nanoTime();

        final TestSpec currentSpec = spec;
        FutureTask<TestExecutionResult> task = new FutureTask<TestExecutionResult>(
            new java.util.concurrent.Callable<TestExecutionResult>() {
                @Override
                public TestExecutionResult call() {
                    // Start the trace session on the executing thread so the
                    // agent's thread-local session (and children threads via
                    // InheritableThreadLocal) observe it.
                    TraceBridge.startTest(testId);
                    return dispatch(currentSpec);
                }
            }
        );
        Thread worker = new Thread(task, "causalfl-test-" + testId);
        worker.setDaemon(true);
        worker.start();

        TestExecutionResult result;
        try {
            result = task.get(perTestTimeoutSeconds, TimeUnit.SECONDS);
        } catch (TimeoutException timeout) {
            result = TestExecutionResult.infrastructureError(
                "Test timed out after " + perTestTimeoutSeconds + " seconds"
            );
        } catch (Exception exc) {
            result = TestExecutionResult.infrastructureError(stackTraceOf(exc));
        }
        long runtimeMillis = Math.max((System.nanoTime() - startedAtNanos) / 1000000L, 0L);
        TraceBridge.finishTest(testId, result.failed, result.error, runtimeMillis, result.stackTrace);
        System.out.println(
            "[causalfl-runner] " + testId + " " + result.outcomeLabel() + " " + (runtimeMillis / 1000.0) + "s"
        );
    }

    private static TestExecutionResult dispatch(TestSpec spec) {
        try {
            if ("TESTNG".equals(spec.framework)) {
                return TestNGExecutor.run(spec);
            }
            if ("JUNIT5".equals(spec.framework)) {
                return JUnit5Executor.run(spec);
            }
            return JUnit4Executor.run(spec);
        } catch (NoClassDefFoundError missingFramework) {
            return TestExecutionResult.infrastructureError(
                spec.framework + " is not available on the classpath: " + missingFramework.getMessage()
            );
        } catch (Throwable exc) {
            return TestExecutionResult.infrastructureError(stackTraceOf(exc));
        }
    }

    private static int discoverTests(String[] args) throws Exception {
        File classesRoot = null;
        Path outputFile = null;
        String includes = "*";
        for (int index = 1; index < args.length; index++) {
            if ("--outputFile".equals(args[index]) && index + 1 < args.length) {
                outputFile = Paths.get(args[++index]);
            } else if ("--includes".equals(args[index]) && index + 1 < args.length) {
                includes = args[++index];
            } else if (classesRoot == null) {
                classesRoot = new File(args[index]);
            }
        }
        if (classesRoot == null || !classesRoot.isDirectory() || outputFile == null) {
            System.err.println("[causalfl-runner] usage: discoverTests <classesRoot> --outputFile <file> [--includes <globs>]");
            return 2;
        }

        List<Pattern> includePatterns = TestDiscovery.compileIncludes(includes);
        List<String> lines = TestDiscovery.discover(classesRoot, includePatterns);
        if (outputFile.getParent() != null) {
            Files.createDirectories(outputFile.getParent());
        }
        StringBuilder payload = new StringBuilder();
        for (String line : lines) {
            payload.append(line).append('\n');
        }
        Files.write(outputFile, payload.toString().getBytes(StandardCharsets.UTF_8));
        System.out.println("[causalfl-runner] discovered " + lines.size() + " test method(s)");
        return 0;
    }

    private static String stackTraceOf(Throwable exc) {
        StringWriter buffer = new StringWriter();
        exc.printStackTrace(new PrintWriter(buffer));
        return buffer.toString();
    }

    private static void printUsage() {
        System.err.println("Usage:");
        System.err.println("  causalfl.runner.TestRunnerMain runTests --testMethods <file> [--perTestTimeout <seconds>]");
        System.err.println("  causalfl.runner.TestRunnerMain discoverTests <classesRoot> --outputFile <file> [--includes <globs>]");
    }
}
