package causalfl.trace;

import java.io.BufferedWriter;
import java.io.IOException;
import java.io.ObjectInputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;

public final class TraceReportMain {
    private TraceReportMain() {
    }

    public static void main(String[] args) throws Exception {
        Arguments arguments = Arguments.parse(args);
        if (arguments.dataFile == null || arguments.outputDirectory == null) {
            System.err.println("Usage: causalfl.trace.TraceReportMain --dataFile <trace.ser> --outputDirectory <dir>");
            System.exit(2);
        }

        TraceData data = readTraceData(arguments.dataFile);
        Files.createDirectories(arguments.outputDirectory);
        writeSpectra(data, arguments.outputDirectory.resolve("spectra.csv"));
        writeTests(data, arguments.outputDirectory.resolve("tests.csv"));
        writeTrace(data, arguments.outputDirectory.resolve("trace.txt"));
    }

    private static TraceData readTraceData(Path path) throws IOException, ClassNotFoundException {
        ObjectInputStream input = new ObjectInputStream(Files.newInputStream(path));
        try {
            Object value = input.readObject();
            if (!(value instanceof TraceData)) {
                throw new IOException("Unexpected trace data type: " + value.getClass().getName());
            }
            return (TraceData) value;
        } finally {
            input.close();
        }
    }

    private static void writeSpectra(TraceData data, Path path) throws IOException {
        BufferedWriter writer = Files.newBufferedWriter(path, StandardCharsets.UTF_8);
        try {
            writer.write("id,name");
            writer.newLine();
            List<Integer> ids = new ArrayList<Integer>(data.getMethods().keySet());
            Collections.sort(ids);
            for (Integer id : ids) {
                writer.write(Integer.toString(id.intValue()));
                writer.write(",");
                writer.write(csv(data.getMethods().get(id)));
                writer.newLine();
            }
        } finally {
            writer.close();
        }
    }

    private static void writeTests(TraceData data, Path path) throws IOException {
        BufferedWriter writer = Files.newBufferedWriter(path, StandardCharsets.UTF_8);
        try {
            writer.write("id,name,outcome,runtime,stacktrace");
            writer.newLine();
            for (TraceTestData test : data.getTests()) {
                writer.write(Integer.toString(test.getId()));
                writer.write(",");
                writer.write(csv(test.getName()));
                writer.write(",");
                writer.write(test.isError() ? "ERROR" : (test.isFailed() ? "FAIL" : "PASS"));
                writer.write(",");
                writer.write(Double.toString(test.getRuntimeMillis() / 1000.0));
                writer.write(",");
                writer.write(csv(sanitizeUtf8(test.getErrorMessage())));
                writer.newLine();
            }
        } finally {
            writer.close();
        }
    }

    private static void writeTrace(TraceData data, Path path) throws IOException {
        BufferedWriter writer = Files.newBufferedWriter(path, StandardCharsets.UTF_8);
        try {
            for (TraceTestData test : data.getTests()) {
                writer.write(Integer.toString(test.getId()));
                writer.write("\t");
                writer.write(test.isFailed() || test.isError() ? "-" : "+");
                writer.write("\t");
                // writeHitCounts(writer, test.getHitCounts());
                // writer.write("\t");
                // writeDynamicEdges(writer, test.getDynamicEdges());
                // Enter/exit probe stream: e<id>,x<id> (see TraceTestData.getMethodCallOrder)
                writeCallSequence(writer, test.getMethodCallOrder());
                writer.newLine();
            }
        } finally {
            writer.close();
        }
    }

    private static void writeCallSequence(BufferedWriter writer, int[] callOrder) throws IOException {
        for (int i = 0; i < callOrder.length; i++) {
            if (i > 0) {
                writer.write(",");
            }
            int encoded = callOrder[i];
            if (encoded > 0) {
                writer.write("e");
                writer.write(Integer.toString(encoded - 1));
            } else {
                writer.write("x");
                writer.write(Integer.toString((-encoded) - 1));
            }
        }
    }

    private static void writeHitCounts(BufferedWriter writer, Map<Integer, Integer> hitCounts) throws IOException {
        List<Integer> methodIds = new ArrayList<Integer>(hitCounts.keySet());
        Collections.sort(methodIds);
        for (int index = 0; index < methodIds.size(); index++) {
            if (index > 0) {
                writer.write(",");
            }
            Integer methodId = methodIds.get(index);
            writer.write(Integer.toString(methodId.intValue()));
            writer.write(":");
            writer.write(Integer.toString(hitCounts.get(methodId).intValue()));
        }
    }

    private static void writeDynamicEdges(BufferedWriter writer, List<TraceEdgeData> dynamicEdges) throws IOException {
        for (int index = 0; index < dynamicEdges.size(); index++) {
            if (index > 0) {
                writer.write(",");
            }
            TraceEdgeData edge = dynamicEdges.get(index);
            writer.write(Integer.toString(edge.getCallerMethodId()));
            writer.write(">");
            writer.write(Integer.toString(edge.getCalleeMethodId()));
        }
    }

    private static String csv(String value) {
        String text = value == null ? "" : value;
        if (text.indexOf(',') < 0 && text.indexOf('"') < 0 && text.indexOf('\n') < 0 && text.indexOf('\r') < 0) {
            return text;
        }
        return "\"" + text.replace("\"", "\"\"") + "\"";
    }

    private static String sanitizeUtf8(String value) {
        if (value == null) {
            return "";
        }
        return new String(value.getBytes(StandardCharsets.UTF_8), StandardCharsets.UTF_8);
    }

    private static final class Arguments {
        private Path dataFile;
        private Path outputDirectory;

        static Arguments parse(String[] args) {
            Arguments arguments = new Arguments();
            for (int index = 0; index < args.length; index++) {
                String arg = args[index];
                if ("--dataFile".equals(arg) && index + 1 < args.length) {
                    arguments.dataFile = Paths.get(args[++index]);
                } else if ("--outputDirectory".equals(arg) && index + 1 < args.length) {
                    arguments.outputDirectory = Paths.get(args[++index]);
                } else if (arguments.dataFile == null) {
                    arguments.dataFile = Paths.get(arg);
                } else if (arguments.outputDirectory == null) {
                    arguments.outputDirectory = Paths.get(arg);
                }
            }
            return arguments;
        }
    }
}
