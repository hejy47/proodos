package causalfl.trace;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

public final class TraceConfig {
    private final String destfile;
    private final boolean appendOutputs;
    private final List<String> includePrefixes;
    private final Set<String> targetMethods;
    private final Set<String> targetClasses;

    private TraceConfig(
        String destfile,
        boolean appendOutputs,
        List<String> includePrefixes,
        Set<String> targetMethods
    ) {
        this.destfile = destfile;
        this.appendOutputs = appendOutputs;
        this.includePrefixes = includePrefixes;
        Set<String> filteredTargetMethods = filterTargetMethods(targetMethods);
        this.targetMethods = Collections.unmodifiableSet(filteredTargetMethods);
        this.targetClasses = Collections.unmodifiableSet(extractTargetClasses(filteredTargetMethods));
    }

    public static TraceConfig parse(String agentArgs) {
        String destfile = null;
        boolean appendOutputs = false;
        List<String> includePrefixes = new ArrayList<String>();
        Set<String> targetMethods = new HashSet<String>();
        if (agentArgs != null && !agentArgs.trim().isEmpty()) {
            for (String rawPart : splitAgentArgs(agentArgs)) {
                String part = rawPart.trim();
                if (part.isEmpty() || !part.contains("=")) {
                    continue;
                }
                String[] keyValue = part.split("=", 2);
                String key = keyValue[0].trim();
                String value = keyValue[1].trim();
                if ("destfile".equals(key)) {
                    destfile = value;
                } else if ("outputDir".equals(key)) {
                    // test_runner pipeline passes outputDir; map to the .ser destfile
                    // under that directory (TraceReportMain converts .ser → CSV).
                    if (destfile == null || destfile.trim().isEmpty()) {
                        String dir = value;
                        while (dir.endsWith("/") || dir.endsWith("\\")) {
                            dir = dir.substring(0, dir.length() - 1);
                        }
                        destfile = dir + "/trace.ser";
                    }
                } else if ("appendOutputs".equals(key)) {
                    appendOutputs = "true".equalsIgnoreCase(value);
                } else if ("includes".equals(key)) {
                    for (String prefix : value.split("[,:]")) {
                        String cleaned = prefix.trim();
                        if (!cleaned.isEmpty()) {
                            includePrefixes.add(cleaned);
                        }
                    }
                } else if ("targetMethods".equals(key)) {
                    for (String methodId : value.split(",")) {
                        String cleaned = methodId.trim();
                        if (!cleaned.isEmpty()) {
                            targetMethods.add(cleaned);
                        }
                    }
                } else if ("targetMethodsFile".equals(key)) {
                    targetMethods.addAll(readTargetMethods(value));
                }
            }
        }
        return new TraceConfig(
            destfile,
            appendOutputs,
            Collections.unmodifiableList(includePrefixes),
            targetMethods
        );
    }

    private static String[] splitAgentArgs(String agentArgs) {
        if (agentArgs.indexOf(';') >= 0) {
            return agentArgs.split(";");
        }
        return agentArgs.split(",");
    }

    public String getDestfile() {
        return destfile;
    }

    public boolean shouldAppendOutputs() {
        return appendOutputs;
    }

    public boolean shouldInstrument(String internalClassName) {
        if (internalClassName == null) {
            return false;
        }
        String dottedName = internalClassName.replace('/', '.');
        if (dottedName.startsWith("java.")
            || dottedName.startsWith("javax.")
            || dottedName.startsWith("jdk.")
            || dottedName.startsWith("sun.")
            || dottedName.startsWith("com.sun.")
            || dottedName.startsWith("org.objectweb.asm.")
            || dottedName.startsWith("causalfl.trace.")) {
            return false;
        }
        if (isProbableTestClass(dottedName)) {
            return false;
        }
        if (includePrefixes.isEmpty()) {
            return targetClasses.isEmpty() || targetClasses.contains(dottedName);
        }
        boolean included = false;
        for (String prefix : includePrefixes) {
            if (dottedName.startsWith(prefix)) {
                included = true;
                break;
            }
        }
        return included && (targetClasses.isEmpty() || targetClasses.contains(dottedName));
    }

    public boolean shouldProbeMethod(String methodId) {
        if (isProbableTestMethod(methodId)) {
            return false;
        }
        return targetMethods.isEmpty() || targetMethods.contains(methodId);
    }

    public String describeIncludePrefixes() {
        if (includePrefixes.isEmpty()) {
            return "(all non-JDK classes)";
        }
        return includePrefixes.toString();
    }

    public int targetMethodCount() {
        return targetMethods.size();
    }

    private static Set<String> readTargetMethods(String path) {
        Set<String> methods = new HashSet<String>();
        if (path == null || path.trim().isEmpty()) {
            return methods;
        }
        try {
            for (String rawLine : Files.readAllLines(Paths.get(path), StandardCharsets.UTF_8)) {
                String cleaned = rawLine.trim();
                if (!cleaned.isEmpty() && !isProbableTestMethod(cleaned)) {
                    methods.add(cleaned);
                }
            }
        } catch (IOException ignored) {
        }
        return methods;
    }

    private static Set<String> filterTargetMethods(Set<String> targetMethods) {
        Set<String> methods = new HashSet<String>();
        for (String methodId : targetMethods) {
            if (!isProbableTestMethod(methodId)) {
                methods.add(methodId);
            }
        }
        return methods;
    }

    private static Set<String> extractTargetClasses(Set<String> targetMethods) {
        Set<String> classes = new HashSet<String>();
        for (String methodId : targetMethods) {
            int separator = methodId.indexOf('#');
            if (separator > 0) {
                classes.add(methodId.substring(0, separator));
            }
        }
        return classes;
    }

    static boolean isProbableTestMethod(String methodId) {
        if (methodId == null || methodId.trim().isEmpty()) {
            return false;
        }
        int separator = methodId.indexOf('#');
        if (separator <= 0) {
            return isProbableTestClass(methodId);
        }
        String className = methodId.substring(0, separator);
        if (isProbableTestClass(className)) {
            return true;
        }
        String methodAndDescriptor = methodId.substring(separator + 1);
        int descriptorStart = methodAndDescriptor.indexOf('(');
        String methodName = descriptorStart >= 0
            ? methodAndDescriptor.substring(0, descriptorStart)
            : methodAndDescriptor;
        return methodName.startsWith("test") && className.endsWith("TestCase");
    }

    static boolean isProbableTestClass(String className) {
        if (className == null || className.trim().isEmpty()) {
            return false;
        }
        String dottedName = className.replace('/', '.');
        String lowerName = dottedName.toLowerCase();
        if (lowerName.contains(".junit.")
            || lowerName.contains(".test.")
            || lowerName.contains(".tests.")) {
            return true;
        }
        int lastSeparator = dottedName.lastIndexOf('.');
        String simpleName = lastSeparator >= 0 ? dottedName.substring(lastSeparator + 1) : dottedName;
        int innerClassSeparator = simpleName.indexOf('$');
        if (innerClassSeparator > 0) {
            simpleName = simpleName.substring(0, innerClassSeparator);
        }
        return simpleName.endsWith("Test")
            || simpleName.endsWith("Tests")
            || simpleName.endsWith("TestCase")
            || simpleName.endsWith("IT")
            || simpleName.endsWith("ITCase");
    }
}
