"""Deterministic fake provider for tests and CI (hard rule 11). No network."""

import hashlib
import math
import random
from collections.abc import Sequence

from app.llm.base import EMBEDDING_DIM, EmbeddingError


class FakeProvider:
    def __init__(self, fail_times: int = 0, retryable: bool = True) -> None:
        self.fail_times = fail_times
        self.retryable = retryable
        self.calls: list[int] = []

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(len(texts))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise EmbeddingError("fake provider failure", retryable=self.retryable)
        return [_vector(t) for t in texts]


def _vector(text: str) -> list[float]:
    seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest(), "big")
    rng = random.Random(seed)
    raw = [rng.gauss(0.0, 1.0) for _ in range(EMBEDDING_DIM)]
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw]
