package proodos.runner;

/** One test method parsed from a {@code FRAMEWORK,Class#method} line. */
final class TestSpec {
    final String framework;
    final String className;
    final String methodName;

    TestSpec(String framework, String className, String methodName) {
        this.framework = framework;
        this.className = className;
        this.methodName = methodName;
    }

    /** Canonical id used across tests.csv / trace artifacts: {@code Class::method}. */
    String testId() {
        return className + "::" + methodName;
    }

    static TestSpec parse(String rawLine) {
        String line = rawLine == null ? "" : rawLine.trim();
        if (line.isEmpty() || line.startsWith("#")) {
            return null;
        }
        String framework = "JUNIT";
        String identifier = line;
        int comma = line.indexOf(',');
        if (comma >= 0) {
            framework = line.substring(0, comma).trim().toUpperCase();
            identifier = line.substring(comma + 1).trim();
        }
        int hash = identifier.indexOf('#');
        if (hash <= 0 || hash == identifier.length() - 1) {
            return null;
        }
        return new TestSpec(framework, identifier.substring(0, hash), identifier.substring(hash + 1));
    }

    @Override
    public String toString() {
        return framework + "," + className + "#" + methodName;
    }
}
