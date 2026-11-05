import pytest
from pydantic import BaseModel, ConfigDict

from app.llm.base import (
    LLMAuthError,
    LLMBlocked,
    LLMError,
    LLMMalformedOutput,
    LLMRateLimited,
    LLMUnavailable,
)
from app.llm.fake import FakeProvider
from app.llm.structured import generate_validated

SCHEMA = {"type": "object"}
SENTINEL = "SECRET-MODEL-OUTPUT-9f3a"


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    score: int


def run(provider: FakeProvider) -> Answer:
    return generate_validated(provider, "p", SCHEMA, Answer)


def test_valid_output_is_parsed_and_args_forwarded() -> None:
    provider = FakeProvider()
    provider.queue_generate({"text": "hi", "score": 3})
    result = generate_validated(
        provider, "p", SCHEMA, Answer, system="s", temperature=0.5, max_output_tokens=77
    )
    assert result == Answer(text="hi", score=3)
    call = provider.generate_calls[0]
    assert (call.prompt, call.schema, call.system) == ("p", SCHEMA, "s")
    assert (call.temperature, call.max_output_tokens) == (0.5, 77)


def test_invalid_then_valid_succeeds_with_two_calls() -> None:
    provider = FakeProvider()
    provider.queue_generate({"text": "hi"}, {"text": "hi", "score": 1})
    assert run(provider).score == 1
    assert len(provider.generate_calls) == 2


def test_invalid_twice_raises_after_exactly_two_calls() -> None:
    provider = FakeProvider()
    provider.queue_generate({"text": "hi"}, {"score": "x"}, {"text": "hi", "score": 1})
    with pytest.raises(LLMMalformedOutput) as exc:
        run(provider)
    assert exc.value.retryable is True
    assert len(provider.generate_calls) == 2


def test_provider_malformed_once_then_good_works() -> None:
    provider = FakeProvider()
    provider.queue_generate(
        LLMMalformedOutput("bad json", retryable=True), {"text": "hi", "score": 2}
    )
    assert run(provider).score == 2
    assert len(provider.generate_calls) == 2


def test_provider_malformed_twice_reraises_second() -> None:
    provider = FakeProvider()
    second = LLMMalformedOutput("second", retryable=True)
    provider.queue_generate(LLMMalformedOutput("first", retryable=True), second)
    with pytest.raises(LLMMalformedOutput) as exc:
        run(provider)
    assert exc.value is second
    assert len(provider.generate_calls) == 2


@pytest.mark.parametrize(
    "error",
    [
        LLMBlocked("blocked", retryable=False),
        LLMRateLimited("slow down", retryable=True, retry_after=3.0),
        LLMUnavailable("down", retryable=True),
        LLMAuthError("auth", retryable=False),
    ],
)
def test_other_errors_propagate_without_retry(error: LLMError) -> None:
    provider = FakeProvider()
    provider.queue_generate(error, {"text": "hi", "score": 1})
    with pytest.raises(type(error)) as exc:
        run(provider)
    assert exc.value is error
    assert len(provider.generate_calls) == 1


def test_error_after_malformed_propagates_without_third_call() -> None:
    provider = FakeProvider()
    provider.queue_generate({"bad": 1}, LLMBlocked("blocked", retryable=False))
    with pytest.raises(LLMBlocked):
        run(provider)
    assert len(provider.generate_calls) == 2


def test_message_does_not_leak_model_output() -> None:
    provider = FakeProvider()
    bad = {"text": SENTINEL, "score": SENTINEL}
    provider.queue_generate(bad, bad)
    with pytest.raises(LLMMalformedOutput) as exc:
        run(provider)
    assert SENTINEL not in str(exc.value)
    assert SENTINEL not in repr(exc.value)
    assert exc.value.__cause__ is None
    assert exc.value.__suppress_context__ is True


def test_extra_field_rejected_by_forbidding_model() -> None:
    provider = FakeProvider()
    extra = {"text": "hi", "score": 1, "tool": "rm -rf"}
    provider.queue_generate(extra, extra)
    with pytest.raises(LLMMalformedOutput):
        run(provider)


def test_extra_field_ignored_by_default_model() -> None:
    class Loose(BaseModel):
        text: str

    provider = FakeProvider()
    provider.queue_generate({"text": "hi", "extra": 1})
    assert generate_validated(provider, "p", SCHEMA, Loose) == Loose(text="hi")
    assert len(provider.generate_calls) == 1


def test_missing_field_is_malformed() -> None:
    provider = FakeProvider()
    provider.queue_generate({}, {})
    with pytest.raises(LLMMalformedOutput):
        run(provider)
