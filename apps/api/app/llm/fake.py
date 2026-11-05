"""Deterministic fake provider for tests and CI (hard rule 11). No network."""

import copy
import hashlib
import math
import random
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from app.llm.base import EMBEDDING_DIM, EmbeddingError, EmbedKind, LLMError


@dataclass(frozen=True)
class GenerateCall:
    prompt: str
    schema: dict[str, Any]
    system: str | None
    temperature: float
    max_output_tokens: int


class FakeProvider:
    def __init__(
        self,
        fail_times: int = 0,
        retryable: bool = True,
        generate_responses: Iterable[dict[str, Any] | LLMError] = (),
    ) -> None:
        self.fail_times = fail_times
        self.retryable = retryable
        self.calls: list[int] = []
        self.kinds: list[EmbedKind] = []
        self.generate_calls: list[GenerateCall] = []
        self._responses: deque[dict[str, Any] | LLMError] = deque(generate_responses)

    def embed(self, texts: Sequence[str], *, kind: EmbedKind = "document") -> list[list[float]]:
        self.calls.append(len(texts))
        self.kinds.append(kind)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise EmbeddingError("fake provider failure", retryable=self.retryable)
        # Vectors ignore `kind` so query and document vectors match in tests.
        return [_vector(t) for t in texts]

    def queue_generate(self, *responses: dict[str, Any] | LLMError) -> None:
        self._responses.extend(responses)

    def generate(
        self,
        prompt: str,
        schema: dict[str, Any],
        *,
        system: str | None = None,
        temperature: float = 0.2,
        max_output_tokens: int = 1024,
    ) -> dict[str, Any]:
        self.generate_calls.append(
            GenerateCall(prompt, schema, system, temperature, max_output_tokens)
        )
        if not self._responses:
            raise AssertionError("FakeProvider.generate called with no scripted response")
        item = self._responses.popleft()
        if isinstance(item, LLMError):
            raise item
        return copy.deepcopy(item)


def _vector(text: str) -> list[float]:
    seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest(), "big")
    rng = random.Random(seed)
    raw = [rng.gauss(0.0, 1.0) for _ in range(EMBEDDING_DIM)]
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw]
