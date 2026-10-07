from __future__ import annotations

from dataclasses import dataclass
import re


_SKIP_FUNCTIONS = {
    "memcpy",
    "memcpy_orig",
    "memmove",
    "memset",
    "memcmp",
    "__memcpy",
    "__memmove",
    "__memset",
    "copy_user_generic_unrolled",
    "copy_user_enhanced_fast_string",
    "entry_SYSCALL_64",
    "entry_SYSCALL_64_after_hwframe",
    "do_syscall_64",
    "do_syscall_x64",
    "error_entry",
    "exc_page_fault",
    "asm_exc_page_fault",
    "page_fault_oops",
    "__die",
    "die",
    "oops_end",
    "kmem_cache_alloc",
    "kmem_cache_free",
    # Reporting machinery — the report's context frames sit below these.
    "dump_stack",
    "dump_stack_lvl",
    "__dump_stack",
    "__warn",
    "warn_slowpath_fmt",
    "warn_slowpath_null",
    "ubsan_epilogue",
    "__ubsan_handle_shift_out_of_bounds",
    "kasan_report",
    "__kasan_report",
    "print_report",
    "kasan_report_invalid_free",
    # Locking / coverage hooks that sit between the reporter and the real site.
    "_raw_spin_lock_irqsave",
    "_raw_spin_unlock_irqrestore",
    "write_comp_data",
    "__sanitizer_cov_trace_pc",
}

# GCC partitions/suffixes appended to function symbols: .cold, .isra.0,
# .part.1, .constprop.0, .llvm.* — the base name is the real function.
_FUNC_SUFFIX_RE = re.compile(r"^(?P<base>[A-Za-z_][\w]*?)(?:\.(?:cold|isra|part|constprop|llvm)\b.*)?$")

# UBSAN/KASAN/BUG report headers name the site file:line directly:
#   UBSAN: shift-out-of-bounds in drivers/usb/gadget/udc/dummy_hcd.c:2293:33
_REPORT_IN_RE = re.compile(
    r"(?:UBSAN|KASAN|BUG)[^\n]*?\bin\s+(?P<file>\S+\.[cS]):(?P<line>\d+)"
)

_RIP_RE = re.compile(
    r"^RIP:\s+\S+:(?P<func>[A-Za-z_][\w.]*)(?:\+0x[0-9a-fA-F]+/0x[0-9a-fA-F]+)?(?:\s+(?P<file>\S+\.[cS])(?::(?P<line>\d+))?)?"
)
_FRAME_RE = re.compile(
    r"^\s*(?P<q>\? )?(?P<func>[A-Za-z_][\w.]*)(?:\+0x[0-9a-fA-F]+/0x[0-9a-fA-F]+)?(?:\s+(?P<file>\S+\.[chS])(?::(?P<line>\d+))?)?"
)
# Kmemleak reports use a slightly different stack format from oopses:
# ``backtrace:`` followed by ``[<address>] function file:line``.  The address
# is only decoration; remove it before applying the normal frame parser.
_FRAME_ADDRESS_PREFIX_RE = re.compile(r"^\s*(?:\[<[^>]+>\]\s*)+")
# WARN-type splats name the real site on the header line itself, e.g.:
#   WARNING: CPU: 0 PID: 8093 at kernel/bpf/verifier.c:301 bpf_verifier_vlog+0x297/0x400
# The call trace that follows is dominated by the warn/lockdep reporting
# machinery, so this line is the authoritative crash location.
_WARNING_AT_RE = re.compile(
    r"WARNING:.*?\bat\s+(?P<file>\S+\.[cS]):(?P<line>\d+)\s+"
    r"(?P<func>[A-Za-z_][\w.]*)(?:\+0x[0-9a-fA-F]+/0x[0-9a-fA-F]+)?"
)


@dataclass(frozen=True)
class KernelStackFrame:
    function_name: str
    file_path: str | None = None
    line: int | None = None
    inlined: bool = False


_LOG_PREFIX_RE = re.compile(r"^\s*(?:\[[^\]]*\]\s*){1,2}")


def _strip_log_prefix(line: str) -> str:
    """Drop kernel printk prefixes like ``[   63.833150][ T8436] ``."""
    return _LOG_PREFIX_RE.sub("", line, count=1) if line.lstrip().startswith("[") else line


def _strip_func_suffix(name: str) -> str:
    """Map GCC partition symbols (foo.cold, foo.isra.0, ...) to the base name."""
    match = _FUNC_SUFFIX_RE.match(name)
    return match.group("base") if match is not None else name


def parse_report_location(text: str | None) -> tuple[str, int] | None:
    """Extract the (file, line) a UBSAN/KASAN/BUG report header points at."""
    if not text:
        return None
    match = _REPORT_IN_RE.search(str(text))
    if match is None:
        return None
    return match.group("file"), int(match.group("line"))


def parse_kernel_oops_frames(stacktrace: str | None) -> list[KernelStackFrame]:
    if not stacktrace:
        return []
    frames: list[KernelStackFrame] = []
    warning_at = _WARNING_AT_RE.search(str(stacktrace))
    if warning_at is not None:
        frames.append(
            KernelStackFrame(
                function_name=warning_at.group("func"),
                file_path=warning_at.group("file"),
                line=int(warning_at.group("line")),
            )
        )
    in_trace = False
    for raw in str(stacktrace).splitlines():
        line = _strip_log_prefix(raw.rstrip("\r"))
        rip = _RIP_RE.match(line.strip()) if line.lstrip().startswith("RIP:") else None
        if rip is not None:
            frames.append(
                KernelStackFrame(
                    function_name=_strip_func_suffix(rip.group("func")),
                    file_path=rip.group("file"),
                    line=int(rip.group("line")) if rip.group("line") else None,
                )
            )
            continue
        # KASAN/oops reports use ``Call Trace:``, while kmemleak reports use
        # lowercase ``backtrace:``.  Both sections contain useful source
        # frames and should seed the same lightweight kernel graph.
        if "Call Trace:" in line or line.strip().lower().startswith("backtrace:"):
            in_trace = True
            continue
        if in_trace and line.strip() in {"</TASK>", "<TASK>", "Modules linked in:"}:
            if line.strip() == "</TASK>":
                break
            continue
        if not in_trace:
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("---"):
            continue
        stripped = _FRAME_ADDRESS_PREFIX_RE.sub("", stripped)
        match = _FRAME_RE.match(stripped)
        if match is None:
            continue
        # '?' marks an unreliable-unwind guess (common with .cold splits); the
        # frames are still the best call-site evidence we have, so keep them and
        # let catalog membership filter noise.
        func = _strip_func_suffix(match.group("func"))
        frames.append(
            KernelStackFrame(
                function_name=func,
                file_path=match.group("file"),
                line=int(match.group("line")) if match.group("line") else None,
                inlined="[inline]" in stripped,
            )
        )
    return frames


def resolve_crash_method_id_from_kernel_oops(
    stacktrace: str | None,
    method_ids: set[str] | list[str] | tuple[str, ...],
) -> str | None:
    catalog = list(method_ids)
    if not catalog:
        return None
    by_name: dict[str, list[str]] = {}
    for method_id in catalog:
        name = method_id.split("#", 1)[-1].split("(")[0]
        by_name.setdefault(name, []).append(method_id)

    for frame in parse_kernel_oops_frames(stacktrace):
        if frame.function_name in _SKIP_FUNCTIONS:
            continue
        if frame.function_name.startswith("__x64_sys_"):
            continue
        candidates = by_name.get(frame.function_name, [])
        if not candidates:
            continue
        if frame.file_path:
            file_matches = [item for item in candidates if item.startswith(f"{frame.file_path}#")]
            if len(file_matches) == 1:
                return file_matches[0]
            if file_matches:
                return sorted(file_matches)[0]
        if len(candidates) == 1:
            return candidates[0]
        return sorted(candidates)[0]
    return None
