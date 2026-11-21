"""Provider normalisation, error classification and the failover walk (no network, no database)."""

import asyncio
from typing import Any

import httpx2
import pytest

from janus_ops.ai import llm_router, providers
from janus_ops.ai.llm_http import LLMError, Outcome, classify_http, parse_duration
from janus_ops.ai.llm_router import Entry, LLMRouter, NoProvider, RouteExhausted
from janus_ops.ai.providers import Provider, ProviderError


def _provider(slug: str, **over: Any) -> Provider:
    base: dict[str, Any] = {
        "id": f"id-{slug}", "slug": slug, "type": "custom", "base_url": f"https://{slug}.test/v1",
        "headers": {}, "extra_body": {}, "config": {}, "connect": 5.0, "first_token": 30.0,
        "total": 30.0, "verify_tls": True, "enabled": True, "budget_daily": None,
        "budget_monthly": None,
    }  # fmt: skip
    return Provider(**{**base, **over})


# ------------------------------------------------------------------------------ classification
def test_http_errors_are_classified_like_the_gateway_does() -> None:
    def outcome(status: int, body: str = "{}") -> Outcome:
        return classify_http(status, body, {}).outcome

    assert outcome(402) is Outcome.QUOTA
    assert (
        outcome(429, '{"error": {"message": "You exceeded your current quota"}}') is Outcome.QUOTA
    )
    assert outcome(429, '{"error": {"message": "slow down"}}') is Outcome.RATE_LIMIT
    assert outcome(401) is Outcome.AUTH and outcome(403) is Outcome.AUTH
    assert outcome(404, '{"error": {"message": "model not found"}}') is Outcome.MODEL_MISSING
    assert outcome(404, "page not found") is Outcome.UNAVAILABLE
    assert outcome(503) is Outcome.UNAVAILABLE and outcome(408) is Outcome.TIMEOUT
    assert (
        outcome(400, '{"error": {"message": "maximum context length exceeded"}}') is Outcome.CONTEXT
    )
    assert outcome(400, '{"error": {"message": "bad tools"}}') is Outcome.FATAL


def test_retry_after_accepts_seconds_and_durations() -> None:
    assert parse_duration("12") == 12.0
    assert parse_duration("1m30s") == 90.0
    assert classify_http(429, "{}", {"retry-after": "3"}).retry_after == 3.0


# ------------------------------------------------------------------------------ providers
def test_provider_input_is_validated() -> None:
    ok = providers.normalise({"type": "groq", "slug": "groq"}, None)
    assert ok["base_url"] == "https://api.groq.com/openai/v1" and ok["verify_tls"] == "true"
    cf = providers.normalise(
        {"type": "cloudflare", "slug": "cf", "config": {"account_id": "abc123"}}, None
    )
    assert "/accounts/abc123/ai/v1" in cf["base_url"]
    for bad in (
        {"type": "nope", "slug": "x"},
        {"type": "groq", "slug": "Bad Slug"},
        {"type": "cloudflare", "slug": "cf"},  # needs the account id
        {"type": "custom", "slug": "c", "base_url": "ftp://x"},
        {"type": "groq", "slug": "g", "headers": {"Authorization": "x"}},
        {"type": "groq", "slug": "g", "connect_timeout": 9999},
        {"type": "groq", "slug": "g", "budget_tokens_daily": 0},
    ):
        with pytest.raises(ProviderError):
            providers.normalise(bad, None)


def test_model_lists_of_each_provider_family_are_read() -> None:
    openai = {"data": [{"id": "a", "context_length": 8000}, {"id": "b"}, {"id": "bad id"}]}
    assert providers.parse_models(openai) == [
        {"model_id": "a", "context_window": 8000},
        {"model_id": "b", "context_window": None},
    ]
    assert [
        m["model_id"] for m in providers.parse_models({"models": [{"name": "llama3"}]}, "ollama")
    ] == ["llama3"]
    cloudflare = {"result": [{"id": "uuid", "name": "@cf/meta/llama"}]}
    assert providers.parse_models(cloudflare, "cloudflare")[0]["model_id"] == "@cf/meta/llama"
    with pytest.raises(ValueError):
        providers.parse_models({"nothing": 1})


def test_urls_follow_the_provider_type() -> None:
    assert _provider("o", type="ollama", base_url="http://h:11434").chat_url == (
        "http://h:11434/v1/chat/completions"
    )
    assert _provider("o", type="ollama", base_url="http://h:11434").models_url == (
        "http://h:11434/api/tags"
    )
    assert _provider("g", type="groq").models_url.endswith("/models")


# ------------------------------------------------------------------------------ the walk
class FakeRouter(LLMRouter):
    """The failover walk with an in-memory health and usage instead of the database."""

    def __init__(
        self, replies: dict[str, list[Any]], keys: dict[str, list[str]] | None = None
    ) -> None:
        self.replies = replies
        self.keys = keys or {}
        self.health: dict[tuple[str, str], str] = {}
        self.used: list[tuple[str, str]] = []
        self.unavailable: list[str] = []
        self.slept: list[float] = []
        self._transport = None
        self._sleep = self._fake_sleep  # type: ignore[method-assign]

    async def _fake_sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    async def _plan(self, entries: list[Entry]) -> Any:
        plan, skipped = [], []
        for e in entries:
            if not e.provider.enabled:
                skipped.append((e.provider.slug, "provider disabled"))
                continue
            if self.health.get(("entry", e.ref)):
                skipped.append((e.provider.slug, self.health[("entry", e.ref)]))
                continue
            keys = [
                k
                for k in self.keys.get(e.provider.slug, [None])
                if (  # type: ignore[list-item]
                    self.health.get(("key", str(k))) is None
                )
            ]
            if not keys:
                skipped.append((e.provider.slug, "all keys unavailable"))
                continue
            plan.append((e, llm_router._Status(keys)))
        return plan, skipped

    async def _record(self, walk: Any, entry: Entry, outcome: str, *a: Any, **k: Any) -> None:
        self.used.append((entry.label, outcome))

    async def _health_set(
        self, scope: str, ref: str, state: str, seconds: Any, reason: str, detail: str
    ) -> None:
        self.health[(scope, ref)] = reason

    async def _failures(self, scope: str, ref: str) -> int:
        return 0

    async def _mark_success(self, entry: Entry, key_id: Any) -> None:
        self.health.pop(("entry", entry.ref), None)

    async def _model_unavailable(self, entry: Entry) -> None:
        self.unavailable.append(entry.label)

    async def key_value_for(self, key_id: str | None) -> str | None:
        return key_id


def _walk(router: FakeRouter, entries: list[Entry], monkeypatch: pytest.MonkeyPatch) -> Any:
    async def fake_post(endpoint: Any, messages: Any, tools: Any = None, **kw: Any) -> Any:
        result = router.replies[endpoint.chat_url.split("//")[1].split(".")[0]].pop(0)
        if isinstance(result, LLMError):
            raise result
        return result

    class _Providers:
        async def key_value(self, key_id: str) -> str:
            return key_id

    router._providers = _Providers()  # type: ignore[assignment]
    monkeypatch.setattr(llm_router, "post_chat", fake_post)
    return asyncio.run(router.complete(entries, [{"role": "user", "content": "x"}]))


def _entry(slug: str, model: str = "m", **provider: Any) -> Entry:
    return Entry(_provider(slug, **provider), model)


def _reply(text: str) -> Any:
    from janus_ops.ai.llm_http import Reply

    return Reply({"content": text}, 1, 1)


def test_the_first_usable_entry_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    router = FakeRouter({"a": [_reply("one")], "b": [_reply("two")]})
    done = _walk(router, [_entry("a"), _entry("b")], monkeypatch)
    assert done.reply.message["content"] == "one" and done.notices == []


def test_an_outage_puts_the_entry_on_hold_and_the_next_one_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = FakeRouter(
        {"a": [LLMError("HTTP 503", Outcome.UNAVAILABLE)], "b": [_reply("fallback")]}
    )
    done = _walk(router, [_entry("a"), _entry("b")], monkeypatch)
    assert done.reply.message["content"] == "fallback"
    assert done.notices and "Switched to b/m" in done.notices[0]
    assert router.health[("entry", "id-a|m")] == "unavailable"


def test_a_bad_key_is_switched_off_and_the_next_key_is_tried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = FakeRouter(
        {"a": [LLMError("HTTP 401", Outcome.AUTH), _reply("second key")]},
        keys={"a": ["k1", "k2"]},
    )
    done = _walk(router, [_entry("a")], monkeypatch)
    assert done.reply.message["content"] == "second key"
    assert router.health[("key", "k1")] == "invalid key"
    assert ("entry", "id-a|m") not in router.health  # the entry itself is fine


def test_a_short_rate_limit_waits_and_retries_the_same_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = FakeRouter(
        {"a": [LLMError("HTTP 429", Outcome.RATE_LIMIT, retry_after=2.0), _reply("later")]}
    )
    done = _walk(router, [_entry("a")], monkeypatch)
    assert done.reply.message["content"] == "later" and router.slept == [2.0]


def test_a_missing_model_is_marked_and_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    router = FakeRouter({"a": [LLMError("HTTP 404", Outcome.MODEL_MISSING)], "b": [_reply("ok")]})
    done = _walk(router, [_entry("a"), _entry("b")], monkeypatch)
    assert done.entry.label == "b/m" and router.unavailable == ["a/m"]


def test_every_entry_failing_is_reported_with_the_reasons(monkeypatch: pytest.MonkeyPatch) -> None:
    router = FakeRouter(
        {
            "a": [LLMError("HTTP 402", Outcome.QUOTA)],
            "b": [LLMError("timed out", Outcome.TIMEOUT)],
        }
    )
    with pytest.raises(RouteExhausted) as info:
        _walk(router, [_entry("a"), _entry("b")], monkeypatch)
    assert dict(info.value.failures) == {"a": "quota exhausted", "b": "timeout"}


def test_a_request_every_entry_rejects_the_same_way_is_raised_as_it_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = FakeRouter(
        {
            "a": [LLMError("HTTP 400: bad tools", Outcome.FATAL)],
            "b": [LLMError("HTTP 400: bad tools", Outcome.FATAL)],
        }
    )
    with pytest.raises(LLMError) as info:
        _walk(router, [_entry("a"), _entry("b")], monkeypatch)
    assert info.value.outcome is Outcome.FATAL
    assert not router.health  # a rejection is not a reason to punish an entry


def test_a_context_too_long_goes_to_the_next_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    router = FakeRouter({"a": [LLMError("HTTP 400", Outcome.CONTEXT)], "b": [_reply("fits")]})
    big = Entry(_provider("b"), "m", context_window=200000)
    small = Entry(_provider("a"), "m", context_window=8000)
    assert _walk(router, [small, big], monkeypatch).reply.message["content"] == "fits"


def test_nothing_to_walk_is_not_configured() -> None:
    with pytest.raises(NoProvider):
        asyncio.run(FakeRouter({}).complete([], []))


def test_the_vault_failing_is_not_the_keys_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    from janus_ops.vault import VaultUnavailable

    router = FakeRouter({"a": [_reply("x")]}, keys={"a": ["k1"]})

    async def boom(endpoint: Any, *a: Any, **k: Any) -> Any:
        raise VaultUnavailable

    class _Providers:
        async def key_value(self, key_id: str) -> str:
            raise VaultUnavailable

    router._providers = _Providers()  # type: ignore[assignment]
    monkeypatch.setattr(llm_router, "post_chat", boom)
    with pytest.raises(llm_router.RouteError) as info:
        asyncio.run(router.complete([_entry("a")], []))
    assert info.value.code == "vault_unavailable" and not router.health


def test_a_real_http_error_body_becomes_a_classified_error() -> None:
    from janus_ops.ai.llm_http import Endpoint, post_chat

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429, json={"error": {"message": "quota exceeded"}})

    with pytest.raises(LLMError) as info:
        asyncio.run(
            post_chat(
                Endpoint("https://x.test/v1/chat/completions", "m"),
                [],
                transport=httpx2.MockTransport(handler),
            )
        )
    assert info.value.outcome is Outcome.QUOTA


def test_keys_are_read_from_a_file_and_never_echoed() -> None:
    from janus_ops.ai import providers_import

    env = providers_import.read_env_file(
        [
            "# comment",
            "smtp:",
            "GROQ_API_KEY=gsk_abc",
            "export NVIDIA_API_KEY='nv-1'",
            "EMPTY=",
            "x y",
        ]
    )
    assert env == {"GROQ_API_KEY": "gsk_abc", "NVIDIA_API_KEY": "nv-1"}
    sources = {s.type for s in providers_import.SOURCES}
    assert {"groq", "openrouter", "gemini", "cloudflare", "cerebras", "mistral", "nvidia",
            "huggingface", "ollama"} <= sources  # fmt: skip
    assert providers.PRESETS["gemini"].paid  # billed providers are created switched off


def test_a_billed_provider_previews_as_disabled() -> None:
    service = providers.ProviderService(None, None, None)  # type: ignore[arg-type]
    data = {"type": "gemini", "slug": "gemini", "enabled": False}
    preview = asyncio.run(service.preview(data))
    assert preview.enabled is False and preview.slug == "gemini"


def test_recording_files_are_removed_and_searched(tmp_path: Any) -> None:
    import json

    from janus_ops import retention
    from janus_ops.archive import _content_matches

    sid = "11111111-1111-1111-1111-111111111111"
    cast = tmp_path / f"{sid}.cast"
    header = {"version": 2, "width": 80, "height": 24}
    cast.write_text(
        json.dumps(header) + "\n"
        + json.dumps([0.1, "o", "\x1b[32mdeploy fin"]) + "\n"
        + json.dumps([0.2, "o", "ished OK\r\n"]) + "\n"
        + json.dumps([0.3, "i", "secret-typed"]) + "\n"
    )  # fmt: skip
    assert _content_matches(cast, "Deploy Finished")  # across two chunks, case-insensitive
    assert not _content_matches(cast, "secret-typed")  # keystrokes are never recorded
    assert (
        retention.delete_recording_files(tmp_path, [sid, "22222222-2222-2222-2222-222222222222"])
        == 1
    )
    assert not cast.exists()
    assert retention.delete_recording_files(None, [sid]) == 0
