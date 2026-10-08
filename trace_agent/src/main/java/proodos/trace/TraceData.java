package proodos.trace;

import java.io.Serializable;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

final class TraceData implements Serializable {
    private static final long serialVersionUID = 1L;

    private final Map<Integer, String> methods = new LinkedHashMap<Integer, String>();
    private final List<TraceTestData> tests = new ArrayList<TraceTestData>();

    Map<Integer, String> getMethods() {
        return methods;
    }

    List<TraceTestData> getTests() {
        return tests;
    }
}

final class TraceTestData implements Serializable {
    private static final long serialVersionUID = 2L;

    private int id;
    private String name;
    private boolean failed;
    private boolean error;
    private long runtimeMillis;
    private String errorMessage;
    private final Map<Integer, Integer> hitCounts = new LinkedHashMap<Integer, Integer>();
    private final List<TraceEdgeData> dynamicEdges = new ArrayList<TraceEdgeData>();
    private int[] methodCallOrder = new int[0];

    int getId() {
        return id;
    }

    void setId(int id) {
        this.id = id;
    }

    String getName() {
        return name;
    }

    void setName(String name) {
        this.name = name;
    }

    boolean isFailed() {
        return failed;
    }

    void setFailed(boolean failed) {
        this.failed = failed;
    }

    boolean isError() {
        return error;
    }

    void setError(boolean error) {
        this.error = error;
    }

    long getRuntimeMillis() {
        return runtimeMillis;
    }

    void setRuntimeMillis(long runtimeMillis) {
        this.runtimeMillis = runtimeMillis;
    }

    String getErrorMessage() {
        return errorMessage;
    }

    void setErrorMessage(String errorMessage) {
        this.errorMessage = errorMessage;
    }

    Map<Integer, Integer> getHitCounts() {
        return hitCounts;
    }

    List<TraceEdgeData> getDynamicEdges() {
        return dynamicEdges;
    }

    /**
     * Ordered probe events encoded as primitive ints to keep trace serialization compact.
     * Positive values encode enter as {@code methodId + 1}; negative values encode exit as
     * {@code -(methodId + 1)}. Ids refer to rows in spectra.csv / {@link TraceData#getMethods()}.
     */
    int[] getMethodCallOrder() {
        return methodCallOrder;
    }

    void setMethodCallOrder(int[] methodCallOrder) {
        this.methodCallOrder = methodCallOrder;
    }
}

final class TraceEdgeData implements Serializable {
    private static final long serialVersionUID = 1L;

    private final int callerMethodId;
    private final int calleeMethodId;

    TraceEdgeData(int callerMethodId, int calleeMethodId) {
        this.callerMethodId = callerMethodId;
        this.calleeMethodId = calleeMethodId;
    }

    int getCallerMethodId() {
        return callerMethodId;
    }

    int getCalleeMethodId() {
        return calleeMethodId;
    }
}
