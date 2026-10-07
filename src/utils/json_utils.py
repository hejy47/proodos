from __future__ import annotations

from dataclasses import asdict, is_dataclass
from enum import Enum
import io
import json
import os
from pathlib import Path
import tempfile
from typing import Iterable
import jsonlines

def to_jsonable(value: object) -> object:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, set):
        return sorted(value)
    to_list = getattr(value, "tolist", None)
    if callable(to_list):
        return to_list()
    return value


def json_dumps(payload: object, *, indent: int = 2, sort_keys: bool = True) -> str:
    return json.dumps(payload, indent=indent, sort_keys=sort_keys, default=to_jsonable)


def load_jsonl(path: Path, *, limit: int | None = None) -> list[dict[str, object]]:
    if not path.exists():
        return []

    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return []

    entries = _load_jsonl_with_jsonlines(text, limit=limit)
    if entries is not None:
        return entries
    return _load_json_objects_from_buffer(text, limit=limit)


def _load_jsonl_with_jsonlines(text: str, *, limit: int | None = None) -> list[dict[str, object]] | None:
    entries: list[dict[str, object]] = []
    reader = jsonlines.Reader(io.StringIO(text))
    try:
        for payload in reader:
            if not isinstance(payload, dict):
                continue
            entries.append(payload)
            if limit is not None and len(entries) >= limit:
                break
    except (jsonlines.InvalidLineError, json.JSONDecodeError):
        return None
    finally:
        reader.close()
    return entries


def _load_json_objects_from_buffer(text: str, *, limit: int | None = None) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    decoder = json.JSONDecoder()
    index = 0
    length = len(text)
    while index < length:
        while index < length and text[index].isspace():
            index += 1
        if index >= length:
            break
        try:
            payload, next_index = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            break
        index = next_index
        if not isinstance(payload, dict):
            continue
        entries.append(payload)
        if limit is not None and len(entries) >= limit:
            break
    return entries


def write_jsonl(path: Path, entries: Iterable[object]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        writer = jsonlines.Writer(
            handle,
            dumps=lambda entry: json.dumps(entry, default=to_jsonable),
        )
        try:
            for entry in entries:
                writer.write(entry)
        finally:
            writer.close()


def write_json_atomic(path: Path, payload: object, *, indent: int = 2, sort_keys: bool = False) -> None:
    """Write a complete JSON document atomically in the destination directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=indent, sort_keys=sort_keys, default=to_jsonable) + "\n"
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
