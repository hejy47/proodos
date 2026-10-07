from __future__ import annotations

import re


OBS_MARKER = "causalfl_observe"

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def split_function_source(source_code: str) -> tuple[str, str]:
    """Split a C function definition into (header-through-'{', body-through-'}')."""
    brace = source_code.find("{")
    if brace < 0:
        raise ValueError("function source has no opening brace")
    return source_code[: brace + 1], source_code[brace + 1 :]


def parse_param_names(header: str) -> list[tuple[str, bool]]:
    """Parse (name, printable) pairs from a function header's parameter list.

    ``printable`` is False for by-value struct/union parameters and function
    pointers, which cannot be cast to ``unsigned long`` for a generic probe.
    """
    start = header.find("(")
    depth = 0
    end = -1
    for index in range(start, len(header)):
        ch = header[index]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                end = index
                break
    if start < 0 or end < 0:
        return []
    params_text = header[start + 1 : end].strip()
    if not params_text or params_text == "void":
        return []
    params: list[tuple[str, bool]] = []
    for raw in _split_top_level_commas(params_text):
        decl = raw.strip()
        if not decl or decl == "...":
            continue
        if "(" in decl:  # function pointer parameter
            continue
        names = _IDENT_RE.findall(decl)
        if len(names) < 2:
            continue
        name = names[-1]
        by_value_aggregate = ("struct" in names or "union" in names) and "*" not in decl
        params.append((name, not by_value_aggregate))
    return params


def _split_top_level_commas(text: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in text:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def build_observation_source(source_code: str, function_name: str, expressions: list[str]) -> str:
    """Insert a pr_err probe printing params and optional expressions at entry."""
    header, body = split_function_source(source_code)
    params = parse_param_names(header)
    printable_params = [name for name, printable in params if printable]
    labels = [(name, name) for name in printable_params]
    labels.extend((f"expr{index}", expr) for index, expr in enumerate(expressions))
    if not labels:
        probe = (
            f'\n\tpr_err("{OBS_MARKER} {function_name} call: (no printable params)\\n");\n'
        )
        return header + probe + body
    fmt = " ".join(f"{label}=0x%lx" for label, _ in labels)
    args = ", ".join(f"(unsigned long)({expr})" for _, expr in labels)
    probe = f'\n\tpr_err("{OBS_MARKER} {function_name} call: {fmt}\\n", {args});\n'
    return header + probe + body


def build_stub_source(source_code: str, stub_c: str) -> str:
    """Replace a C function body with the given stub statements."""
    header, _body = split_function_source(source_code)
    stub_lines = "\n".join(
        "\t" + line if line.strip() else line
        for line in _extract_stub_body(source_code, stub_c).splitlines()
    )
    return f"{header}\n{stub_lines}\n}}\n"


def _extract_stub_body(source_code: str, stub_c: str) -> str:
    """Accept a bare body, or peel the body out of a full function definition.

    Agents sometimes paste the entire function (signature + body) instead of
    just the body; nesting that inside the original header breaks compilation.
    Agents also sometimes emit JSON-style escapes (literal ``\\n``/``\\t``)
    instead of real newlines; unescape those single-line blobs first.
    """
    body = _unescape_stub(stub_c.strip())
    header, _ = split_function_source(source_code)
    name_match = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", header)
    function_name = name_match.group(1) if name_match else None
    if not function_name:
        return body
    call = re.search(rf"{re.escape(function_name)}\s*\(", body)
    if call is None:
        return body
    brace = body.find("{", call.end())
    end = body.rfind("}")
    if brace < 0 or end <= brace:
        return body
    return body[brace + 1 : end].strip()


def _unescape_stub(stub: str) -> str:
    """Decode JSON-style escapes when the whole stub is one escaped line.

    Only applies when the stub has escape sequences but no real newlines, so
    genuine C escapes (e.g. "\\n" inside a printk format string) in multi-line
    stubs are left untouched.
    """
    if "\\n" in stub and "\n" not in stub:
        return stub.replace("\\n", "\n").replace("\\t", "\t")
    return stub


def replace_function_in_file(file_text: str, original_function: str, patched_function: str) -> str:
    """Swap the exact original function text inside a translation unit."""
    if original_function not in file_text:
        raise ValueError("original function source not found in target file")
    return file_text.replace(original_function, patched_function, 1)
