package proodos.trace;

import java.io.File;
import java.lang.instrument.Instrumentation;
import java.lang.management.ManagementFactory;
import java.net.URL;
import java.util.List;
import java.util.jar.JarFile;

public final class TraceAgent {
    private TraceAgent() {
    }

    public static void premain(String agentArgs, Instrumentation instrumentation) {
        appendAgentJarToBootstrapClassLoaderSearch(instrumentation);
        TraceConfig config = TraceConfig.parse(agentArgs);
        TraceRuntime.configure(
            config.getDestfile(),
            config.shouldAppendOutputs()
        );
        TraceRuntime.log("TraceAgent premain started from " + resolveAgentJarPath());
        TraceRuntime.log("include prefixes: " + config.describeIncludePrefixes());
        TraceRuntime.log("target method count: " + config.targetMethodCount());
        instrumentation.addTransformer(new TraceClassTransformer(config));
    }

    private static void appendAgentJarToBootstrapClassLoaderSearch(Instrumentation instrumentation) {
        try {
            URL location = TraceAgent.class.getProtectionDomain().getCodeSource().getLocation();
            if (location == null || !"file".equals(location.getProtocol())) {
                return;
            }
            File jarFile = new File(location.toURI());
            if (!jarFile.isFile()) {
                return;
            }
            instrumentation.appendToBootstrapClassLoaderSearch(new JarFile(jarFile));
        } catch (Throwable exc) {
            System.err.println(
                "Proodos TraceAgent could not append itself to the bootstrap classpath: "
                    + exc.getClass().getName()
                    + ": "
                    + exc.getMessage()
            );
        }
    }

    private static String resolveAgentJarPath() {
        try {
            List<String> inputArguments = ManagementFactory.getRuntimeMXBean().getInputArguments();
            for (String argument : inputArguments) {
                if (!argument.startsWith("-javaagent:")) {
                    continue;
                }
                String rawValue = argument.substring("-javaagent:".length());
                int optionsSeparator = rawValue.indexOf('=');
                String jarPath = optionsSeparator >= 0 ? rawValue.substring(0, optionsSeparator) : rawValue;
                File jarFile = new File(jarPath);
                if (jarFile.isFile()) {
                    return jarFile.getAbsolutePath();
                }
            }
            URL location = TraceAgent.class.getProtectionDomain().getCodeSource().getLocation();
            if (location == null || !"file".equals(location.getProtocol())) {
                return null;
            }
            File jarFile = new File(location.toURI());
            if (!jarFile.isFile()) {
                return null;
            }
            return jarFile.getAbsolutePath();
        } catch (Throwable ignored) {
            return null;
        }
    }
}
