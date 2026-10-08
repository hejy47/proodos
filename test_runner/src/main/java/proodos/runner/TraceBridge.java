package proodos.runner;

import java.lang.reflect.Method;

/**
 * Reflective bridge to {@code proodos.trace.TraceRuntime}.
 *
 * <p>When the trace agent is attached it appends itself to the bootstrap class
 * loader, so the class resolves from anywhere. When the runner is used without
 * the agent every call degrades to a no-op.
 */
final class TraceBridge {
    private static final Method START_TEST = lookup("startTest", String.class);
    private static final Method FINISH_TEST_BY_ID = lookup(
        "finishTestById",
        String.class,
        boolean.class,
        boolean.class,
        long.class,
        String.class
    );

    private TraceBridge() {
    }

    static boolean isAgentPresent() {
        return START_TEST != null;
    }

    /** Must be invoked on the thread that will execute the test body. */
    static void startTest(String testId) {
        invoke(START_TEST, testId);
    }

    /** Thread-independent: resolves the session from the active-session registry. */
    static void finishTest(String testId, boolean failed, boolean error, long runtimeMillis, String stackTrace) {
        invoke(FINISH_TEST_BY_ID, testId, failed, error, runtimeMillis, stackTrace);
    }

    private static Method lookup(String name, Class<?>... parameterTypes) {
        try {
            Class<?> runtime = Class.forName("proodos.trace.TraceRuntime");
            Method method = runtime.getMethod(name, parameterTypes);
            method.setAccessible(true);
            return method;
        } catch (Throwable absent) {
            return null;
        }
    }

    private static void invoke(Method method, Object... args) {
        if (method == null) {
            return;
        }
        try {
            method.invoke(null, args);
        } catch (Throwable exc) {
            System.err.println("[proodos-runner] trace bridge call failed: " + exc);
        }
    }
}
