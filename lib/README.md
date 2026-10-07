# Java test dependencies

`junit.jar` and `hamcrest-core.jar` support the Java test runner. Build the runner
JAR in `test_runner/` before running Java intervention or observation.

The Java backend in `src/intervention/java/` compiles temporary production-source
changes and places the resulting classes first on the project classpath. It does
not require vendored Mockito, Byte Buddy, or Objenesis JARs. Target projects retain
their own dependencies through their project classpath.
