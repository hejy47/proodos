from __future__ import annotations

import math
import re
from collections import Counter
from typing import Mapping


_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
_CAMEL_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Split code-ish text into lowercase search tokens.

    Identifiers are split on non-alphanumeric characters first, then on
    camelCase / UPPER_CASE boundaries so ``trimValue`` and ``TRIM_VALUE``
    both yield ``["trim", "value"]``.
    """
    tokens: list[str] = []
    for raw in _TOKEN_RE.findall(text):
        for piece in _CAMEL_RE.findall(raw):
            tokens.append(piece.lower())
    return tokens


class BM25Index:
    """Small self-contained Okapi BM25 index over string documents."""

    def __init__(self, *, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.doc_ids: list[str] = []
        self.doc_lengths: list[int] = []
        self.avg_doc_length: float = 0.0
        # term -> {doc_position: term_frequency}
        self.postings: dict[str, dict[int, int]] = {}

    @classmethod
    def build(cls, documents: Mapping[str, str], *, k1: float = 1.5, b: float = 0.75) -> "BM25Index":
        index = cls(k1=k1, b=b)
        total_length = 0
        for position, (doc_id, text) in enumerate(documents.items()):
            tokens = tokenize(text)
            index.doc_ids.append(doc_id)
            index.doc_lengths.append(len(tokens))
            total_length += len(tokens)
            for term, frequency in Counter(tokens).items():
                posting = index.postings.setdefault(term, {})
                posting[position] = frequency
        if index.doc_ids:
            index.avg_doc_length = total_length / len(index.doc_ids)
        return index

    def search(self, query: str, *, top_k: int = 10) -> list[tuple[str, float]]:
        """Return ``(doc_id, score)`` pairs sorted by descending score."""
        if not self.doc_ids or top_k <= 0:
            return []
        doc_count = len(self.doc_ids)
        scores: dict[int, float] = {}
        for term in set(tokenize(query)):
            posting = self.postings.get(term)
            if not posting:
                continue
            doc_freq = len(posting)
            idf = math.log(1.0 + (doc_count - doc_freq + 0.5) / (doc_freq + 0.5))
            for position, frequency in posting.items():
                doc_length = self.doc_lengths[position]
                norm = frequency + self.k1 * (1.0 - self.b + self.b * doc_length / self.avg_doc_length)
                scores[position] = scores.get(position, 0.0) + idf * (frequency * (self.k1 + 1.0)) / norm
        ranked = sorted(scores.items(), key=lambda item: (-item[1], self.doc_ids[item[0]]))
        return [(self.doc_ids[position], score) for position, score in ranked[:top_k]]

    def to_dict(self) -> dict[str, object]:
        return {
            "k1": self.k1,
            "b": self.b,
            "doc_ids": self.doc_ids,
            "doc_lengths": self.doc_lengths,
            "avg_doc_length": self.avg_doc_length,
            "postings": {
                term: [[position, frequency] for position, frequency in sorted(posting.items())]
                for term, posting in sorted(self.postings.items())
            },
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "BM25Index":
        index = cls(k1=float(payload["k1"]), b=float(payload["b"]))
        index.doc_ids = [str(doc_id) for doc_id in payload["doc_ids"]]  # type: ignore[index]
        index.doc_lengths = [int(length) for length in payload["doc_lengths"]]  # type: ignore[index]
        index.avg_doc_length = float(payload["avg_doc_length"])
        postings = payload["postings"]
        if isinstance(postings, Mapping):
            for term, pairs in postings.items():
                index.postings[str(term)] = {int(position): int(frequency) for position, frequency in pairs}  # type: ignore[misc]
        return index
