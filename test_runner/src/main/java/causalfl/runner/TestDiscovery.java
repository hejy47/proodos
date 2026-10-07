package causalfl.runner;

import java.io.File;
import java.lang.annotation.Annotation;
import java.lang.reflect.Method;
import java.lang.reflect.Modifier;
import java.net.URL;
import java.net.URLClassLoader;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Pattern;

/**
 * Discovers test methods in a compiled-classes directory.
 *
 * <p>Frameworks are detected by annotation name so JUnit 5 / TestNG remain
 * optional dependencies: JUnit 4 ({@code @org.junit.Test}), JUnit 3
 * ({@code junit.framework.TestCase} subclasses with public {@code test*}
 * methods), JUnit 5 ({@code @org.junit.jupiter.api.Test}) and TestNG
 * ({@code @org.testng.annotations.Test}).
 */
final class TestDiscovery {
    private static final String JUNIT4_TEST_ANNOTATION = "org.junit.Test";
    private static final String JUNIT5_TEST_ANNOTATION = "org.junit.jupiter.api.Test";
    private static final String TESTNG_TEST_ANNOTATION = "org.testng.annotations.Test";

    private TestDiscovery() {
    }

    /** Returns lines in the {@code FRAMEWORK,Class#method} contract order. */
    static List<String> discover(File classesRoot, List<Pattern> includePatterns) throws Exception {
        List<String> classNames = new ArrayList<String>();
        collectClassNames(classesRoot, "", classNames);

        URLClassLoader loader = new URLClassLoader(
            new URL[] {classesRoot.toURI().toURL()},
            TestDiscovery.class.getClassLoader()
        );
        List<String> lines = new ArrayList<String>();
        for (String className : classNames) {
            if (!matchesIncludes(className, includePatterns)) {
                continue;
            }
            try {
                Class<?> candidate = Class.forName(className, false, loader);
                appendTestMethods(candidate, lines);
            } catch (Throwable ignored) {
                // Unloadable classes (missing optional deps etc.) are not tests we can run.
            }
        }
        return lines;
    }

    static List<Pattern> compileIncludes(String rawIncludes) {
        List<Pattern> patterns = new ArrayList<Pattern>();
        if (rawIncludes == null || rawIncludes.trim().isEmpty() || "*".equals(rawIncludes.trim())) {
            return patterns;
        }
        for (String rawPattern : rawIncludes.split("[:,]")) {
            String cleaned = rawPattern.trim();
            if (cleaned.isEmpty()) {
                continue;
            }
            StringBuilder regex = new StringBuilder();
            for (int index = 0; index < cleaned.length(); index++) {
                char ch = cleaned.charAt(index);
                if (ch == '*') {
                    regex.append(".*");
                } else if (ch == '?') {
                    regex.append('.');
                } else {
                    regex.append(Pattern.quote(String.valueOf(ch)));
                }
            }
            patterns.add(Pattern.compile(regex.toString()));
        }
        return patterns;
    }

    private static boolean matchesIncludes(String className, List<Pattern> includePatterns) {
        if (includePatterns.isEmpty()) {
            return true;
        }
        for (Pattern pattern : includePatterns) {
            if (pattern.matcher(className).matches()) {
                return true;
            }
        }
        return false;
    }

    private static void collectClassNames(File directory, String packagePrefix, List<String> classNames) {
        File[] entries = directory.listFiles();
        if (entries == null) {
            return;
        }
        for (File entry : entries) {
            if (entry.isDirectory()) {
                String nested = packagePrefix.isEmpty() ? entry.getName() : packagePrefix + "." + entry.getName();
                collectClassNames(entry, nested, classNames);
            } else if (entry.getName().endsWith(".class")) {
                String simpleName = entry.getName().substring(0, entry.getName().length() - ".class".length());
                classNames.add(packagePrefix.isEmpty() ? simpleName : packagePrefix + "." + simpleName);
            }
        }
    }

    private static void appendTestMethods(Class<?> candidate, List<String> lines) {
        int modifiers = candidate.getModifiers();
        if (candidate.isInterface() || Modifier.isAbstract(modifiers) || !Modifier.isPublic(modifiers)) {
            return;
        }
        boolean junit3Case = isJUnit3TestCase(candidate);
        for (Method method : candidate.getMethods()) {
            if (Modifier.isStatic(method.getModifiers())) {
                continue;
            }
            String framework = frameworkForAnnotations(method.getAnnotations());
            if (framework == null
                && junit3Case
                && method.getName().startsWith("test")
                && method.getParameterTypes().length == 0
                && method.getReturnType() == void.class) {
                framework = "JUNIT";
            }
            if (framework != null) {
                lines.add(framework + "," + candidate.getName() + "#" + method.getName());
            }
        }
    }

    private static String frameworkForAnnotations(Annotation[] annotations) {
        for (Annotation annotation : annotations) {
            String name = annotation.annotationType().getName();
            if (JUNIT4_TEST_ANNOTATION.equals(name)) {
                return "JUNIT";
            }
            if (JUNIT5_TEST_ANNOTATION.equals(name)) {
                return "JUNIT5";
            }
            if (TESTNG_TEST_ANNOTATION.equals(name)) {
                return "TESTNG";
            }
        }
        return null;
    }

    private static boolean isJUnit3TestCase(Class<?> candidate) {
        for (Class<?> ancestor = candidate.getSuperclass(); ancestor != null; ancestor = ancestor.getSuperclass()) {
            if ("junit.framework.TestCase".equals(ancestor.getName())) {
                return true;
            }
        }
        return false;
    }
}
