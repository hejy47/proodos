from __future__ import annotations

import os
import tempfile
from pathlib import Path

from src.patching.java.classpath import java_runtime_classpath, missing_java_runtime_classpath_jars
from src.patching.java.gates import check_request_supported, find_target_method_descriptor
from src.patching.java.models import InterventionRequest, InterventionResult, InterventionStatus
from src.patching.java.runner import (
    build_fixture_classpath,
    compile_sources,
    resolve_layout,
    run_single_test_with_runner,
)
from src.patching.java.source_override import patch_target_method_source, read_java_source


def run_intervention(
    request: InterventionRequest,
    *,
    project_root: Path,
) -> InterventionResult:
    """Compare the baseline test with a temporary production-source intervention."""
    project_root = Path(project_root)
    missing = missing_java_runtime_classpath_jars()
    if missing:
        return InterventionResult(
            status=InterventionStatus.ERROR,
            error_message="Missing jars: " + ", ".join(str(path) for path in missing),
        )

    layout = resolve_layout(project_root)
    source_roots = [Path(p) for p in request.source_roots] or _default_source_roots(layout)
    target_descriptor = find_target_method_descriptor(
        source_roots,
        request.target_class,
        request.target_method,
        request.parameter_types,
    )
    if target_descriptor is None:
        return InterventionResult(
            status=InterventionStatus.UNSUPPORTED,
            error_message="unable to resolve target method source with given parameter_types",
        )
    gate = check_request_supported(request, target_descriptor=target_descriptor)
    if not gate.ok:
        return InterventionResult(
            status=InterventionStatus.UNSUPPORTED,
            error_message=gate.reason,
        )

    target_file = Path(target_descriptor.file_path)
    generated_code: str | None = None
    try:
        original_source = read_java_source(target_file)
        patched_source = patch_target_method_source(original_source, target_descriptor, request)
    except ValueError as exc:
        return InterventionResult(status=InterventionStatus.UNSUPPORTED, error_message=str(exc))
    except OSError as exc:
        return InterventionResult(status=InterventionStatus.ERROR, error_message=str(exc))

    baseline = _compile_and_run(layout, request)
    if baseline.status == InterventionStatus.ERROR:
        return baseline

    try:
        generated_code = patched_source[
            max(0, target_descriptor.start_byte - 200) : target_descriptor.end_byte + 800
        ]
        # Temporary classes take precedence without changing production or test sources.
        with tempfile.TemporaryDirectory(prefix="causalfl-intervention-") as tmp:
            tmp_root = Path(tmp)
            temp_java = tmp_root / target_file.name
            temp_java.write_bytes(patched_source.encode("utf-8"))
            intervened = _compile_and_run(
                layout,
                request,
                compile_main_files=[temp_java],
                override_classes_dir=tmp_root / "classes",
            )
    except Exception as exc:
        return InterventionResult(
            status=InterventionStatus.ERROR,
            original_result=baseline.intervention_result,
            generated_source_path=str(target_file),
            generated_source_code=generated_code,
            error_message=str(exc),
            stdout=baseline.stdout,
            stderr=baseline.stderr,
        )

    return InterventionResult(
        status=intervened.status,
        original_result=baseline.intervention_result,
        intervention_result=intervened.intervention_result,
        generated_source_path=str(target_file),
        generated_source_code=generated_code,
        stdout=intervened.stdout,
        stderr=intervened.stderr,
        error_message=intervened.error_message,
        extras={
            "working_tree_unchanged": read_java_source(target_file) == original_source,
            "layout": str(layout.classes_dir),
        },
    )


def _default_source_roots(layout) -> list[Path]:
    """Source roots for resolving target methods (Maven, Chart, Closure, …)."""
    root = layout.project_root
    candidates = [
        root / "src" / "main" / "java",
        root / "src" / "test" / "java",
        root / "source",
        root / "tests",
        root / "src",
        root / "test",
    ]
    module_roots = {
        relative.parts[0]
        for source in layout.main_sources
        for relative in [source.relative_to(root)]
        if relative.parts
    }
    candidates[:0] = [root / name for name in sorted(module_roots)]
    ordered: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if not resolved.is_dir():
            continue
        # Skip a root that is an ancestor of an already-selected, more specific root
        # (e.g. skip src/ when src/main/java/ is already included).
        if any(
            existing == resolved or existing.is_relative_to(resolved)
            for existing in ordered
        ):
            continue
        # Drop broader roots already covered by a more specific child.
        ordered = [
            existing
            for existing in ordered
            if not resolved.is_relative_to(existing) or existing == resolved
        ]
        if resolved not in ordered:
            ordered.append(resolved)
    return ordered


def prepend_classpath_entries(base: str, *entries: Path | str | None) -> str:
    """Prepend absolute classpath entries (first wins for duplicate classes)."""
    head: list[str] = []
    for entry in entries:
        if entry is None:
            continue
        text = str(Path(entry).resolve()) if not isinstance(entry, str) else entry.strip()
        if text:
            head.append(text)
    parts = [*head]
    for part in base.split(os.pathsep):
        text = part.strip()
        if text and text not in parts:
            parts.append(text)
    return os.pathsep.join(parts)


def _compile_and_run(
    layout,
    request: InterventionRequest,
    *,
    compile_main_files: list[Path] | None = None,
    override_classes_dir: Path | None = None,
) -> InterventionResult:
    layout = resolve_layout(layout.project_root)

    if not layout.skip_main_compile:
        main_ok, main_out, main_err = compile_sources(
            source_files=layout.main_sources,
            output_dir=layout.classes_dir,
            classpath=build_fixture_classpath(layout),
            cwd=layout.project_root,
        )
        if not main_ok:
            return InterventionResult(
                status=InterventionStatus.ERROR,
                error_message="main sources failed to compile",
                stdout=main_out,
                stderr=main_err,
            )

    project_cp = build_fixture_classpath(layout)
    # Prefer the resolved test output directory over other project classpath entries.
    project_cp = prepend_classpath_entries(project_cp, layout.test_classes_dir)
    try:
        full_cp = java_runtime_classpath(project_cp)
    except FileNotFoundError as exc:
        return InterventionResult(status=InterventionStatus.ERROR, error_message=str(exc))

    if compile_main_files and override_classes_dir is not None:
        override_classes_dir.mkdir(parents=True, exist_ok=True)
        main_ok, main_out, main_err = compile_sources(
            source_files=compile_main_files,
            output_dir=override_classes_dir,
            classpath=full_cp,
            cwd=layout.project_root,
        )
        if not main_ok:
            return InterventionResult(
                status=InterventionStatus.ERROR,
                error_message="override target source failed to compile",
                stdout=main_out,
                stderr=main_err,
                generated_source_code=compile_main_files[0].read_text(encoding="utf-8")
                if compile_main_files
                else None,
            )
        project_cp = prepend_classpath_entries(project_cp, override_classes_dir)
        full_cp = java_runtime_classpath(project_cp)

    # Precompiled projects use existing test classes; fixtures compile all tests.
    test_files = [] if layout.skip_main_compile else layout.test_sources

    test_ok, test_out, test_err = compile_sources(
        source_files=test_files,
        output_dir=layout.test_classes_dir,
        classpath=full_cp,
        cwd=layout.project_root,
    )
    if not test_ok:
        return InterventionResult(
            status=InterventionStatus.ERROR,
            error_message="test sources failed to compile",
            stdout=test_out,
            stderr=test_err,
        )

    outcome, stdout, stderr = run_single_test_with_runner(
        classpath=full_cp,
        test_class=request.test_class,
        test_method=request.test_method,
        cwd=layout.project_root,
    )
    # Prefer compile stderr when runner only says ERROR with no detail.
    merged_stderr = stderr
    if test_err and "ERROR" in (stdout or ""):
        merged_stderr = (stderr or "") + ("\n" + test_err if test_err else "")

    status = InterventionStatus.SUCCESS if outcome.passed else InterventionStatus.FAILED
    return InterventionResult(
        status=status,
        intervention_result=outcome,
        original_result=None,
        stdout=stdout,
        stderr=merged_stderr,
    )
