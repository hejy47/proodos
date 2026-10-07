from __future__ import annotations

from pathlib import Path
import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import struct
import subprocess
import tempfile
import zlib

from src.project.container_runtime import exec_argv, in_target_container, shell_argv
from src.project.kernel_tree import is_c_repro, is_execprog_repro, resolve_kernel_syz_path
from src.utils.output_paths import kernel_runtime_dir


_SYZ_HEADER_RE = re.compile(r"^#\s*(\{.*\})\s*$", re.M)
_REPO_ROOT = Path(__file__).resolve().parents[3]


def parse_syz_options(syz_text: str) -> dict:
    """Return execprog options from a syzkaller reproducer header, or {}."""
    match = _SYZ_HEADER_RE.search(syz_text)
    if not match:
        return {}
    raw = match.group(1)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return _parse_go_syz_header(raw)


_GO_HEADER_BOOLS = (
    ("Threaded", "threaded"),
    ("Repeat", "repeat"),
    ("UseTmpDir", "tmpdir"),
    ("CloseFDs", "close_fds"),
    ("VhciInjection", "vhci"),
    ("Wifi", "wifi"),
    ("IEEE802154", "ieee802154"),
    ("Sysctl", "sysctl"),
    ("DevlinkPCI", "devlinkpci"),
)


def _parse_go_syz_header(raw: str) -> dict:
    """Map a syzkaller Go-syntax comment header onto execprog JSON keys."""
    opts: dict = {}
    for src, dst in _GO_HEADER_BOOLS:
        match = re.search(rf"\b{src}:(true|false)\b", raw)
        if match:
            opts[dst] = match.group(1) == "true"
    match = re.search(r"\bProcs:(\d+)\b", raw)
    if match:
        opts["procs"] = int(match.group(1))
    match = re.search(r"\bSandbox:([^,\s}]*)", raw)
    if match:
        opts["sandbox"] = match.group(1).strip()
    if re.search(r"\bFault:true\b", raw):
        opts["fault"] = True
    return opts


# This syz-execprog only copies resources into later calls if the producer
# names them with ``<rN=>``. Several CoHiker ``.syz`` files omit that marker.
_BARE_PIPEFD = re.compile(r"\{0xffffffffffffffff,\s*0xffffffffffffffff\}")
_BARE_PTR_RES = re.compile(r"(&\(0x[0-9a-fA-F]+\)=)0x0\b")
_BARE_PACKET_IFINDEX = re.compile(
    r"(getsockname\$packet\([^;]*?\{0x11,\s*0x[0-9a-fA-F]+,\s*)0x0\b"
)
_BARE_U64_RES = re.compile(r"\{0xffffffffffffffff\}")


def annotate_syz_resources(syz_text: str) -> str:
    """Name unnamed pipefds / io_uring copyouts so execprog can thread rN."""
    declared = {int(n) for n in re.findall(r"<r(\d+)=>", syz_text)}
    declared |= {int(n) for n in re.findall(r"^r(\d+)\s*=", syz_text, re.M)}
    uses = {int(n) for n in re.findall(r"\br(\d+)\b", syz_text)}
    nxt = sorted(uses - declared)
    if not nxt:
        return syz_text

    def _repl_pipe(_match: re.Match[str]) -> str:
        nonlocal nxt
        if len(nxt) >= 2:
            a, b = nxt[0], nxt[1]
            nxt = nxt[2:]
            return f"{{<r{a}=>0xffffffffffffffff, <r{b}=>0xffffffffffffffff}}"
        if len(nxt) == 1:
            a = nxt.pop(0)
            return f"{{0xffffffffffffffff, <r{a}=>0xffffffffffffffff}}"
        return _match.group(0)

    def _repl_ptr(match: re.Match[str]) -> str:
        nonlocal nxt
        if not nxt:
            return match.group(0)
        a = nxt.pop(0)
        return f"{match.group(1)}<r{a}=>0x0"

    def _repl_pkt(match: re.Match[str]) -> str:
        nonlocal nxt
        if not nxt:
            return match.group(0)
        a = nxt.pop(0)
        return f"{match.group(1)}<r{a}=>0x0"

    def _repl_u64(_match: re.Match[str]) -> str:
        nonlocal nxt
        if not nxt:
            return _match.group(0)
        a = nxt.pop(0)
        return f"{{<r{a}=>0xffffffffffffffff}}"

    syz_text = _BARE_PIPEFD.sub(_repl_pipe, syz_text)
    syz_text = _BARE_PTR_RES.sub(_repl_ptr, syz_text)
    syz_text = _BARE_PACKET_IFINDEX.sub(_repl_pkt, syz_text)
    return _BARE_U64_RES.sub(_repl_u64, syz_text)


# Newer syzkaller serializes mount images as zlib+base64 ``"$eJz..."``.
# This docker binary still wants ``(size, nsegs, segments, flags, opts, chdir)``.
_MOUNT_CALL_RE = re.compile(
    r"^((?:r\d+\s*=\s*)?)(syz_mount_image\$\w+)\((.*)\)(\s*(?:#.*)?)?$",
    re.M,
)
_COMPRESSED_STR_RE = re.compile(r'"(\$[A-Za-z0-9+/=]+)"')
_SEGMENT_GAP = 64
# This executor maps ``0x7f0000000000+off`` into a 16MB data window. Keep
# rewritten image blobs in the high end so they do not clobber fs/dir/opts
# pointers that syz programs usually put at low offsets.
_SYZ_DATA_START = 0x7F0000000000
_SYZ_DATA_WINDOW = 0x1000000
_SYZ_DATA_LOW_RESERVE = 0x100000
# executor ``struct fs_image_segment { void* data; uintptr_t size; uintptr_t offset; }``
_FS_IMAGE_SEGMENT_SIZE = 24


def _mount_call_parts(match: re.Match[str]) -> tuple[str, str, str, str]:
    return match.group(1), match.group(2), match.group(3), match.group(4) or ""


def _split_syz_args(body: str) -> list[str]:
    out: list[str] = []
    buf: list[str] = []
    depth = 0
    quote = ""
    for i, ch in enumerate(body):
        if quote:
            buf.append(ch)
            if ch == quote and body[i - 1] != "\\":
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
            continue
        if ch in "({[":
            depth += 1
            buf.append(ch)
        elif ch in ")}]":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf).strip())
    return out


def _decode_compressed_image(blob: str) -> bytes:
    raw = blob[1:] if blob.startswith("$") else blob
    pad = (-len(raw)) % 4
    return zlib.decompress(base64.b64decode(raw + "=" * pad))


def _coalesce_image_segments(img: bytes, gap: int = _SEGMENT_GAP) -> list[tuple[int, bytes]]:
    segs: list[tuple[int, bytes]] = []
    i = 0
    n = len(img)
    while i < n:
        if img[i] == 0:
            i += 1
            continue
        j = i
        while j < n:
            if img[j] != 0:
                j += 1
                continue
            k = j
            while k < n and img[k] == 0 and (k - j) < gap:
                k += 1
            if k < n and img[k] != 0 and (k - j) < gap:
                j = k
                continue
            break
        segs.append((i, img[i:j]))
        i = j
    return segs


def rewrite_compressed_mount_images(syz_text: str) -> str:
    """Rewrite ``compressed_image`` mount calls into the old segment layout."""

    def _repl(match: re.Match[str]) -> str:
        lhs, name, body, tail = _mount_call_parts(match)
        args = _split_syz_args(body)
        if len(args) != 7:
            return match.group(0)
        compressed = _COMPRESSED_STR_RE.search(args[-1])
        if not compressed:
            return match.group(0)
        img = _decode_compressed_image(compressed.group(1))
        pieces = _coalesce_image_segments(img)
        if not pieces:
            pieces = [(0, b"\x00")]
        payload = sum(len(data) for _off, data in pieces)
        array_bytes = len(pieces) * _FS_IMAGE_SEGMENT_SIZE
        page = 4096
        low = _SYZ_DATA_START + _SYZ_DATA_LOW_RESERVE
        data_end = _SYZ_DATA_START + _SYZ_DATA_WINDOW
        data_addr = (data_end - payload) & ~(page - 1)
        array_addr = (data_addr - array_bytes) & ~(page - 1)
        if array_addr < low:
            array_addr = low
            data_addr = (array_addr + array_bytes + page - 1) & ~(page - 1)
        addr = data_addr
        segs_txt = []
        for offset, data in pieces:
            segs_txt.append(
                f"{{&(0x{addr:x})=\"{data.hex()}\"/{len(data)}, 0x{len(data):x}, 0x{offset:x}}}"
            )
            addr += len(data)
        fs, directory, flags, opts, chdir = args[0], args[1], args[2], args[3], args[4]
        rewritten = (
            f"{lhs}{name}({fs}, {directory}, 0x{len(img):x}, 0x{len(pieces):x}, "
            f"&(0x{array_addr:x})=[{', '.join(segs_txt)}], {flags}, {opts}, {chdir})"
        )
        return rewritten + tail

    return _MOUNT_CALL_RE.sub(_repl, syz_text)


_MOUNT_FS_BARE_RE = re.compile(
    r"^((?:r\d+\s*=\s*)?syz_mount_image\$(\w+)\()(&\(0x[0-9a-fA-F]+\))(?!\s*=)",
    re.M,
)


def rewrite_mount_fs_literal(syz_text: str) -> str:
    """Fill ``syz_mount_image$fs`` when the pointer has no filesystem string.

    Newer programs omit ``='jfs\\x00'`` because the type carries it. This
    executor then mounts an empty name and the image never comes up.
    """

    def _repl(match: re.Match[str]) -> str:
        return f"{match.group(1)}{match.group(3)}='{match.group(2)}\\x00'"

    return _MOUNT_FS_BARE_RE.sub(_repl, syz_text)


def rewrite_mount_opts_struct(syz_text: str) -> str:
    """Keep typed ``fs_options`` structs; do not wrap them as C strings.

    ``opts`` is ``ptr[in, fs_options[...]]``. Copyin of the packed struct
    already emits ``grpquota,iocharset=cp1251,...,`` (trailing comma + NUL).
    A ``='...'`` literal is not copied into that type, so ``strlen`` sees
    an empty buffer and ``mount`` runs with no options.
    """
    return syz_text


_MOUNT_VARIANT_RE = re.compile(r"\bmount\$(?:afs|pvfs2)\(")


def rewrite_generic_mount_calls(syz_text: str) -> str:
    """Rewrite ``mount$afs`` / ``mount$pvfs2`` to untyped ``mount``.

    This execprog's descriptions only have generic ``mount`` for those
    two. Other variants such as ``mount$9p_fd`` are known and must stay:
    the ``$`` name carries fstype ``9p`` and typed ``p9_options``. Turning
    them into generic ``mount`` leaves fstype as leftover ``./file0`` and
    never reaches ``v9fs_cache_session_get_cookie``.
    """
    return _MOUNT_VARIANT_RE.sub("mount(", syz_text)


_CALL_LINE_RE = re.compile(
    r"^((?:r\d+\s*=\s*)?)([a-zA-Z0-9_$]+)\((.*)\)(\s*(?:#.*)?)?$",
    re.M,
)
_BARE_PTR_RE = re.compile(r"^&\(0x[0-9a-fA-F]+\)$")
# Newer programs omit ``='logon\x00'`` etc. because the $variant carries the
# string. This executor then passes an empty buffer and the call is a no-op.
_CONST_PTR_STRINGS: dict[str, dict[int, str]] = {
    "add_key$fscrypt_v1": {0: "logon"},
    "setxattr$trusted_overlay_upper": {1: "trusted.overlay.upper"},
    "lsetxattr$trusted_overlay_upper": {1: "trusted.overlay.upper"},
    "fsetxattr$trusted_overlay_upper": {1: "trusted.overlay.upper"},
    "openat$fuse": {1: "/dev/fuse"},
    "openat$dsp": {1: "/dev/dsp"},
    "setsockopt$inet_tcp_TCP_ULP": {3: "tls"},
    "setsockopt$inet6_tcp_TCP_ULP": {3: "tls"},
}


def rewrite_const_ptr_strings(syz_text: str) -> str:
    """Fill type-carried string pointers that have no ``='...'`` payload."""

    def _repl(match: re.Match[str]) -> str:
        lhs, name, body, tail = (
            match.group(1),
            match.group(2),
            match.group(3),
            match.group(4) or "",
        )
        table = _CONST_PTR_STRINGS.get(name)
        if not table:
            return match.group(0)
        args = _split_syz_args(body)
        changed = False
        for idx, lit in table.items():
            if idx < len(args) and _BARE_PTR_RE.match(args[idx]):
                args[idx] = f"{args[idx]}='{lit}\\x00'"
                changed = True
        if not changed:
            return match.group(0)
        return f"{lhs}{name}({', '.join(args)}){tail}"

    return _CALL_LINE_RE.sub(_repl, syz_text)


_SEG_LEN_OFF_RE = re.compile(
    r",\s*(0x[0-9a-fA-F]+),\s*(0x[0-9a-fA-F]+)\}"
)


def rewrite_undersized_mount_images(syz_text: str) -> str:
    """Bump ``syz_mount_image`` size so high-offset segments fit.

    This executor ``ftruncate``s the memfd to ``size`` then ``pwrite``s
    segments; size 0 drops inodes past EOF.
    """

    def _repl(match: re.Match[str]) -> str:
        lhs, name, body, tail = _mount_call_parts(match)
        args = _split_syz_args(body)
        if len(args) < 5:
            return match.group(0)
        try:
            size = int(args[2], 0)
        except ValueError:
            return match.group(0)
        needed = 0
        for seg in _SEG_LEN_OFF_RE.finditer(args[4]):
            needed = max(needed, int(seg.group(1), 16) + int(seg.group(2), 16))
        # Filesystems read whole blocks; a byte-exact size is one sector short.
        needed = (needed + 4095) & ~4095
        if needed <= size:
            return match.group(0)
        args[2] = f"0x{needed:x}"
        return f"{lhs}{name}({', '.join(args)}){tail}"

    return _MOUNT_CALL_RE.sub(_repl, syz_text)


def rewrite_null_mount_segments(syz_text: str) -> str:
    """Give empty mount opts a real string so ``strlen`` does not SEGV.

    ``nsegs=0`` still used to skip the loop device: this executor's
    ``syz_mount_image`` was patched to ``need_loop_device = nsegs != 0``
    (``0x0``/``nil`` segment pointers are sanitized to ``0x20000000``).
    """

    def _repl(match: re.Match[str]) -> str:
        lhs, name, body, tail = _mount_call_parts(match)
        args = _split_syz_args(body)
        if len(args) < 5:
            return match.group(0)
        try:
            nsegs = int(args[3], 0)
        except ValueError:
            return match.group(0)
        if nsegs != 0 or args[4] not in ("0", "0x0"):
            return match.group(0)
        args[4] = "nil"
        if len(args) >= 7 and args[6] in ("0", "0x0"):
            args[6] = "&(0x7f0000000180)=''"
        return f"{lhs}{name}({', '.join(args)}){tail}"

    return _MOUNT_CALL_RE.sub(_repl, syz_text)


def rewrite_header_fault(syz_text: str) -> str:
    """Map JSON ``fault_call``/``fault_nth`` onto ``(fail_nth: N)``.

    This execprog has no ``-fault_call`` flag; the same injection is
    expressed on the call itself. Header ``fault_nth`` is the old
    0-based execprog flag; that binary wrote ``nth+1`` into
    ``/proc/thread-self/fail-nth``. The call property is 1-based and
    written as-is, so JSON ``3`` becomes ``(fail_nth: 4)``.
    """
    opts = parse_syz_options(syz_text)
    if not opts.get("fault") or "(fail_nth:" in syz_text:
        return syz_text
    call_i = int(opts.get("fault_call") or 0)
    nth = int(opts.get("fault_nth") or 0)
    if nth <= 0:
        return syz_text
    nth = nth + 1
    lines = syz_text.splitlines(keepends=True)
    idx = 0
    out: list[str] = []
    for line in lines:
        code = line.split("#", 1)[0].rstrip()
        if "(" in code:
            if idx == call_i:
                ended = line.endswith("\n")
                line = f"{code} (fail_nth: {nth})\n" if ended else f"{code} (fail_nth: {nth})"
            idx += 1
        out.append(line)
    return "".join(out)


# This execprog predates ``syz_open_procfs$pagemap`` / ``ioctl$PAGEMAP_SCAN``.
# Generic ``ioctl`` takes a byte buffer, so the typed ``pagemap_arg`` literal
# has to become the same 12 little-endian u64s (96 bytes).
_PAGEMAP_ARG_U64S = 12
_VMA_ADDR_RE = re.compile(
    r"^&\((0x[0-9a-fA-F]+)(?:/(0x[0-9a-fA-F]+))?\)(?:\s*=.*)?$"
)


# Program text uses 0x7f0000000000; this executor maps that window at 0x20000000.
_ENCODING_ADDR_BASE = 0x7F0000000000
_DATA_OFFSET = 0x20000000
_DATA_WINDOW = 0x1000000  # SYZ_NUM_PAGES * SYZ_PAGE_SIZE


def _syz_u64(text: str) -> int:
    text = text.strip()
    match = _VMA_ADDR_RE.match(text)
    if match:
        addr = int(match.group(1), 16)
    elif text.startswith(("0x", "0X")):
        addr = int(text, 16)
    elif text.lstrip("-").isdigit():
        addr = int(text, 10)
    else:
        return 0
    if text.startswith("&(") and _ENCODING_ADDR_BASE <= addr < _ENCODING_ADDR_BASE + _DATA_WINDOW:
        return _DATA_OFFSET + (addr - _ENCODING_ADDR_BASE)
    return addr


def _fill_pagemap_filename(arg: str) -> str:
    arg = arg.strip()
    if "=" not in arg:
        return f"{arg}='pagemap\\x00'"
    head, rhs = arg.rsplit("=", 1)
    if rhs.strip() in ("", "nil"):
        return f"{head}='pagemap\\x00'"
    return arg


def _pack_pagemap_arg(fields: list[str]) -> str:
    values = [_syz_u64(f) for f in fields]
    if not values:
        values = [0x60]
    size = values[0] if values[0] else _PAGEMAP_ARG_U64S * 8
    n = max(_PAGEMAP_ARG_U64S, (size + 7) // 8, len(values))
    values.extend([0] * (n - len(values)))
    return b"".join(struct.pack("<Q", v & 0xFFFFFFFFFFFFFFFF) for v in values[:n]).hex()


# This execprog predates ``dev_virtual_nci.txt`` / NFC genetlink. Typed
# ``$nci`` / ``$nfc`` / ``$NFC_CMD_*`` calls fail to parse; the C helper still
# looks up whatever name is in the buffer, so ``$nl80211`` + an ``ANYBLOB``
# of ``nfc\\0`` is equivalent. NCI response tags with no payload become the
# packed all-zero frames of the given write length.
_NCI_ZERO_FRAMES = {
    "NCI_OP_CORE_RESET_RSP": "400003000000",
    "NCI_OP_CORE_INIT_RSP": "400111" + "00" * 17,
    "NCI_OP_RF_DISCOVER_MAP_RSP": "41000100",
}
_NFC_CMD_NUM = {
    "NFC_CMD_GET_DEVICE": 1,
    "NFC_CMD_DEV_UP": 2,
    "NFC_CMD_DEV_DOWN": 3,
    "NFC_CMD_DEP_LINK_UP": 4,
    "NFC_CMD_DEP_LINK_DOWN": 5,
    "NFC_CMD_START_POLL": 6,
    "NFC_CMD_STOP_POLL": 7,
    "NFC_CMD_GET_SE": 0x1A,
    "NFC_CMD_SE_IO": 0x1B,
}
_NFC_ATTR_NUM = {
    "NFC_ATTR_DEVICE_INDEX": 1,
    "NFC_ATTR_PROTOCOLS": 3,
    "NFC_ATTR_SE_INDEX": 0x15,
    "NFC_ATTR_SE_APDU": 0x19,
}
_NCI_PTR_RE = re.compile(r"^(&\(0x[0-9a-fA-F]+\))(.*)$")


def _fill_virtual_nci_path(arg: str) -> str:
    if "virtual_nci" in arg:
        return arg
    match = _NCI_PTR_RE.match(arg.strip())
    if not match:
        return arg
    rest = match.group(2)
    if "='" in rest or '="' in rest or "ANY=" in rest:
        return arg
    return f"{match.group(1)}='/dev/virtual_nci\\x00'"


def _nci_write_blob(tag: str, size_arg: str) -> str | None:
    hex_blob = _NCI_ZERO_FRAMES.get(tag)
    if hex_blob is None:
        return None
    try:
        nbytes = int(size_arg, 0)
    except ValueError:
        nbytes = len(hex_blob) // 2
    raw = bytes.fromhex(hex_blob)
    if nbytes > len(raw):
        raw = raw + bytes(nbytes - len(raw))
    else:
        raw = raw[:nbytes]
    return raw.hex()


def _syz_quoted_bytes(val: str) -> bytes:
    inner = val[1:-1]
    out = bytearray()
    i = 0
    while i < len(inner):
        if inner.startswith("\\x", i) and i + 4 <= len(inner):
            out.append(int(inner[i + 2 : i + 4], 16))
            i += 4
        else:
            out.append(ord(inner[i]))
            i += 1
    return bytes(out)


def _nla_payload_hex(nla_len: int, val: str | None) -> str:
    want = max(0, nla_len - 4)
    pad = ((nla_len + 3) & ~3) - nla_len
    if val is None:
        raw = bytes(want)
    elif val[0] in "'\"":
        raw = _syz_quoted_bytes(val)
    else:
        n = int(val, 0)
        if want <= 0:
            raw = b""
        elif want == 1:
            raw = bytes([n & 0xFF])
        elif want == 2:
            raw = struct.pack("<H", n & 0xFFFF)
        elif want == 4:
            raw = struct.pack("<I", n & 0xFFFFFFFF)
        else:
            raw = (n & ((1 << (8 * want)) - 1)).to_bytes(want, "little")
    if len(raw) < want:
        raw = raw + bytes(want - len(raw))
    return (raw[:want] + bytes(pad)).hex()


def _nfc_nl_anyblob(nlmsg_len: str, family_res: str, flags: str, seq: str, pid: str, cmd: int, attrs: str) -> str:
    """Pack a genetlink header + attrs as ``ANYBLOB`` / ``ANYRES`` pieces."""

    def _u16(n: int) -> str:
        return struct.pack("<H", n & 0xFFFF).hex()

    def _u32(n: str) -> str:
        return struct.pack("<I", int(n, 0) & 0xFFFFFFFF).hex()

    pieces = [
        f'@ANYBLOB="{_u32(nlmsg_len)}"',
        f"@ANYRES16={family_res}",
        f'@ANYBLOB="{_u16(int(flags, 0))}{_u32(seq)}{_u32(pid)}{struct.pack("<I", cmd).hex()}"',
    ]
    named: list[str] = []
    holes: list[str] = []
    for match in re.finditer(r"@(NFC_ATTR_\w+)=\{([^}]*)\}", attrs):
        name = match.group(1)
        fields = [p.strip() for p in match.group(2).split(",") if p.strip()]
        nla_len = int(fields[0], 0) if fields else 4
        nla_type = _NFC_ATTR_NUM.get(name, 0)
        val: str | None = None
        rest = fields[1:]
        if rest and re.fullmatch(r"0x[0-9a-fA-F]+|\d+", rest[0]):
            nla_type = int(rest[0], 0)
            rest = rest[1:]
        if rest:
            val = rest[0]
        hdr = f'@ANYBLOB="{_u16(nla_len)}{_u16(nla_type)}"'
        if val and re.fullmatch(r"r\d+", val):
            holes.append(hdr)
        else:
            named.append(hdr)
            payload = _nla_payload_hex(nla_len, val)
            if payload:
                named.append(f'@ANYBLOB="{payload}"')
    pieces.extend(named)
    pieces.extend(holes)
    return f"ANY=[{', '.join(pieces)}]"


def _syz_ptr_body(arg: str) -> tuple[str, str] | None:
    arg = arg.strip()
    if "={" not in arg:
        return None
    prefix, body = arg.split("={", 1)
    if not body.endswith("}"):
        return None
    return prefix, body[:-1]


def _rewrite_nfc_sendmsg_msg(
    args: list[str],
    cmd: int,
    *,
    payload_ptr: str | None = None,
) -> list[str] | None:
    parsed = _syz_ptr_body(args[1])
    if parsed is None:
        return None
    hdr_prefix, hdr_body = parsed
    hdr_fields = _split_syz_args(hdr_body)
    if len(hdr_fields) < 3:
        return None
    iov = _syz_ptr_body(hdr_fields[2])
    if iov is None:
        return None
    iov_prefix, iov_body = iov
    iov_fields = _split_syz_args(iov_body)
    if len(iov_fields) < 2:
        return None
    payload = _syz_ptr_body(iov_fields[0])
    if payload is None:
        return None
    payload_prefix, nl_body = payload
    nl_fields = _split_syz_args(nl_body)
    if len(nl_fields) < 7:
        return None
    packed = _nfc_nl_anyblob(
        nl_fields[0],
        nl_fields[1],
        nl_fields[2],
        nl_fields[3],
        nl_fields[4],
        cmd,
        nl_fields[6],
    )
    if payload_ptr is not None:
        payload_prefix = payload_ptr
    iov_fields[0] = f"{payload_prefix}={packed}"
    hdr_fields[2] = f"{iov_prefix}={{{', '.join(iov_fields)}}}"
    args = list(args)
    args[1] = f"{hdr_prefix}={{{', '.join(hdr_fields)}}}"
    return args


def rewrite_nci_nfc_syscalls(syz_text: str) -> str:
    """Rewrite virtual-NCI / NFC netlink calls this execprog does not know."""
    out: list[str] = []
    nci_fd: str | None = None
    ioctl_addr: int | None = None
    overlapped_ioctl = False
    for line in syz_text.splitlines(keepends=True):
        ended = line.endswith("\n")
        raw = line[:-1] if ended else line
        comment = ""
        code = raw
        extra_ioctl = ""
        if "#" in code:
            code, comment = code.split("#", 1)
            comment = "#" + comment
        code = code.rstrip()
        if "openat$nci(" in code:
            pre, rest = code.split("openat$nci(", 1)
            body, post = rest.rsplit(")", 1)
            args = _split_syz_args(body)
            if len(args) >= 2:
                args[1] = _fill_virtual_nci_path(args[1])
            code = f"{pre}openat({', '.join(args)}){post}"
        if "ioctl$IOCTL_GET_NCIDEV_IDX(" in code:
            pre, rest = code.split("ioctl$IOCTL_GET_NCIDEV_IDX(", 1)
            body, post = rest.rsplit(")", 1)
            args = _split_syz_args(body)
            if args:
                nci_fd = args[0]
            if len(args) >= 3:
                addr_m = re.search(r"&\((0x[0-9a-fA-F]+)\)", args[2])
                if addr_m:
                    ioctl_addr = int(addr_m.group(1), 16)
            code = f"{pre}ioctl({', '.join(args)}){post}"
        if "syz_genetlink_get_family_id$nfc(" in code:
            pre, rest = code.split("syz_genetlink_get_family_id$nfc(", 1)
            body, post = rest.rsplit(")", 1)
            args = _split_syz_args(body)
            if args and "6e666300" not in args[0]:
                ptr = args[0]
                if "ANY=" not in ptr:
                    base = ptr.split("=", 1)[0] if "=" in ptr else ptr
                    args[0] = f'{base}=ANY=[@ANYBLOB="6e666300"]'
            code = f"{pre}syz_genetlink_get_family_id$nl80211({', '.join(args)}){post}"
        if "write$nci(" in code:
            pre, rest = code.split("write$nci(", 1)
            body, post = rest.rsplit(")", 1)
            args = _split_syz_args(body)
            if len(args) >= 3:
                tag_match = re.search(r"@(\w+)", args[1])
                if tag_match:
                    blob = _nci_write_blob(tag_match.group(1), args[2])
                    if blob is not None:
                        ptr = args[1].split("=", 1)[0]
                        args[1] = f'{ptr}="{blob}"'
                        code = f"{pre}write({', '.join(args)}){post}"
        cmd_match = re.search(r"sendmsg\$(NFC_CMD_\w+)\(", code)
        if cmd_match and cmd_match.group(1) in _NFC_CMD_NUM:
            cmd = _NFC_CMD_NUM[cmd_match.group(1)]
            pre, rest = code.split(f"sendmsg${cmd_match.group(1)}(", 1)
            body, post = rest.rsplit(")", 1)
            args = _split_syz_args(body)
            payload_ptr = None
            if len(args) >= 2 and ioctl_addr is not None:
                hdr = _syz_ptr_body(args[1])
                hdr_fields = _split_syz_args(hdr[1]) if hdr else []
                iov = _syz_ptr_body(hdr_fields[2]) if len(hdr_fields) >= 3 else None
                iov_fields = _split_syz_args(iov[1]) if iov else []
                payload = _syz_ptr_body(iov_fields[0]) if iov_fields else None
                if payload and payload[0].startswith("&("):
                    orig_addr = int(payload[0][2:-1], 16)
                    nl_len = int(_split_syz_args(payload[1])[0], 0)
                    idx_off = nl_len - 4
                    if not overlapped_ioctl:
                        payload_ptr = f"&(0x{ioctl_addr - idx_off:x})"
                        overlapped_ioctl = True
                    elif nci_fd is not None:
                        extra_ioctl = f"ioctl({nci_fd}, 0x0, &(0x{orig_addr + idx_off:x}))\n"
            rewritten = (
                _rewrite_nfc_sendmsg_msg(args, cmd, payload_ptr=payload_ptr)
                if len(args) >= 2
                else None
            )
            if rewritten is not None:
                code = f"{pre}sendmsg$nl_generic({', '.join(rewritten)}){post}"
        rebuilt = extra_ioctl + code + comment
        out.append(rebuilt + ("\n" if ended else ""))
    return "".join(out)


def rewrite_pagemap_ioctl(syz_text: str) -> str:
    """Rewrite ``$pagemap`` / ``$PAGEMAP_SCAN`` into calls this execprog knows."""
    out: list[str] = []
    for line in syz_text.splitlines(keepends=True):
        ended = line.endswith("\n")
        raw = line[:-1] if ended else line
        comment = ""
        code = raw
        if "#" in code:
            code, comment = code.split("#", 1)
            comment = "#" + comment
        code = code.rstrip()
        if "syz_open_procfs$pagemap(" in code:
            pre, rest = code.split("syz_open_procfs$pagemap(", 1)
            body, post = rest.rsplit(")", 1)
            args = _split_syz_args(body)
            if len(args) >= 2:
                args[1] = _fill_pagemap_filename(args[1])
            code = f"{pre}syz_open_procfs({', '.join(args)}){post}"
        if "ioctl$PAGEMAP_SCAN(" in code:
            pre, rest = code.split("ioctl$PAGEMAP_SCAN(", 1)
            body, post = rest.rsplit(")", 1)
            args = _split_syz_args(body)
            if len(args) >= 3:
                ptr = args[2].strip()
                if "={" in ptr:
                    addr_part, struct_body = ptr.split("={", 1)
                    struct_body = struct_body.rsplit("}", 1)[0]
                    blob = _pack_pagemap_arg(_split_syz_args(struct_body))
                    args[2] = f"{addr_part}=\"{blob}\""
                code = f"{pre}ioctl({', '.join(args)}){post}"
            else:
                code = f"{pre}ioctl({body}){post}"
        rebuilt = code + comment
        out.append(rebuilt + ("\n" if ended else ""))
    return "".join(out)


# JSON flags that this execprog understands as ``-enable`` names.
# Never use ``-disable close_fds`` (turns every feature on) or invent
# usb/cgroups. ``wifi`` is enabled from the program body (nl80211/wlan)
# even if JSON omitted it: ``close_fds:false`` only sets tun/net_dev,
# which has no wiphy, so ``NL80211_CMD_REQ_SET_REG`` never reaches
# ``restore_regulatory_settings``.
_JSON_ENABLE_FEATURES = ("vhci", "wifi", "ieee802154", "sysctl", "devlinkpci")


_FEATURE_BODY_HINTS = {
    "vhci": ("syz_emit_vhci",),
    "wifi": ("wlan", "nl80211", "80211"),
    "ieee802154": ("nl802154", "ieee802154", "wpan"),
    "devlinkpci": ("devlink",),
}

_SOUND_BODY_HINTS = (
    "openat$dsp",
    "openat$audio",
    "/dev/dsp",
    "/dev/audio",
    "SNDCTL_DSP",
)


def _syz_program_body(syz_text: str) -> str:
    return "\n".join(
        line for line in syz_text.splitlines() if line and not line.startswith("#")
    )


def _syz_uses_feature(syz_text: str, feat: str) -> bool:
    """Skip leftover JSON flags that are not used by the prog.

    Extra executor setup (ieee802154 radios, etc.) consumes ``fail_nth``
    slots, so a btrfs repro with a stale ``"ieee802154":true`` header
    never hits ``sendfile (fail_nth: 45)``.
    """
    hints = _FEATURE_BODY_HINTS.get(feat)
    if not hints:
        return True
    body = _syz_program_body(syz_text)
    return any(h in body for h in hints)


def _syz_uses_sound(syz_text: str) -> bool:
    """OSS ``/dev/dsp`` needs a PCM card; QEMU has none unless we add HDA."""
    body = _syz_program_body(syz_text)
    return any(h in body for h in _SOUND_BODY_HINTS)


def _execprog_enable_features(
    opts: dict | None, syz_text: str | None = None
) -> list[str]:
    """``close_fds:false`` → tun/net_dev; plus JSON-true features the prog uses."""
    enable: list[str] = []
    if opts and opts.get("close_fds") is False:
        # ``-disable close_fds`` means every feature ON (wifi/usb/cgroups),
        # which is not in the JSON and keeps writeback kworkers busy.
        # Omitting ``-enable`` is the same trap: execprog defaults both
        # ``-enable`` and ``-disable`` to ``none``, and ParseFeaturesFlags
        # then turns every feature on (wifi/vhci/usb/close_fds).
        # ``-disable all`` keeps fds open but also skips tun/net_dev, so
        # ``connect$inet6(..., @empty)`` returns EADDRNOTAVAIL and TLS/KCM
        # never run. ``-enable tun,net_dev`` keeps close_fds off (not in
        # the list) and sets up the dummy/tun devices the executor needs.
        enable.extend(["tun", "net_dev"])
        if syz_text is not None and _syz_uses_feature(syz_text, "wifi"):
            enable.append("wifi")
    if opts:
        for feat in _JSON_ENABLE_FEATURES:
            if not opts.get(feat):
                continue
            if syz_text is not None and not _syz_uses_feature(syz_text, feat):
                continue
            enable.append(feat)
    return list(dict.fromkeys(enable))


def _syz_mount_then_relpath(syz_text: str) -> bool:
    """True when the prog mounts an image then opens ``./bus`` in that cwd."""
    body = _syz_program_body(syz_text)
    if "syz_mount_image" not in body:
        return False
    return bool(re.search(r"open(?:\$\w+)?\([^;\n]*'\./bus", body))


def _execprog_use_tmpdir(opts: dict | None, syz_text: str | None = None) -> bool:
    """Per-iteration ``./N`` so a chdir'd btrfs mount does not occupy loop0.

    Header ``tmpdir`` is the official C equivalent. ``syz_mount_image``
    also needs it: ``chdir=1`` (or a later ``open('./bus')``) keeps the
    first loop0 busy, so ``-repeat`` never remounts.
    """
    if opts and opts.get("tmpdir"):
        return True
    if syz_text is None:
        return False
    if _syz_mount_then_relpath(syz_text):
        return True
    return "syz_mount_image" in _syz_program_body(syz_text)


def execprog_argv(
    opts: dict | None = None,
    prog_file: str = "repro.txt",
    syz_text: str | None = None,
    cover_file: str | None = None,
    repeat: str | None = None,
) -> list[str]:
    """CoHiker's ``-repeat 10``, unless the header has ``"repeat":true``.

    Official ``ExecprogCmd`` maps that JSON flag to ``-repeat 0`` (infinite).
    ``-repeat 10`` only runs 10 programs total across all procs, so a
    6-proc watch_queue race that needs overlapping 5s kill/restart waves
    never develops. This binary defaults to 128 procs; never leave that
    implicit. Header ``procs`` wins, otherwise 1.
    """
    procs = 1
    if opts and opts.get("procs") is not None:
        procs = int(opts["procs"])
    if repeat is None:
        repeat = "0" if opts and opts.get("repeat") else "10"
    cmd = ["./syz-execprog", "-repeat", repeat, "-procs", str(procs)]
    enable = _execprog_enable_features(opts, syz_text)
    enable_override = os.environ.get("CAUSALFL_EXECPROG_ENABLE", "").strip()
    if enable_override:
        cmd.extend(["-enable", enable_override])
    elif enable:
        cmd.extend(["-enable", ",".join(enable)])
    # This binary defaults to threaded=true. Syzbot JSON omits false fields
    # (``omitempty``), so a header without ``"threaded":true`` means sequential
    # syscalls. Threaded mode races NEWLINK vs sendmmsg and the packet never
    # hits sit/GUE. ``fault`` is also per-thread and must stay single-thread.
    threaded = bool(opts and opts.get("threaded")) and not (
        opts and opts.get("fault")
    )
    cmd.append("-threaded=true" if threaded else "-threaded=false")
    if cover_file:
        cmd.extend(["-cover", "-coverfile", cover_file])
    cmd.append(prog_file)
    return cmd


_HOST_REGDB = (
    Path("/lib/firmware/regulatory.db"),
    Path("/lib/firmware/regulatory.db.p7s"),
)
_CONTAINER_FW_INITRD = "/tmp/causalfl_fw.cpio"


def _newc_add(buf: bytearray, name: str, mode: int, data: bytes) -> None:
    name_b = name.encode("ascii") + b"\0"
    hdr = (
        b"070701"
        + (
            f"{0:08x}{mode & 0xFFFFFFFF:08x}{0:08x}{0:08x}{1:08x}{0:08x}"
            f"{len(data):08x}{0:08x}{0:08x}{0:08x}{0:08x}{len(name_b):08x}{0:08x}"
        ).encode()
    )
    buf.extend(hdr)
    buf.extend(name_b)
    buf.extend(b"\0" * ((4 - (len(buf) % 4)) % 4))
    buf.extend(data)
    buf.extend(b"\0" * ((4 - (len(buf) % 4)) % 4))


def firmware_initrd_bytes(
    files: list[tuple[str, bytes]] | None = None,
) -> bytes:
    """newc cpio with ``lib/firmware/regulatory.db`` for QEMU ``-initrd``."""
    if files is None:
        files = [
            (f"lib/firmware/{path.name}", path.read_bytes())
            for path in _HOST_REGDB
            if path.is_file()
        ]
    buf = bytearray()
    _newc_add(buf, ".", 0o40755, b"")
    _newc_add(buf, "lib", 0o40755, b"")
    _newc_add(buf, "lib/firmware", 0o40755, b"")
    for name, data in files:
        _newc_add(buf, name, 0o100644, data)
    _newc_add(buf, "TRAILER!!!", 0, b"")
    return bytes(buf)


def _prepare_firmware_initrd_sh() -> str:
    """Use a pre-staged newc image; the container has no ``cpio`` binary."""
    return f"""
INITRD_OPT=""
if [[ -s {_CONTAINER_FW_INITRD} ]]; then
  INITRD_OPT="-initrd {_CONTAINER_FW_INITRD}"
fi
"""


def _stage_firmware_initrd(container: str) -> None:
    blob = firmware_initrd_bytes()
    if not blob or b"regulatory.db" not in blob:
        return
    with tempfile.NamedTemporaryFile(suffix=".cpio", delete=False) as tmp:
        tmp.write(blob)
        host_path = tmp.name
    try:
        if in_target_container(container):
            shutil.copy2(host_path, _CONTAINER_FW_INITRD)
        else:
            subprocess.run(
                ["docker", "cp", host_path, f"{container}:{_CONTAINER_FW_INITRD}"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
    finally:
        Path(host_path).unlink(missing_ok=True)


def _stage_regulatory_firmware(container: str) -> None:
    """Copy ``regulatory.db`` into the container, guest rootfs, and initrd.

    cfg80211 loads it at boot; scp-after-ssh is too late for that path, so
    also plant the files in ``bullseye.img`` once and pass them via initrd.
    """
    files = [path for path in _HOST_REGDB if path.is_file()]
    if not files:
        return
    subprocess.run(
        exec_argv(container, "mkdir", "-p", "/lib/firmware"),
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )
    for src in files:
        if in_target_container(container):
            destination = Path("/lib/firmware") / src.name
            if src.resolve() != destination.resolve():
                shutil.copy2(src, destination)
        else:
            subprocess.run(
                ["docker", "cp", str(src), f"{container}:/lib/firmware/{src.name}"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
    subprocess.run(
        shell_argv(
            container,
            "set -e\n"
            "if [[ -f /root/rootfs/bullseye.img.lib_firmware_regdb ]]; then exit 0; fi\n"
            "mkdir -p /mnt/causalfl_rootfs\n"
            "mount -o loop /root/rootfs/bullseye.img /mnt/causalfl_rootfs\n"
            "mkdir -p /mnt/causalfl_rootfs/lib/firmware\n"
            "cp -f /lib/firmware/regulatory.db /lib/firmware/regulatory.db.p7s "
            "/mnt/causalfl_rootfs/lib/firmware/ 2>/dev/null || true\n"
            "umount /mnt/causalfl_rootfs\n"
            "touch /root/rootfs/bullseye.img.lib_firmware_regdb\n",
        ),
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=60,
    )


def _syz_text_for_case(case_id: str) -> str:
    path = resolve_kernel_syz_path(case_id, _REPO_ROOT)
    if path is None:
        raise FileNotFoundError(f"no testcase for {case_id}")
    if not (is_execprog_repro(path) or is_c_repro(path)):
        raise FileNotFoundError(
            f"testcase {path} is not a .syz/.prog or .c/.cprog reproducer"
        )
    return path.read_text(encoding="utf-8", errors="replace")


def _repro_kind_for_case(case_id: str) -> str:
    override = os.environ.get("CAUSALFL_REPRO_KIND", "").strip().lower()
    if override in {"syz", "gcc"}:
        return override
    path = resolve_kernel_syz_path(case_id, _REPO_ROOT)
    if is_c_repro(path):
        return "gcc"
    return "syz"


def _compile_c_repro(
    container: str,
    *,
    host_src: Path,
    host_bin: Path,
    container_src: Path,
    container_bin: Path,
) -> None:
    if in_target_container(container):
        script = _REPO_ROOT / "scripts" / "compile_c_repro.sh"
        src, dst = host_src, host_bin
    else:
        script = Path("/data/scripts/compile_c_repro.sh")
        src, dst = container_src, container_bin
    result = subprocess.run(
        exec_argv(container, "bash", str(script), str(src), str(dst)),
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    if result.returncode != 0:
        detail = (result.stdout or "") + (result.stderr or "")
        raise RuntimeError(f"gcc failed to compile C reproducer:\n{detail}")


def kernel_makefile_name(repro_kind: str | None = None) -> str:
    override = os.environ.get("CAUSALFL_KERNEL_MAKEFILE", "").strip()
    if override:
        return override
    kind = (repro_kind or os.environ.get("CAUSALFL_REPRO_KIND") or "syz").strip().lower()
    if kind == "gcc":
        return "Makefile.gcc"
    return "Makefile.syz"


def _should_rewrite_syz() -> bool:
    explicit = os.environ.get("CAUSALFL_SYZ_REWRITE")
    if explicit is not None and explicit.strip() != "":
        return explicit.strip().lower() not in {"0", "false", "no"}
    dataset = os.environ.get("CAUSALFL_KERNEL_DATASET", "cohiker").strip().lower()
    return dataset not in {"recent", "recent_syz", "recentsyz"}


def _rewrite_syz_for_runtime(syz_text: str) -> str:
    syz_text = syz_text.replace("\r\n", "\n").replace("\r", "\n")
    if not _should_rewrite_syz():
        return syz_text
    syz_text = rewrite_compressed_mount_images(syz_text)
    syz_text = rewrite_mount_fs_literal(syz_text)
    syz_text = rewrite_mount_opts_struct(syz_text)
    syz_text = rewrite_generic_mount_calls(syz_text)
    syz_text = rewrite_const_ptr_strings(syz_text)
    syz_text = rewrite_undersized_mount_images(syz_text)
    syz_text = rewrite_null_mount_segments(syz_text)
    syz_text = rewrite_pagemap_ioctl(syz_text)
    syz_text = rewrite_nci_nfc_syscalls(syz_text)
    syz_text = rewrite_header_fault(syz_text)
    return annotate_syz_resources(syz_text)


def _write_run_env(repro_dir: Path, values: dict[str, str]) -> None:
    text = "".join(f"{key}={shlex.quote(str(value))}\n" for key, value in values.items())
    (repro_dir / "run.env").write_text(text, encoding="utf-8")


def _make_repo_and_file(
    container: str | None, repro_kind: str | None = None
) -> tuple[str, str]:
    name = kernel_makefile_name(repro_kind)
    path = Path(name)
    if path.is_absolute():
        if in_target_container(container):
            return str(path.parent), path.name
        return "/data", path.name
    repo = str(_REPO_ROOT) if in_target_container(container) else "/data"
    return repo, name


_GUEST_COVER_FILE = "/root/cover.out"
_TRACE_SYMBOL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.$]*$")


def _trace_symbols(values: list[str] | tuple[str, ...] | None) -> list[str]:
    """Validate and deduplicate kernel symbols before placing them in a shell."""
    symbols: list[str] = []
    for value in values or ():
        symbol = str(value).strip()
        if not symbol or not _TRACE_SYMBOL_RE.fullmatch(symbol) or symbol in symbols:
            continue
        symbols.append(symbol)
    return symbols[:64]


def _kprobe_fetch_args(value: str | None) -> str:
    """Return a conservative kprobe fetch specification.

    The default captures the six register arguments as hexadecimal values.  A
    caller may provide named fetch arguments, but shell metacharacters and
    whitespace are rejected because the specification is written to tracefs.
    """
    default = " ".join(f"arg{i}=$arg{i}:x64" for i in range(1, 7))
    text = (value or "").strip()
    if not text:
        return default
    if "->" in text or "<-" in text:
        raise ValueError(
            "invalid kprobe fetch arguments: C-style field access is unsupported; "
            "use tracefs offsets such as +8($arg2):x8"
        )
    if len(text) > 512 or any(ch in text for ch in "\n\r;|&`\"'"):
        raise ValueError("invalid kprobe fetch arguments")
    if not re.fullmatch(r"[A-Za-z0-9_$%@+:.()=<>/\\-]+(?:\s+[A-Za-z0-9_$%@+:.()=<>/\\-]+)*", text):
        raise ValueError("invalid kprobe fetch arguments")
    return text


def run_qemu_case(
    container: str,
    case_id: str,
    *,
    repro_timeout: int = 180,
    scan_wait: int = 30,
    extra_append: str = "",
    cover: bool = False,
    ftrace_functions: list[str] | tuple[str, ...] | None = None,
    kprobe_function: str | None = None,
    kprobe_fetch_args: str | None = None,
    kprobe_type: str = "entry",
    kprobe_stacktrace: bool = False,
    qemu_timeout: int | None = None,
) -> str:
    """Prepare the reproducer, then run ``make test|ftrace|kprobe``.

    Dataset Makefiles choose kernel, rootfs, and syzkaller paths. This helper
    writes ``repro.syz`` (execprog) or ``repro.c`` + ``repro.bin`` (gcc) plus
    ``run.env`` with ``REPRO_KIND``, then selects the Make target. QEMU flags
    match CoHiker ``testcase_reproduce.py`` (snapshot=on so the rootfs image
    is not dirtied).
    """
    if qemu_timeout is None:
        qemu_timeout = int(os.environ.get("CAUSALFL_QEMU_TIMEOUT_SECONDS", "2700"))
    if qemu_timeout <= 0:
        raise ValueError("qemu_timeout must be positive")

    original_syz = _syz_text_for_case(case_id)
    repro_kind = _repro_kind_for_case(case_id)
    syz_text = original_syz if repro_kind == "gcc" else _rewrite_syz_for_runtime(original_syz)
    repro_dir = kernel_runtime_dir(case_id)
    if in_target_container(container):
        container_repro_dir = repro_dir
    else:
        # The repository is mounted at /data in test1. Translate the host
        # output path before using it in the container's shell.
        container_repro_dir = Path("/data") / repro_dir.relative_to(_REPO_ROOT)
    repro_dir.mkdir(parents=True, exist_ok=True)
    if repro_kind == "gcc":
        (repro_dir / "repro.c").write_text(syz_text, encoding="utf-8")
        (repro_dir / "original.c").write_text(original_syz, encoding="utf-8")
        for leftover in ("repro.syz", "original.syz"):
            (repro_dir / leftover).unlink(missing_ok=True)
        executed_name = "repro.c"
        original_name = "original.c"
        _compile_c_repro(
            container,
            host_src=repro_dir / "repro.c",
            host_bin=repro_dir / "repro.bin",
            container_src=container_repro_dir / "repro.c",
            container_bin=container_repro_dir / "repro.bin",
        )
    else:
        (repro_dir / "repro.syz").write_text(syz_text, encoding="utf-8")
        (repro_dir / "original.syz").write_text(original_syz, encoding="utf-8")
        for leftover in ("repro.c", "original.c", "repro.bin"):
            (repro_dir / leftover).unlink(missing_ok=True)
        executed_name = "repro.syz"
        original_name = "original.syz"
    linux_dir = os.environ.get("COHIKER_LINUX_DIR", "/root/linux")
    revision = subprocess.run(
        exec_argv(container, "git", "-C", linux_dir, "rev-parse", "HEAD"),
        capture_output=True, text=True, check=False, timeout=30,
    )
    (repro_dir / "case_manifest.json").write_text(json.dumps({
        "case_id": case_id,
        "kernel_commit": revision.stdout.strip() if revision.returncode == 0 else None,
        "repro_kind": repro_kind,
        "original_syz": original_name,
        "executed_syz": executed_name,
        "executed_syz_sha256": hashlib.sha256(syz_text.encode()).hexdigest(),
        "coverage_call_indices": "executed_syz",
    }, indent=2) + "\n", encoding="utf-8")
    opts = parse_syz_options(syz_text) if repro_kind == "syz" else {}
    ftrace_symbols = _trace_symbols(ftrace_functions)
    kprobe_symbols = _trace_symbols([kprobe_function]) if kprobe_function else []
    if kprobe_function and not kprobe_symbols:
        raise ValueError("invalid kprobe function symbol")
    if kprobe_type not in {"entry", "return"}:
        raise ValueError("invalid kprobe type")
    kprobe_symbol = kprobe_symbols[0] if kprobe_symbols else None
    kprobe_args = _kprobe_fetch_args(kprobe_fetch_args) if kprobe_symbol else ""
    if ftrace_symbols or kprobe_symbol:
        # A case runtime directory is reused across tool calls. Remove both
        # trace outputs before a new run so a ftrace-only result cannot be
        # mistaken for a stale kprobe observation (and vice versa).
        for trace_name in ("ftrace.log", "kprobe.log", "kprobe_setup.log"):
            (repro_dir / trace_name).unlink(missing_ok=True)
    if repro_kind == "gcc":
        cmd = "./repro"
        use_tmpdir = False
        workdir = "/root"
        cover = False
    else:
        cmd = shlex.join(
            execprog_argv(
                opts,
                prog_file="repro.txt",
                syz_text=syz_text,
                cover_file=_GUEST_COVER_FILE if cover else None,
                # -coverfile is only written when execprog exits cleanly. A crash
                # or hang mid-run loses everything, so coverage runs execute the
                # program once (one execution unit) instead of repeating.
                repeat="1" if cover else None,
            )
        )
        use_tmpdir = _execprog_use_tmpdir(opts, syz_text)
        workdir = "/tmp/syzkaller-repro" if use_tmpdir else "/root"
    _stage_regulatory_firmware(container)
    _stage_firmware_initrd(container)
    append = (
        "console=ttyS0 root=/dev/sda earlyprintk=serial net.ifnames=0 "
        "ip=10.0.2.15::10.0.2.2:255.255.255.0::eth0:off"
    )
    if extra_append:
        append = f"{append} {extra_append}"
    if ftrace_symbols:
        (repro_dir / "ftrace.symbols").write_text(
            "\n".join(ftrace_symbols) + "\n", encoding="utf-8"
        )
    else:
        (repro_dir / "ftrace.symbols").unlink(missing_ok=True)
    if kprobe_symbol:
        probe_prefix = "r" if kprobe_type == "return" else "p"
        (repro_dir / "kprobe.event").write_text(
            f"{probe_prefix}:cfl_input {kprobe_symbol} {kprobe_args}\n",
            encoding="utf-8",
        )
    else:
        (repro_dir / "kprobe.event").unlink(missing_ok=True)
    dsp_prep = ""
    if _syz_uses_sound(syz_text):
        dsp_prep = (
            "modprobe snd-dummy 2>/dev/null || true; "
            "mknod /dev/dsp c 14 3 2>/dev/null || true; "
            "mknod /dev/audio c 14 4 2>/dev/null || true; "
            "test -e /dev/dsp && echo CFL_DSP_YES >/dev/kmsg || echo CFL_DSP_NO >/dev/kmsg; "
        )
    if kprobe_symbol:
        target = "kprobe"
    elif ftrace_symbols:
        target = "ftrace"
    else:
        target = "test"
    _write_run_env(repro_dir, {
        "CASE": case_id,
        "APPEND": append,
        "REPRO_KIND": repro_kind,
        "EXECPROG_CMD": cmd,
        "WORKDIR": workdir,
        "USE_TMPDIR": "1" if use_tmpdir else "0",
        "REPRO_TIMEOUT": str(repro_timeout),
        "SCAN_WAIT": str(scan_wait),
        "COVER": "1" if cover else "0",
        "DSP_PREP": dsp_prep,
        "SOUND_OPT": "-soundhw hda" if _syz_uses_sound(syz_text) else "",
        "KPROBE_STACKTRACE": "1" if kprobe_stacktrace else "0",
        "INITRD": "/tmp/causalfl_fw.cpio",
    })
    repo, makefile = _make_repo_and_file(container, repro_kind)
    make_args = [
        f"REPRO_DIR={container_repro_dir}",
        f"CASE={case_id}",
    ]
    dataset = os.environ.get("CAUSALFL_KERNEL_DATASET", "").strip()
    if dataset:
        make_args.append(f"KERNEL_DATASET={dataset}")
    image_file = os.environ.get("IMAGE_FILE", "").strip()
    for key in (
        "LINUX",
        "IMAGE_DIR",
        "SSH_KEY",
        "SYZ_BIN",
        "KERNEL_IMAGE",
        "QEMU_EXTRA",
    ):
        value = os.environ.get(key, "").strip()
        if value:
            make_args.append(f"{key}={value}")
    if image_file:
        make_args.append(f"IMAGE_FILE={image_file}")
        image_format = os.environ.get("IMAGE_FORMAT", "").strip()
        if image_format:
            make_args.append(f"IMAGE_FORMAT={image_format}")
    command = exec_argv(
        container,
        "make",
        "-C",
        repo,
        "-f",
        makefile,
        target,
        *make_args,
    )
    serial_path = repro_dir / "vm.serial.log"
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            check=False,
            timeout=qemu_timeout,
        )
        serial = ""
        if serial_path.is_file():
            serial = serial_path.read_text(encoding="utf-8", errors="replace")
        return (result.stdout or "") + ("\n" + serial if serial else "")
    except subprocess.TimeoutExpired:
        subprocess.run(
            shell_argv(
                container,
                "if [[ -f /tmp/causalfl_vm.pid ]]; then kill -9 $(cat /tmp/causalfl_vm.pid) 2>/dev/null || true; fi"
                "; pkill -9 qemu-system-x86 2>/dev/null || true",
            ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        )
        return (
            "CFL_QEMU_STATUS=execution_timeout\n"
            f"qemu execution timeout after {qemu_timeout} seconds\n"
        )
