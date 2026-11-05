"""Call the provider and validate its JSON against a Pydantic model (hard rule 3)."""

from typing import Any

from pydantic import BaseModel, ValidationError

from app.llm.base import LLMMalformedOutput, LLMProvider

_ATTEMPTS = 2


def generate_validated[T: BaseModel](
    provider: LLMProvider,
    prompt: str,
    schema: dict[str, Any],
    model: type[T],
    *,
    system: str | None = None,
    temperature: float = 0.2,
    max_output_tokens: int = 1024,
) -> T:
    """Generate JSON and parse it into `model`.

    Malformed output (from the provider or from validation) is retried exactly once.
    Every other LLMError propagates at once: blocked or auth errors will not improve,
    and rate-limit / unavailable handling belongs to the caller (the job queue).
    """
    for attempt in range(_ATTEMPTS):
        try:
            data = provider.generate(
                prompt,
                schema,
                system=system,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
            )
            try:
                return model.model_validate(data)
            except ValidationError:
                # Fixed text: never echo the model output or validation details.
                raise LLMMalformedOutput(
                    "The model returned output that does not match the expected schema.",
                    retryable=True,
                ) from None
        except LLMMalformedOutput:
            if attempt == _ATTEMPTS - 1:
                raise
    raise AssertionError("unreachable")  # pragma: no cover
