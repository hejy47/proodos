"""Validate a returned function and retain only a passing source change."""
from __future__ import annotations

from dataclasses import asdict
import difflib
from pathlib import Path

from src.patching.java.bridge import (
    apply_intervention, parse_method_id, parse_test_id, parse_jvm_parameter_types,
    _validate_replacement_budget,
)
from src.patching.java.gates import find_target_method_descriptor
from src.patching.java.models import InterventionRequest
from src.patching.java.source_override import patch_target_method_source


def source_diff(before: str, after: str, relative_path: str) -> str:
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"a/{relative_path}", tofile=f"b/{relative_path}",
    ))


def validate_patch(*, project, test_id: str, method_id: str,
                   replacement_function: str) -> dict:
    """Reuse the temporary Java validator, then compile/test the actual checkout.

    Failed candidates restore the exact previous bytes and rebuild that version.
    Accepted patches stay on disk so the next full regression sees all repairs.
    This is a controller operation, never an agent tool.
    """
    result = dict(status="validation_error", method_id=method_id, test_id=test_id,
                  test_passed=False, accepted=False)
    target = None
    before = None
    written = False
    try:
        error = _validate_replacement_budget(replacement_function)
        if error:
            raise ValueError(error)
        target_class, target_method, _ = parse_method_id(method_id)
        test_class, test_method = parse_test_id(test_id)
        parameters = parse_jvm_parameter_types(method_id)
        roots = project.discover_source_roots()
        method = find_target_method_descriptor(roots, target_class, target_method, parameters)
        if method is None:
            raise ValueError("Cannot resolve the selected production method")
        target = Path(method.file_path).resolve()
        root = project.project_path.resolve()
        if not target.is_relative_to(root) or not any(target.is_relative_to(p.resolve()) for p in roots):
            raise ValueError("Patch target must be inside a production source root")
        if any(target.is_relative_to(p.resolve()) for p in project.discover_test_roots()):
            raise ValueError("Patches to test sources are not allowed")
        before = target.read_bytes()
        original = before.decode("utf-8")
        request = InterventionRequest(
            test_class=test_class, test_method=test_method,
            target_class=target_class, target_method=target_method,
            parameter_types=parameters, replacement_function=replacement_function,
        )
        changed = patch_target_method_source(original, method, request)
        if changed == original:
            raise ValueError("The replacement does not change the method")

        temporary = apply_intervention(
            project_root=root, test_id=test_id, method_id=method_id,
            replacement_function=replacement_function,
        )
        result["temporary_validation"] = temporary
        if temporary.get("status") != "success" or not temporary.get("test_passed"):
            result.update(status="rejected", error=temporary.get("error") or "Selected test still fails")
            return result
        if target.read_bytes() != before:
            raise ValueError("Target source changed during validation")
        # The temporary override is only a preliminary check. Require the actual
        # build and project test adapter to agree before accepting this change.
        written = True
        target.write_bytes(changed.encode("utf-8"))
        compilation = project.compile()
        result["compilation"] = asdict(compilation)
        if not compilation.success:
            result.update(status="rejected", error="Patched project failed to compile")
            return result
        test = project.run_test_case(test_id)
        result["selected_test"] = asdict(test)
        if not test.success or test.errors or test.failed or test.failing_tests:
            result.update(status="rejected", error="Patched checkout did not pass the selected test")
            return result
        relative = target.relative_to(root).as_posix()
        result.update(
            status="accepted", accepted=True, test_passed=True, file_path=relative,
            source_before=original,
            diff=source_diff(original, changed, relative),
        )
    except Exception as exc:
        result.update(status="error", error=f"{type(exc).__name__}: {exc}")
    finally:
        if written and not result["accepted"]:
            # Never undo previous accepted patches or unrelated project edits.
            target.write_bytes(before)
            try:
                restored = project.compile()
                result["rollback_compilation"] = asdict(restored)
                if not restored.success:
                    result.update(status="rollback_error", fatal=True,
                                  error="Source restored, but rebuilding the previous version failed")
            except Exception as exc:
                result.update(status="rollback_error", fatal=True, error=str(exc))
    return result
