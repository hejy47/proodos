from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess

from src.fault_graph.kernel_oops import parse_kernel_oops_frames
from src.utils.c_source import extract_c_call_names_from_source
from src.project.container_runtime import exec_argv


KERNEL_DATASETS = {"cohiker", "recent", "recent_syz", "recentsyz"}


def is_kernel_dataset(name: str | None = None) -> bool:
    dataset = (name or os.environ.get("PROODOS_KERNEL_DATASET", "cohiker")).strip().lower()
    return dataset in KERNEL_DATASETS


def is_recent_syz_dataset(name: str | None = None) -> bool:
    dataset = (name or os.environ.get("PROODOS_KERNEL_DATASET", "")).strip().lower()
    return dataset in {"recent", "recent_syz", "recentsyz"}


def kernel_git_head(source_root: Path) -> str | None:
    """Return HEAD only when *source_root* itself is a git worktree.

    A tarball checkout nested in this repository must not inherit Proodos's
    commit. ``git -C <dir> rev-parse HEAD`` walks parents when ``.git`` is
    missing.
    """
    try:
        toplevel = subprocess.check_output(
            ["git", "-C", str(source_root), "rev-parse", "--show-toplevel"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if Path(toplevel).resolve() != Path(source_root).resolve():
            return None
        return subprocess.check_output(
            ["git", "-C", str(source_root), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


# Prefer syzkaller programs that QEMU can run with execprog. C reproducers
# are accepted for preprocess and for the gcc guest recipe (REPRO_KIND=gcc).
_CASE_REPRO_RELPATHS = (
    "assets/repro.syz",
    "assets/logs/repro.syz",
    "assets/repro.prog",
    "assets/logs/repro.prog",
    "assets/repro.c",
    "assets/repro.cprog",
    "assets/logs/repro.c",
    "assets/repro.txt",
    "assets/logs/repro.txt",
)
_CASE_REPORT_RELPATHS = (
    "results/console.log.reference",
    "assets/repro.report",
    "assets/logs/repro.report",
    "assets/logs/dmesg.txt",
)
_EXECPROG_REPRO_SUFFIXES = {".syz", ".prog"}
_C_REPRO_SUFFIXES = {".c", ".cprog"}


def is_execprog_repro(path: Path | None) -> bool:
    """Return whether *path* can be executed by ``syz-execprog``."""
    return path is not None and path.suffix.lower() in _EXECPROG_REPRO_SUFFIXES


def is_c_repro(path: Path | None) -> bool:
    """Return whether *path* is a syzkaller C reproducer (``.c`` / ``.cprog``)."""
    return path is not None and path.suffix.lower() in _C_REPRO_SUFFIXES


def repro_kind_for_path(path: Path | None) -> str:
    """Guest recipe: ``syz`` (execprog) or ``gcc`` (compile and run the binary)."""
    if is_c_repro(path):
        return "gcc"
    if is_execprog_repro(path):
        return "syz"
    raise ValueError(f"unsupported reproducer {path}")


def resolve_kernel_syz_path(case_id: str, dataset_root: Path) -> Path | None:
    explicit = os.environ.get("PROODOS_SYZ_PATH", "").strip()
    if explicit:
        path = Path(explicit)
        return path if path.is_file() else None
    case_dir = os.environ.get("PROODOS_CASE_DIR", "").strip()
    if case_dir:
        root = Path(case_dir)
        for rel in _CASE_REPRO_RELPATHS:
            path = root / rel
            if path.is_file():
                return path
    path = dataset_root / "datasets" / "testcases" / f"{case_id}.syz"
    return path if path.is_file() else None


def resolve_kernel_report_path(case_id: str, dataset_root: Path) -> Path | None:
    explicit = os.environ.get("PROODOS_REPORT_PATH", "").strip()
    if explicit:
        path = Path(explicit)
        return path if path.is_file() else None
    case_dir = os.environ.get("PROODOS_CASE_DIR", "").strip()
    if case_dir:
        root = Path(case_dir)
        for rel in _CASE_REPORT_RELPATHS:
            path = root / rel
            if path.is_file() and path.stat().st_size > 0:
                return path
    path = dataset_root / "datasets" / "bug_report" / f"{case_id}.txt"
    return path if path.is_file() else None


_HOP_SKIP_FUNCTIONS = {
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
    "dump_stack",
    "dump_stack_lvl",
    "__dump_stack",
    "__warn",
    "ubsan_epilogue",
    "kasan_report",
    "__die",
    "die",
    "oops_end",
    "kmem_cache_alloc",
    "kmem_cache_free",
    "kmalloc",
    "kzalloc",
    "kfree",
    "kvfree",
    "printk",
    "printf",
    "snprintf",
    "sprintf",
    "strlen",
    "strcpy",
    "strcmp",
    "strncmp",
    "BUG",
    "WARN",
    "WARN_ON",
    "pr_err",
    "pr_warn",
    "pr_info",
    "pr_debug",
    "pr_emerg",
    "pr_warn_ratelimited",
    "IS_ERR",
    "PTR_ERR",
    "ERR_PTR",
    "WARN_ON_ONCE",
    "BUG_ON",
    "likely",
    "unlikely",
    "READ_ONCE",
    "WRITE_ONCE",
    "container_of",
    "ARRAY_SIZE",
    "offsetof",
    "min",
    "max",
    "min_t",
    "max_t",
}

_STORAGE_PREFIX = (
    r"(?:static|inline|__init|__exit|__always_inline|asmlinkage|__visible|"
    r"noinline|__maybe_unused|__cold|__weak)"
)
_TYPE_PREFIX = r"(?:const|unsigned|signed|struct|enum|union|volatile|long|short)"


@dataclass(frozen=True)
class KernelTree:
    linux_dir: Path | None = None
    docker_container: str | None = None

    def available(self) -> bool:
        if self.linux_dir is not None and self.linux_dir.is_dir():
            return True
        if not self.docker_container:
            return False
        result = subprocess.run(
            exec_argv(self.docker_container, "test", "-d", "/root/linux"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0

    def read_bytes(self, rel_path: str) -> bytes:
        if self.linux_dir is not None:
            return (self.linux_dir / rel_path).read_bytes()
        if not self.docker_container:
            raise FileNotFoundError(rel_path)
        result = subprocess.run(
            exec_argv(self.docker_container, "cat", f"/root/linux/{rel_path}"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            raise FileNotFoundError(rel_path)
        return result.stdout

    def exists(self, rel_path: str) -> bool:
        if self.linux_dir is not None:
            return (self.linux_dir / rel_path).is_file()
        if not self.docker_container:
            return False
        result = subprocess.run(
            exec_argv(self.docker_container, "test", "-f", f"/root/linux/{rel_path}"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0

    def find_defining_file(
        self,
        function_name: str,
        *,
        preferred_relpaths: list[str] | None = None,
    ) -> str | None:
        if not function_name or function_name in _HOP_SKIP_FUNCTIONS:
            return None
        if function_name.startswith("__x64_sys_") or function_name.startswith("__builtin_"):
            return None
        hits = self._function_hits_batch([function_name]).get(function_name, [])
        return _pick_defining_file(function_name, hits, preferred_relpaths or [])

    def _function_hits_batch(self, function_names: list[str]) -> dict[str, list[tuple[str, str]]]:
        names = [name for name in dict.fromkeys(function_names) if name]
        if not names:
            return {}
        alternation = "|".join(re.escape(name) for name in names)
        pattern = rf"(^|[^A-Za-z0-9_])({alternation})[[:space:]]*\("
        grouped: dict[str, list[tuple[str, str]]] = {name: [] for name in names}
        name_res = {
            name: re.compile(rf"(^|[^A-Za-z0-9_]){re.escape(name)}\s*\(")
            for name in names
        }
        for raw in self._grep_c_lines(pattern):
            path, text = _split_grep_hit(raw)
            if not path:
                continue
            for name, matcher in name_res.items():
                if matcher.search(text):
                    grouped[name].append((path, text))
        return grouped

    def _grep_c_lines(self, pattern: str) -> list[str]:
        lines: list[str] = []
        if self.linux_dir is not None:
            result = subprocess.run(
                ["git", "-C", str(self.linux_dir), "grep", "-n", "-E", pattern, "--", "*.c"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
            )
            lines = [line for line in result.stdout.splitlines() if line.strip()]
            if not lines:
                result = subprocess.run(
                    [
                        "grep",
                        "-rn",
                        "-E",
                        "--include=*.c",
                        pattern,
                        str(self.linux_dir),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    check=False,
                )
                prefix = str(self.linux_dir).rstrip("/") + "/"
                for raw in result.stdout.splitlines():
                    lines.append(raw[len(prefix) :] if raw.startswith(prefix) else raw)
            return lines
        if not self.docker_container:
            return []
        result = subprocess.run(
            exec_argv(
                self.docker_container,
                "git",
                "-C",
                "/root/linux",
                "grep",
                "-n",
                "-E",
                pattern,
                "--",
                "*.c",
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        return [line for line in result.stdout.splitlines() if line.strip()]

    def _grep_c_paths(self, pattern: str) -> list[str]:
        """Return source-relative C/header paths containing ``pattern``.

        Kernel callbacks are often selected through function pointers, so a
        one-hop call scan cannot discover their definitions.  This path-level
        search lets the case seeder use concrete subsystem names from the
        reproducer (for example ``cgroup``) without indexing the whole tree.
        """
        if self.linux_dir is not None:
            result = subprocess.run(
                [
                    "git", "-C", str(self.linux_dir), "grep", "-l", "-I", "-i", "-E",
                    pattern, "--", "*.c", "*.h",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
            )
            lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            if lines:
                return lines
            result = subprocess.run(
                [
                    "grep", "-ril", "-E", "--include=*.c", "--include=*.h",
                    pattern, str(self.linux_dir),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
            )
            prefix = str(self.linux_dir).rstrip("/") + "/"
            return [
                line[len(prefix):] if line.startswith(prefix) else line
                for line in result.stdout.splitlines()
                if line.strip()
            ]
        if not self.docker_container:
            return []
        result = subprocess.run(
            exec_argv(
                self.docker_container,
                "git", "-C", "/root/linux", "grep", "-l", "-I", "-i", "-E",
                pattern, "--", "*.c", "*.h",
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def materialize(self, rel_paths: list[str], dest_root: Path) -> list[Path]:
        written: list[Path] = []
        for rel_path in rel_paths:
            if not self.exists(rel_path):
                continue
            dest = dest_root / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(self.read_bytes(rel_path))
            written.append(dest)
        return written


def seed_relpaths_for_case(
    *,
    case_id: str,
    dataset_root: Path,
    kernel: KernelTree,
    extra_hops: int = 1,
    max_files: int = 40,
) -> list[str]:
    rel_paths: list[str] = []
    seen: set[str] = set()
    frames: list = []
    report_path = resolve_kernel_report_path(case_id, dataset_root)
    report_text = ""
    if report_path is not None and report_path.is_file():
        report_text = report_path.read_text(encoding="utf-8", errors="replace")
        frames = parse_kernel_oops_frames(report_text)
    syz_path = resolve_kernel_syz_path(case_id, dataset_root)
    syz_text = (
        syz_path.read_text(encoding="utf-8", errors="replace")
        if syz_path is not None and syz_path.is_file() else ""
    )

    allowed_tops = _allowed_source_tops(frames)
    # Some kernel reports contain only a subsystem warning such as
    # ``bfs_fill_super(): WARNING`` and no standard Call Trace or file:line.
    # Resolve those concrete function names directly so a one-line report still
    # seeds a small, source-backed graph.
    report_function_paths: list[str] = []
    for function_name in _report_function_names(report_text):
        path = kernel.find_defining_file(function_name, preferred_relpaths=rel_paths)
        if path:
            report_function_paths.append(path)
            allowed_tops.add(path.split("/", 1)[0])

    def add(rel_path: str | None, *, force: bool = False) -> None:
        if not rel_path or rel_path in seen:
            return
        if not rel_path.endswith((".c", ".h", ".S")):
            return
        if not force and not _keep_source_file(rel_path, allowed_tops):
            return
        seen.add(rel_path)
        rel_paths.append(rel_path)

    for frame in frames:
        add(frame.file_path)
    for path in report_function_paths:
        add(path)

    # A callback registered through a function pointer will not appear in the
    # lexical call edges of the report frames.  Seed a small number of files
    # whose content matches concrete identifiers from the reproducer/report,
    # prioritizing paths that name the same subsystem.  These are evidence-
    # supported additions and remain capped by max_files.
    for path in _keyword_seed_relpaths(report_text, syz_text, kernel, max_files=max_files):
        if len(rel_paths) >= max_files:
            break
        add(path, force=True)

    if extra_hops <= 0:
        return rel_paths

    hop_from: set[str] = set()
    hop_count = 0
    for frame in frames:
        if frame.function_name in _HOP_SKIP_FUNCTIONS:
            continue
        rel = frame.file_path
        if not rel or not rel.endswith(".c"):
            continue
        if rel.split("/", 1)[0] not in allowed_tops:
            continue
        hop_from.add(rel)
        hop_count += 1
        if hop_count >= 1:
            break

    callee_names: list[str] = []
    seen_names: set[str] = set()
    for rel_path in list(rel_paths):
        if rel_path not in hop_from:
            continue
        if not rel_path.endswith(".c") or not kernel.exists(rel_path):
            continue
        try:
            source_bytes = kernel.read_bytes(rel_path)
        except OSError:
            continue
        for name in extract_c_call_names_from_source(source_bytes):
            if not _interesting_callee(name) or name in seen_names:
                continue
            seen_names.add(name)
            callee_names.append(name)
    hits_by_name = kernel._function_hits_batch(callee_names)
    for function_name in callee_names:
        if len(rel_paths) >= max_files:
            break
        add(
            _pick_defining_file(
                function_name,
                hits_by_name.get(function_name, []),
                rel_paths,
            )
        )
    return rel_paths


def _allowed_source_tops(frames) -> set[str]:
    tops: set[str] = set()
    for frame in frames:
        if frame.function_name in _HOP_SKIP_FUNCTIONS:
            continue
        if frame.file_path:
            tops.add(frame.file_path.split("/", 1)[0])
            break
    tops.discard("arch")
    tops.add("lib")
    return tops


def _keep_source_file(rel_path: str, allowed_tops: set[str]) -> bool:
    top = rel_path.split("/", 1)[0]
    if top in {"scripts", "tools", "samples", "Documentation", "usr"}:
        return False
    if top == "arch":
        return "crypto" in Path(rel_path).parts
    return top in allowed_tops


def _interesting_callee(name: str) -> bool:
    if len(name) < 4 or name in _HOP_SKIP_FUNCTIONS:
        return False
    if name.isupper() or name.startswith("__x64_sys_") or name.startswith("__builtin_"):
        return False
    return True


def _report_function_names(report_text: str) -> list[str]:
    """Extract function-like names from terse reports without a call trace."""
    if not report_text:
        return []
    names: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(\)", report_text):
        name = match.group(1)
        if name in seen or not _interesting_callee(name):
            continue
        seen.add(name)
        names.append(name)
    return names


_KEYWORD_STOPWORDS = {
    "address", "alloc", "allocated", "atomic", "cache", "call", "check",
    "close", "code", "cpu", "error", "file", "freed", "include", "index",
    "kernel", "memory", "object", "page", "path", "read", "report", "size",
    "source", "stack", "task", "trace", "value", "write",
}


def _keyword_seed_relpaths(
    report_text: str,
    syz_text: str,
    kernel: KernelTree,
    *,
    max_files: int,
) -> list[str]:
    """Find a bounded set of source files named by case-specific keywords."""
    terms: list[str] = []
    seen_terms: set[str] = set()

    def add_term(raw: str) -> None:
        term = raw.strip().lower()
        if (len(term) < 4 or term in _KEYWORD_STOPWORDS
                or not re.fullmatch(r"[a-z_][a-z0-9_]*", term)
                or term in seen_terms):
            return
        seen_terms.add(term)
        terms.append(term)

    # Syzkaller syscall names and string arguments expose subsystem names even
    # when the corresponding kernel callback is reached indirectly.
    for match in re.finditer(r"\b([a-z_][a-z0-9_]*)(?:\$([a-z0-9_]+))?\b", syz_text.lower()):
        add_term(match.group(1))
        if match.group(2):
            add_term(match.group(2))
    for literal in re.findall(r"['\"]([a-z_][a-z0-9_]{3,})['\"]", syz_text.lower()):
        add_term(literal)

    # Report frames provide useful names for callback families and release
    # paths; files already seeded by their frame simply score as duplicates.
    for name in re.findall(r"\b([a-z_][a-z0-9_]{3,})\+0x[0-9a-f]+", report_text.lower()):
        add_term(name)
    if not terms:
        return []

    # Keep the grep expression bounded; path scoring below prefers exact
    # subsystem/function names over generic textual matches.
    terms = terms[:32]
    pattern = "(" + "|".join(re.escape(term) for term in terms) + ")"
    paths = [
        path for path in kernel._grep_c_paths(pattern)
        if path.endswith((".c", ".h"))
        and path.split("/", 1)[0] not in {"scripts", "tools", "samples", "Documentation", "usr"}
    ]
    term_set = tuple(terms)

    def score(path: str) -> tuple[int, int, str]:
        lowered = path.lower()
        path_score = sum(100 for term in term_set if term in lowered)
        basename_score = sum(25 for term in term_set if term in Path(path).name.lower())
        return (path_score + basename_score, -len(path), path)

    return sorted(dict.fromkeys(paths), key=score, reverse=True)[:max_files]


def _split_grep_hit(raw: str) -> tuple[str, str]:
    parts = raw.split(":", 2)
    if len(parts) < 3:
        return "", ""
    return parts[0].strip(), parts[2]


def _is_definition_line(function_name: str, text: str) -> bool:
    pattern = re.compile(
        rf"^[ \t]*(?:{_STORAGE_PREFIX}[ \t]+)*(?:{_TYPE_PREFIX}[ \t]+)*"
        rf"[\w]+(?:[ \t]+\*|[ \t*])+{re.escape(function_name)}[ \t]*\("
    )
    return pattern.search(text) is not None


def _pick_defining_file(
    function_name: str,
    hits: list[tuple[str, str]],
    preferred_relpaths: list[str],
) -> str | None:
    if not hits:
        return None
    definitions = [(path, text) for path, text in hits if _is_definition_line(function_name, text)]
    candidates = definitions or hits
    preferred = set(preferred_relpaths)
    preferred_parents = {str(Path(path).parent.as_posix()) for path in preferred}
    preferred_tops = {path.split("/", 1)[0] for path in preferred if "/" in path}
    preferred_archs = {
        path.split("/")[1]
        for path in preferred
        if path.startswith("arch/") and len(path.split("/")) > 1
    }

    def score(path: str) -> tuple[int, str]:
        if path in preferred:
            return (0, path)
        parent = str(Path(path).parent.as_posix())
        if parent in preferred_parents:
            return (1, path)
        parts = path.split("/")
        arch = parts[1] if len(parts) > 1 and parts[0] == "arch" else None
        if arch and preferred_archs and arch not in preferred_archs:
            return (80, path)
        if parts[0] in preferred_tops:
            return (2, path)
        if parts[0] == "drivers" and "drivers" not in preferred_tops:
            return (90, path)
        return (20, path)

    ranked = sorted({path for path, _ in candidates}, key=score)
    best = ranked[0]
    if score(best)[0] >= 80:
        return None
    return best
