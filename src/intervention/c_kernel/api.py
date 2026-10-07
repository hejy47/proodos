from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
from typing import Any

from src.intervention.c_kernel.patch import (
    replace_function_in_file,
)
from src.intervention.c_kernel.qemu import run_qemu_case
from src.intervention.c_kernel.tracing import collect_kprobe_trace
from src.project.container_runtime import copy_to, exec_argv, in_target_container


MAX_OBSERVE_SAMPLES = 8
INTERVENTION_REPRO_TIMEOUT_SECONDS = int(
    os.environ.get("CAUSALFL_INTERVENTION_REPRO_TIMEOUT_SECONDS", "120")
)
INTERVENTION_QEMU_TIMEOUT_SECONDS = int(
    os.environ.get("CAUSALFL_INTERVENTION_QEMU_TIMEOUT_SECONDS", "600")
)
KERNEL_BUILD_TIMEOUT_SECONDS = int(
    os.environ.get("CAUSALFL_KERNEL_BUILD_TIMEOUT_SECONDS", "1800")
)
_CRASH_PATTERNS = ("Kernel panic", "BUG:", "Oops:", "general protection fault", "KASAN:")


def apply_c_observation(
    *,
    project,
    preprocess_data,
    test_id: str,
    method_id: str,
    fetch_args: str = "",
    probe_spec: str | dict | None = None,
) -> dict[str, Any]:
    """Run an agent-defined tracefs kprobe in the case VM.

    ``probe_spec`` may be JSON with ``type`` (``entry``/``return``), a
    ``fetch`` list/string, and ``stacktrace``.  ``fetch_args`` remains an
    internal compatibility alias for callers that only need register values.
    """
    error = _preconditions(project)
    if error is not None:
        return _obs_error(method_id, [], error, probe_spec=probe_spec)
    record = _find_method_record(preprocess_data, method_id)
    if record is None:
        return _obs_error(
            method_id,
            [],
            f"unable to resolve source for `{method_id}`",
            status="validation_error",
            probe_spec=probe_spec,
        )

    result = collect_kprobe_trace(
        project=project,
        test_id=test_id,
        method_id=method_id,
        fetch_args=fetch_args,
        probe_spec=probe_spec,
        max_samples=MAX_OBSERVE_SAMPLES,
    )
    result.setdefault("error", None)
    result["test_id"] = test_id
    result["truncated"] = result.get("call_count", 0) >= MAX_OBSERVE_SAMPLES
    # A missing probe sample after a guest crash/timeout is a tool failure,
    # not a successful observation with zero calls.  Preserve that distinction
    # for the Intervention and Counterfactual stages instead of unconditionally
    # reporting pass.
    result["test_passed"] = result.get("status") not in {
        "execution_error",
        "validation_error",
    }
    result.setdefault("kprobe_fetch_args", fetch_args or "default six register arguments")
    return result


def apply_c_intervention(
    *,
    project,
    preprocess_data,
    test_id: str,
    method_id: str,
    replacement_function: str,
) -> dict[str, Any]:
    """Run an agent-provided replacement function and return the kernel report."""
    error = _preconditions(project)
    if error is not None:
        return _int_error(method_id, replacement_function, error)
    replacement = (replacement_function or "").strip()
    if not replacement:
        return _int_error(method_id, replacement_function, "empty replacement function", status="validation_error")
    record = _find_method_record(preprocess_data, method_id)
    if record is None or not record.get("source_code") or not record.get("file_path"):
        return _int_error(
            method_id,
            replacement_function,
            f"unable to resolve source for `{method_id}`",
            status="validation_error",
        )

    source_code = str(record["source_code"])
    try:
        patched_function = replacement
    except ValueError as exc:
        return _int_error(method_id, replacement_function, str(exc), status="validation_error")

    run = _patch_build_run_restore(
        project=project,
        rel_path=str(record["file_path"]),
        original_function=source_code,
        patched_function=patched_function,
        case_id=test_id,
    )
    if run["error"] is not None:
        return _int_error(
            method_id,
            replacement_function,
            str(run["error"]),
            stderr=run.get("build_log"),
            generated=patched_function,
        )

    vm_log = str(run["vm_log"])
    crashed = _log_has_crash(vm_log)
    return {
        "status": "success",
        "error": None,
        "method_id": method_id,
        "test_id": test_id,
        "replacement_function": replacement,
        "original_passed": False,
        "test_passed": not crashed,
        "outcome": "still_failing" if crashed else "crash_disappeared",
        "selected_mode": "c_kernel_stub",
        "stdout": vm_log[-2000:],
    }


def _preconditions(project) -> str | None:
    kernel = getattr(project, "kernel", None)
    if kernel is None or not kernel.available():
        return "kernel tree is not available (docker container or COHIKER_LINUX_DIR required)"
    case_id = getattr(project, "case_id", None)
    if not case_id:
        return "project has no case_id"
    return None


def _find_method_record(preprocess_data, method_id: str) -> dict[str, Any] | None:
    from src.preprocess.context import SourceMethodRecords
    if isinstance(preprocess_data.method_records, SourceMethodRecords):
        record = preprocess_data.method_records.get(method_id)
        if record is not None:
            return record
        target = _loose_id_key(method_id)
        matches = [mid for mid in preprocess_data.method_ids if _loose_id_key(mid) == target]
        return preprocess_data.method_records.get(matches[0]) if len(matches) == 1 else None
    records = {
        str(record.get("method_id")): record
        for record in getattr(preprocess_data, "method_records", ())
        if record.get("method_id")
    }
    record = records.get(method_id)
    if record is not None:
        return record
    # Agents often emit near-miss ids (crypto_arc4.c# vs crypto/arc4.c#).
    # Fall back to a punctuation-insensitive comparison; unique match only.
    target = _loose_id_key(method_id)
    matches = [record for known, record in records.items() if _loose_id_key(known) == target]
    if len(matches) == 1:
        return matches[0]
    return None


def _loose_id_key(method_id: str) -> str:
    return re.sub(r"[^a-z0-9]", "", method_id.lower())


def _patch_build_run_restore(
    *,
    project,
    rel_path: str,
    original_function: str,
    patched_function: str,
    case_id: str,
) -> dict[str, Any]:
    """Patch one kernel source file, rebuild, run the reproducer, then restore.

    The kernel tree is always restored and rebuilt so later runs see a clean
    source/base image state.
    """
    kernel = project.kernel
    try:
        original_text = kernel.read_bytes(rel_path).decode("utf-8", errors="replace")
    except OSError:
        return {"error": f"cannot read kernel source {rel_path}", "vm_log": None}
    try:
        patched_text = replace_function_in_file(original_text, original_function, patched_function)
    except ValueError as exc:
        return {"error": str(exc), "vm_log": None}

    patched = False
    try:
        _write_kernel_file(kernel, rel_path, patched_text.encode("utf-8"))
        patched = True
        ok, build_log = _rebuild_kernel(kernel)
        if not ok:
            return {"error": "kernel rebuild failed", "vm_log": None, "build_log": build_log}
        # Intervention runs only need to know whether the crash recurs; a
        # shorter repro window and scan wait keep per-call cost down.
        vm_log = run_qemu_case(
            project.docker_container,
            case_id,
            repro_timeout=INTERVENTION_REPRO_TIMEOUT_SECONDS,
            scan_wait=10,
            qemu_timeout=INTERVENTION_QEMU_TIMEOUT_SECONDS,
        )
        runtime_error = _qemu_tool_error(vm_log)
        if runtime_error:
            return {"error": runtime_error, "vm_log": vm_log}
        return {"error": None, "vm_log": vm_log}
    except Exception as exc:  # noqa: BLE001 - report as tool error, never raise
        return {"error": f"qemu run failed: {exc}", "vm_log": None}
    finally:
        # This must also run for KeyboardInterrupt/SystemExit and unexpected
        # build/runner failures; an interrupted experiment must not poison the
        # source tree or make the next case's indexed spans unreadable.
        if patched:
            _restore(kernel, rel_path, original_text)


def _restore(kernel, rel_path: str, original_text: str) -> None:
    # Restore the source only. Rebuilding here would cost minutes per call for
    # no benefit: the next intervention re-patches and rebuilds anyway, and the
    # next case starts from a fresh checkout.
    _write_kernel_file(kernel, rel_path, original_text.encode("utf-8"))


def _write_kernel_file(kernel, rel_path: str, content: bytes) -> None:
    if kernel.linux_dir is not None:
        (kernel.linux_dir / rel_path).write_bytes(content)
        return
    tmp = tempfile.NamedTemporaryFile(delete=False)
    try:
        tmp.write(content)
        tmp.close()
        copy_to(kernel.docker_container, tmp.name, f"/root/linux/{rel_path}")
    finally:
        os.unlink(tmp.name)


def _guest_linux_dir(kernel) -> str:
    linux_dir = kernel.linux_dir
    if linux_dir is None:
        return "/root/linux"
    text = str(linux_dir)
    if text.startswith("/data/") or text.startswith("/root/"):
        return text
    repo = Path(__file__).resolve().parents[3]
    try:
        rel = Path(linux_dir).resolve().relative_to(repo)
    except ValueError:
        return "/root/linux"
    return f"/data/{rel.as_posix()}"


def _rebuild_kernel(kernel) -> tuple[bool, str]:
    guest = _guest_linux_dir(kernel)
    container = kernel.docker_container
    if kernel.linux_dir is not None and (in_target_container(container) or not container):
        command = [
            "make",
            "-C",
            str(kernel.linux_dir),
            f"-j{os.cpu_count() or 4}",
            "bzImage",
        ]
        env = os.environ.copy()
        # Keep intervention rebuilds on the same compiler/cache path as the
        # case's initial build.  Without this, every replacement falls back
        # to raw gcc and recompiles the whole kernel from scratch.
        env["CC"] = "ccache gcc"
        env["HOSTCC"] = "ccache gcc"
    else:
        command = exec_argv(
            container,
            "bash",
            "-lc",
            (
                "cc=$(command -v ccache >/dev/null 2>&1 && echo 'ccache gcc' || echo gcc); "
                f"cd {shlex.quote(guest)} && make CC=\"$cc\" HOSTCC=\"$cc\" -j$(nproc) bzImage"
            ),
        )
        env = None
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            check=False,
            timeout=KERNEL_BUILD_TIMEOUT_SECONDS,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return False, (
            f"kernel rebuild timed out after {KERNEL_BUILD_TIMEOUT_SECONDS} seconds\n"
            f"{output[-1800:]}"
        )
    except OSError as exc:
        return False, f"kernel rebuild could not start: {exc}"
    return result.returncode == 0, (result.stdout or "")[-2000:]


def _log_has_crash(vm_log: str) -> bool:
    return any(pattern in vm_log for pattern in _CRASH_PATTERNS)


def _qemu_tool_error(vm_log: str) -> str | None:
    """Classify runner/test timeouts before interpreting an intervention.

    A timed-out or disconnected QEMU run must not be reported as a passing
    intervention merely because its partial log lacks a crash marker.  The
    reproducer itself remains bounded by ``INTERVENTION_REPRO_TIMEOUT_SECONDS``
    while this function handles the surrounding tool failure.
    """
    text = str(vm_log or "")
    status = re.search(r"CFL_QEMU_STATUS=([A-Za-z0-9_.-]+)", text)
    if status:
        value = status.group(1)
        if value == "execution_timeout":
            return (
                "QEMU intervention run timed out after "
                f"{INTERVENTION_QEMU_TIMEOUT_SECONDS} seconds"
            )
        if value == "boot_timeout":
            return "QEMU intervention run could not boot a reachable guest"
        if value == "guest_crash":
            # The QEMU launcher emits guest_crash only while waiting for SSH,
            # before it copies or executes the reproducer. A kernel report in
            # the serial log therefore describes a failed boot, not the test
            # outcome; never classify it as a successful intervention run.
            return "guest crashed before the intervention test could run"
    exec_status = re.search(r"CFL_EXEC_STATUS=(\d+)", text)
    if exec_status and int(exec_status.group(1)) == 124:
        return (
            "intervention reproducer timed out after "
            f"{INTERVENTION_REPRO_TIMEOUT_SECONDS} seconds"
        )
    return None


def _obs_error(
    method_id: str,
    expressions,
    error: str,
    *,
    status: str = "execution_error",
    stderr: str | None = None,
    generated: str | None = None,
    probe_spec: str | dict | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "error": error,
        "method_id": method_id,
        "probe": "kprobe",
        "probe_spec": probe_spec,
        "kprobe_fetch_args": None,
        "samples": [],
        "call_count": 0,
        "truncated": False,
        "stderr": stderr,
        "generated_source_snippet": generated,
    }


def _int_error(
    method_id: str,
    replacement_function: str,
    error: str,
    *,
    status: str = "execution_error",
    stderr: str | None = None,
    generated: str | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "error": error,
        "method_id": method_id,
        "replacement_function": replacement_function,
        "test_passed": False,
        "outcome": None,
        "stderr": stderr,
        "generated_source_snippet": generated,
    }
