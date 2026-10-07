package causalfl.trace;

import java.lang.instrument.ClassFileTransformer;
import java.lang.instrument.IllegalClassFormatException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.security.ProtectionDomain;
import org.objectweb.asm.ClassReader;
import org.objectweb.asm.ClassVisitor;
import org.objectweb.asm.ClassWriter;
import org.objectweb.asm.MethodVisitor;
import org.objectweb.asm.Opcodes;
import org.objectweb.asm.commons.AdviceAdapter;

/** Instruments application methods for enter/exit probes. Test lifecycle is owned by test_runner. */
public final class TraceClassTransformer implements ClassFileTransformer {
    private static final String RUNTIME = "causalfl/trace/TraceRuntime";

    private final TraceConfig config;

    public TraceClassTransformer(TraceConfig config) {
        this.config = config;
    }

    @Override
    public byte[] transform(
        ClassLoader loader,
        String className,
        Class<?> classBeingRedefined,
        ProtectionDomain protectionDomain,
        byte[] classfileBuffer
    ) throws IllegalClassFormatException {
        if (!config.shouldInstrument(className)) {
            return null;
        }
        return transformApplicationClass(className, classfileBuffer);
    }

    private byte[] transformApplicationClass(String className, byte[] classfileBuffer) {
        try {
            ClassReader reader = new ClassReader(classfileBuffer);
            ClassWriter writer = new ClassWriter(reader, ClassWriter.COMPUTE_MAXS);
            final int[] probedMethods = new int[] {0};
            ClassVisitor visitor = new ClassVisitor(Opcodes.ASM9, writer) {
                private String currentClassName;

                @Override
                public void visit(
                    int version,
                    int access,
                    String name,
                    String signature,
                    String superName,
                    String[] interfaces
                ) {
                    currentClassName = name;
                    super.visit(version, access, name, signature, superName, interfaces);
                }

                @Override
                public MethodVisitor visitMethod(
                    int access,
                    String name,
                    String descriptor,
                    String signature,
                    String[] exceptions
                ) {
                    MethodVisitor methodVisitor = super.visitMethod(access, name, descriptor, signature, exceptions);
                    if (methodVisitor == null) {
                        return null;
                    }
                    if ((access & (Opcodes.ACC_ABSTRACT | Opcodes.ACC_NATIVE | Opcodes.ACC_BRIDGE | Opcodes.ACC_SYNTHETIC)) != 0) {
                        return methodVisitor;
                    }
                    if ("<clinit>".equals(name)) {
                        return methodVisitor;
                    }
                    final String methodId = currentClassName.replace('/', '.') + "#" + name + descriptor;
                    if (!config.shouldProbeMethod(methodId)) {
                        return methodVisitor;
                    }
                    probedMethods[0]++;
                    return new AdviceAdapter(Opcodes.ASM9, methodVisitor, access, name, descriptor) {
                        @Override
                        protected void onMethodEnter() {
                            super.visitLdcInsn(methodId);
                            super.visitMethodInsn(
                                Opcodes.INVOKESTATIC,
                                RUNTIME,
                                "recordMethodEnter",
                                "(Ljava/lang/String;)V",
                                false
                            );
                        }

                        @Override
                        protected void onMethodExit(int opcode) {
                            super.visitMethodInsn(
                                Opcodes.INVOKESTATIC,
                                RUNTIME,
                                "recordMethodExit",
                                "()V",
                                false
                            );
                        }
                    };
                }
            };
            reader.accept(visitor, ClassReader.EXPAND_FRAMES);
            byte[] transformedBytes = writer.toByteArray();
            if (Boolean.getBoolean("causalfl.trace.debug")) {
                TraceRuntime.log("instrumented application class " + className + " with " + probedMethods[0] + " methods");
            }
            dumpTransformedClass(className, transformedBytes);
            return transformedBytes;
        } catch (Throwable exc) {
            TraceRuntime.log(
                "failed to instrument "
                    + className
                    + ": "
                    + exc.getClass().getName()
                    + ": "
                    + exc.getMessage()
            );
            return null;
        }
    }

    private static void dumpTransformedClass(String className, byte[] transformedBytes) {
        String dumpDir = System.getProperty("causalfl.trace.dumpDir", "").trim();
        if (dumpDir.isEmpty()) {
            return;
        }
        try {
            Path outputPath = Paths.get(dumpDir, className + ".class");
            Path parent = outputPath.getParent();
            if (parent != null) {
                Files.createDirectories(parent);
            }
            Files.write(outputPath, transformedBytes);
        } catch (Throwable exc) {
            TraceRuntime.log("failed to dump transformed class " + className + ": " + exc.getMessage());
        }
    }
}
