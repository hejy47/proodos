package proodos.trace;

import java.io.IOException;
import java.io.ObjectOutputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;

public final class TraceRuntime {
    private static final Object DATA_LOCK = new Object();
    private static final InheritableThreadLocal<TestSession> CURRENT_SESSION = new InheritableThreadLocal<TestSession>();
    private static final ThreadLocal<ArrayDeque<Integer>> CALL_STACK = new ThreadLocal<ArrayDeque<Integer>>();
    private static final ConcurrentHashMap<String, TestSession> ACTIVE_SESSIONS = new ConcurrentHashMap<String, TestSession>();
    private static final ConcurrentHashMap<String, Integer> METHOD_IDS = new ConcurrentHashMap<String, Integer>();
    private static final ConcurrentHashMap<Integer, String> METHOD_NAMES = new ConcurrentHashMap<Integer, String>();
    private static final AtomicInteger NEXT_METHOD_ID = new AtomicInteger(0);
    private static final AtomicInteger NEXT_TEST_ID = new AtomicInteger(0);

    private static volatile String destfile;
    private static volatile boolean appendOutputs;
    private static volatile boolean enabled = false;
    private static volatile boolean shutdownHookRegistered = false;
    private static final List<TraceTestData> FINISHED_TESTS = Collections.synchronizedList(new ArrayList<TraceTestData>());

    private TraceRuntime() {
    }

    public static void configure(String destfileValue, boolean appendOutputsValue) {
        resetState();
        destfile = destfileValue;
        appendOutputs = appendOutputsValue;
        enabled = destfileValue != null && !destfileValue.trim().isEmpty();
        registerShutdownHook();
        log("trace runtime " + (enabled ? "enabled" : "disabled"));
    }

    public static boolean isEnabled() {
        return enabled;
    }

    public static void startTest(String testId) {
        if (!isEnabled()) {
            return;
        }
        CALL_STACK.remove();
        if (testId == null || testId.trim().isEmpty()) {
            CURRENT_SESSION.remove();
            return;
        }
        TestSession session = new TestSession(testId);
        TestSession previous = ACTIVE_SESSIONS.put(testId, session);
        if (previous != null) {
            finishSession(previous, false, true, -1L, "Replaced by a new session for the same test");
        }
        CURRENT_SESSION.set(session);
    }

    /**
     * Records the outcome for a test started via {@link #startTest(String)}.
     * Used by the test_runner TraceBridge.
     * Safe to call from any thread: the session is resolved through the
     * active-session registry, not thread-locals.
     */
    public static void finishTestById(
        String testId,
        boolean failed,
        boolean error,
        long runtimeMillis,
        String stackTrace
    ) {
        TestSession session = testId == null || testId.trim().isEmpty()
            ? null
            : ACTIVE_SESSIONS.get(testId);
        TestSession current = CURRENT_SESSION.get();
        if (session == null) {
            session = current;
        }
        if (current != null && session == current) {
            CURRENT_SESSION.remove();
            CALL_STACK.remove();
        }
        if (session != null) {
            finishSession(
                session,
                failed,
                error,
                runtimeMillis,
                stackTrace == null ? "" : stackTrace
            );
        }
    }

    public static void finishTest() {
        finishCurrentTest(CURRENT_SESSION.get(), false, false, -1L, "");
    }

    public static void resetTest(String testId) {
        TestSession session = null;
        if (testId != null && !testId.trim().isEmpty()) {
            session = ACTIVE_SESSIONS.remove(testId);
        }
        TestSession currentSession = CURRENT_SESSION.get();
        CURRENT_SESSION.remove();
        CALL_STACK.remove();
        if (session == null) {
            session = currentSession;
        }
        if (session != null) {
            finishSession(session, false, true, -1L, "Session reset before a result was reported");
        }
    }

    public static void recordMethodEnter(String methodId) {
        if (!isEnabled() || methodId == null) {
            return;
        }
        TestSession session = CURRENT_SESSION.get();
        if (session == null || session.isFinished()) {
            CURRENT_SESSION.remove();
            CALL_STACK.remove();
            return;
        }

        int methodNumber = methodNumber(methodId);
        incrementMethodHit(session, methodNumber);
        session.recordEnter(methodNumber);
        ArrayDeque<Integer> callStack = currentCallStack();
        Integer callerMethodNumber = callStack.peekLast();
        if (callerMethodNumber != null) {
            session.dynamicEdgeKeys.putIfAbsent(edgeKey(callerMethodNumber.intValue(), methodNumber), Boolean.TRUE);
        }
        callStack.addLast(Integer.valueOf(methodNumber));
    }

    public static void recordMethodExit() {
        if (!isEnabled()) {
            return;
        }
        TestSession session = CURRENT_SESSION.get();
        if (session != null && session.isFinished()) {
            CURRENT_SESSION.remove();
            CALL_STACK.remove();
            return;
        }
        ArrayDeque<Integer> callStack = CALL_STACK.get();
        if (callStack == null || callStack.isEmpty()) {
            return;
        }
        Integer exiting = callStack.peekLast();
        if (session != null && exiting != null) {
            session.recordExit(exiting.intValue());
        }
        callStack.removeLast();
        if (callStack.isEmpty()) {
            CALL_STACK.remove();
        }
    }

    public static void shutdown() {
        if (!enabled) {
            return;
        }
        resetAllSessions();
        writeData();
        CURRENT_SESSION.remove();
        CALL_STACK.remove();
        enabled = false;
    }

    public static void log(String message) {
        if (Boolean.getBoolean("proodos.trace.debug") && message != null) {
            System.err.println("[proodos-trace] " + message);
        }
    }

    private static void finishCurrentTest(
        TestSession session,
        boolean failed,
        boolean error,
        long runtimeMillis,
        String errorMessage
    ) {
        CURRENT_SESSION.remove();
        CALL_STACK.remove();
        if (session == null) {
            return;
        }
        finishSession(session, failed, error, runtimeMillis, errorMessage);
    }

    private static void finishSession(
        TestSession session,
        boolean failed,
        boolean error,
        long runtimeMillis,
        String errorMessage
    ) {
        if (!session.finish()) {
            return;
        }
        ACTIVE_SESSIONS.remove(session.testId, session);
        if (!isEnabled()) {
            return;
        }

        TraceTestData record = new TraceTestData();
        record.setId(NEXT_TEST_ID.getAndIncrement());
        record.setName(session.testId);
        record.setFailed(failed);
        record.setError(error);
        record.setRuntimeMillis(runtimeMillis >= 0 ? runtimeMillis : elapsedMillis(session.startedAtNanos));
        record.setErrorMessage(errorMessage == null ? "" : errorMessage);
        record.setMethodCallOrder(session.getCallOrder());

        List<Integer> methodIds = new ArrayList<Integer>(session.hitCounts.keySet());
        Collections.sort(methodIds);
        for (Integer methodId : methodIds) {
            AtomicInteger count = session.hitCounts.get(methodId);
            if (count != null && count.get() > 0) {
                record.getHitCounts().put(methodId, Integer.valueOf(count.get()));
            }
        }

        List<Long> edgeKeys = new ArrayList<Long>(session.dynamicEdgeKeys.keySet());
        Collections.sort(edgeKeys);
        for (Long edgeKey : edgeKeys) {
            record.getDynamicEdges().add(new TraceEdgeData(edgeCaller(edgeKey.longValue()), edgeCallee(edgeKey.longValue())));
        }
        FINISHED_TESTS.add(record);
    }

    private static int methodNumber(String methodId) {
        Integer existing = METHOD_IDS.get(methodId);
        if (existing != null) {
            return existing.intValue();
        }
        int created = NEXT_METHOD_ID.getAndIncrement();
        Integer previous = METHOD_IDS.putIfAbsent(methodId, Integer.valueOf(created));
        if (previous != null) {
            return previous.intValue();
        }
        METHOD_NAMES.put(Integer.valueOf(created), methodId);
        return created;
    }

    private static void writeData() {
        String target = destfile;
        if (target == null || target.trim().isEmpty()) {
            return;
        }
        synchronized (DATA_LOCK) {
            try {
                Path path = Paths.get(target);
                Path parent = path.getParent();
                if (parent != null) {
                    Files.createDirectories(parent);
                }
                TraceData data = snapshotData();
                ObjectOutputStream output = new ObjectOutputStream(Files.newOutputStream(path));
                try {
                    output.writeObject(data);
                } finally {
                    output.close();
                }
            } catch (IOException exc) {
                System.err.println("[proodos-trace] failed to write trace data: " + exc.getMessage());
            }
        }
    }

    private static TraceData snapshotData() {
        TraceData data = new TraceData();
        List<Integer> methodIds = new ArrayList<Integer>(METHOD_NAMES.keySet());
        Collections.sort(methodIds);
        for (Integer methodId : methodIds) {
            String methodName = METHOD_NAMES.get(methodId);
            if (methodName != null) {
                data.getMethods().put(methodId, methodName);
            }
        }
        synchronized (FINISHED_TESTS) {
            data.getTests().addAll(FINISHED_TESTS);
        }
        return data;
    }

    private static void resetAllSessions() {
        for (TestSession session : ACTIVE_SESSIONS.values()) {
            finishSession(session, false, true, -1L, "Session was still active at shutdown");
        }
        ACTIVE_SESSIONS.clear();
    }

    private static void resetState() {
        ACTIVE_SESSIONS.clear();
        FINISHED_TESTS.clear();
        METHOD_IDS.clear();
        METHOD_NAMES.clear();
        NEXT_METHOD_ID.set(0);
        NEXT_TEST_ID.set(0);
        CURRENT_SESSION.remove();
        CALL_STACK.remove();
    }

    private static void registerShutdownHook() {
        if (shutdownHookRegistered) {
            return;
        }
        synchronized (DATA_LOCK) {
            if (shutdownHookRegistered) {
                return;
            }
            Runtime.getRuntime().addShutdownHook(new Thread(new Runnable() {
                @Override
                public void run() {
                    TraceRuntime.shutdown();
                }
            }, "proodos-trace-shutdown"));
            shutdownHookRegistered = true;
        }
    }

    private static void incrementMethodHit(TestSession session, int methodId) {
        Integer key = Integer.valueOf(methodId);
        AtomicInteger counter = session.hitCounts.get(key);
        if (counter == null) {
            AtomicInteger created = new AtomicInteger(0);
            AtomicInteger previous = session.hitCounts.putIfAbsent(key, created);
            counter = previous != null ? previous : created;
        }
        counter.incrementAndGet();
    }

    private static ArrayDeque<Integer> currentCallStack() {
        ArrayDeque<Integer> callStack = CALL_STACK.get();
        if (callStack == null) {
            callStack = new ArrayDeque<Integer>();
            CALL_STACK.set(callStack);
        }
        return callStack;
    }

    private static long edgeKey(int callerMethodId, int calleeMethodId) {
        return (((long) callerMethodId) << 32) ^ (calleeMethodId & 0xffffffffL);
    }

    private static int edgeCaller(long edgeKey) {
        return (int) (edgeKey >> 32);
    }

    private static int edgeCallee(long edgeKey) {
        return (int) edgeKey;
    }

    private static long elapsedMillis(long startedAtNanos) {
        return Math.max((System.nanoTime() - startedAtNanos) / 1000000L, 0L);
    }

    private static final class TestSession {
        private final String testId;
        private final long startedAtNanos;
        private final ConcurrentHashMap<Integer, AtomicInteger> hitCounts = new ConcurrentHashMap<Integer, AtomicInteger>();
        private final ConcurrentHashMap<Long, Boolean> dynamicEdgeKeys = new ConcurrentHashMap<Long, Boolean>();
        private final AtomicBoolean finished = new AtomicBoolean(false);
        private final IntEventList callOrder = new IntEventList();

        TestSession(String testId) {
            this.testId = testId;
            this.startedAtNanos = System.nanoTime();
        }

        boolean finish() {
            return finished.compareAndSet(false, true);
        }

        boolean isFinished() {
            return finished.get();
        }

        public void recordEnter(int methodId) {
            callOrder.addIfAbsent(methodId + 1);
        }

        public void recordExit(int methodId) {
            callOrder.addIfAbsent(-(methodId + 1));
        }

        public int[] getCallOrder() {
            return callOrder.toArray();
        }
    }

    private static final class IntEventList {
        private int[] values = new int[1024];
        private final Set<Integer> seen = new HashSet<Integer>();
        private int size = 0;

        synchronized void addIfAbsent(int value) {
            Integer boxedValue = Integer.valueOf(value);
            if (!seen.add(boxedValue)) {
                return;
            }
            if (size == values.length) {
                int[] expanded = new int[values.length * 2];
                System.arraycopy(values, 0, expanded, 0, values.length);
                values = expanded;
            }
            values[size++] = value;
        }

        synchronized int[] toArray() {
            int[] snapshot = new int[size];
            System.arraycopy(values, 0, snapshot, 0, size);
            return snapshot;
        }
    }
}
