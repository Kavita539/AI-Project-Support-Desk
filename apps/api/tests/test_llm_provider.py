import logging
from typing import Any

import pytest

import app.config as config
import app.llm as llm
import app.llm.gemini as gemini_module
from app.config import Settings, get_settings
from app.llm import FakeProvider, GeminiProvider, clear_provider_cache, get_provider


def configure(monkeypatch: pytest.MonkeyPatch, provider: str, key: str | None = None) -> None:
    monkeypatch.setenv("LLM_PROVIDER", provider)
    if key is None:
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    else:
        monkeypatch.setenv("GEMINI_API_KEY", key)
    get_settings.cache_clear()
    clear_provider_cache()


def test_fake_returns_a_new_instance_each_call(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(monkeypatch, "fake")
    first, second = get_provider(), get_provider()
    assert isinstance(first, FakeProvider)
    assert isinstance(second, FakeProvider)
    assert first is not second


def test_fake_warning_logged_once_per_process(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    configure(monkeypatch, "fake")
    monkeypatch.setattr(llm, "_fake_warned", False)
    with caplog.at_level(logging.WARNING, logger="app.llm"):
        get_provider()
        get_provider()
    warnings = [r for r in caplog.records if "embeddings are NOT real" in r.getMessage()]
    assert len(warnings) == 1


def test_gemini_returns_provider_without_leaking_key(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(monkeypatch, "gemini", "KEY123")
    provider = get_provider()
    assert isinstance(provider, GeminiProvider)
    assert "KEY123" not in repr(provider)


def test_gemini_is_cached_until_cleared(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(monkeypatch, "gemini", "KEY123")
    first = get_provider()
    assert get_provider() is first
    clear_provider_cache()
    assert get_provider() is not first


def test_gemini_blank_key_raises_without_key_text(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (None, "", "   "):
        configure(monkeypatch, "gemini", key)
        with pytest.raises(RuntimeError, match="GEMINI_API_KEY") as exc:
            get_provider()
        assert "KEY123" not in str(exc.value)


def test_get_provider_normalises_provider_name(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(monkeypatch, "Gemini ", "KEY123")
    assert isinstance(get_provider(), GeminiProvider)


@pytest.mark.parametrize("name", ["openai", ""])
def test_unknown_or_empty_provider_raises(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    configure(monkeypatch, name)
    with pytest.raises(RuntimeError, match="LLM_PROVIDER"):
        get_provider()


# --- Settings.missing_required ---


def settings(*, llm_provider: str, gemini_api_key: str = "", **extra: Any) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url="postgresql://x",
        supabase_url="https://p.supabase.co",
        llm_provider=llm_provider,
        gemini_api_key=gemini_api_key,
        **extra,
    )


@pytest.mark.parametrize("name", ["", "openai"])
def test_missing_required_flags_bad_provider(name: str) -> None:
    assert settings(llm_provider=name).missing_required() == ["LLM_PROVIDER"]


@pytest.mark.parametrize("key", ["", "   "])
def test_missing_required_flags_gemini_without_key(key: str) -> None:
    assert settings(llm_provider="gemini", gemini_api_key=key).missing_required() == [
        "GEMINI_API_KEY"
    ]


def test_missing_required_accepts_good_llm_config() -> None:
    assert settings(llm_provider="gemini", gemini_api_key="KEY123").missing_required() == []
    assert settings(llm_provider="fake").missing_required() == []


def test_missing_required_order_is_urls_then_llm() -> None:
    bare = Settings(_env_file=None, llm_provider="gemini")  # type: ignore[call-arg]
    assert bare.missing_required() == ["DATABASE_URL", "SUPABASE_URL", "GEMINI_API_KEY"]
    bare = Settings(_env_file=None, llm_provider="")  # type: ignore[call-arg]
    assert bare.missing_required() == ["DATABASE_URL", "SUPABASE_URL", "LLM_PROVIDER"]


def test_llm_defaults() -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.gemini_embed_model == "gemini-embedding-001"
    assert s.gemini_generation_model == "gemini-3.5-flash-lite"
    assert s.gemini_base_url == "https://generativelanguage.googleapis.com/v1beta"
    assert s.llm_timeout_seconds == 30.0


@pytest.mark.parametrize("name", ["Gemini ", " GEMINI"])
def test_provider_name_is_normalised(name: str) -> None:
    s = settings(llm_provider=name, gemini_api_key="KEY123")
    assert s.llm_provider == "gemini"
    assert s.missing_required() == []


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"embed_batch_size": 101}, "EMBED_BATCH_SIZE (must be 1-100)"),
        ({"embed_batch_size": 0}, "EMBED_BATCH_SIZE (must be 1-100)"),
        ({"chunk_max_chars": 8001}, "CHUNK_MAX_CHARS (must be at most 8000)"),
        ({"gemini_base_url": "http://x"}, "GEMINI_BASE_URL (must start with https://)"),
        ({"gemini_embed_model": "a/b"}, "GEMINI_EMBED_MODEL"),
        ({"gemini_embed_model": "a?x"}, "GEMINI_EMBED_MODEL"),
        ({"gemini_generation_model": "a/b"}, "GEMINI_GENERATION_MODEL"),
        ({"gemini_generation_model": "a?x"}, "GEMINI_GENERATION_MODEL"),
    ],
)
def test_gemini_setting_limits_reported_only_for_gemini(
    override: dict[str, Any], expected: str
) -> None:
    gem = settings(llm_provider="gemini", gemini_api_key="KEY123", **override)
    assert gem.missing_required() == [expected]
    assert settings(llm_provider="fake", **override).missing_required() == []


def test_gemini_setting_names_come_after_key_check_in_order() -> None:
    s = settings(
        llm_provider="gemini",
        embed_batch_size=0,
        chunk_max_chars=9000,
        gemini_base_url="http://x",
        gemini_embed_model="a/b",
        gemini_generation_model="a?x",
    )
    assert s.missing_required() == [
        "GEMINI_API_KEY",
        "EMBED_BATCH_SIZE (must be 1-100)",
        "CHUNK_MAX_CHARS (must be at most 8000)",
        "GEMINI_BASE_URL (must start with https://)",
        "GEMINI_EMBED_MODEL",
        "GEMINI_GENERATION_MODEL",
    ]


def test_config_limits_match_gemini_module() -> None:
    assert config.MAX_EMBED_BATCH == gemini_module.MAX_EMBED_BATCH
    assert config.MAX_EMBED_CHARS == gemini_module.MAX_EMBED_CHARS
