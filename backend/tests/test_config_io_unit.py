"""Export/import helpers: the encrypted secrets block, id <-> name tokens, document checks."""

import pytest

from janus_ops import config_io
from janus_ops.config_io import ConfigError, Refs, Resolver

NODE = "11111111-2222-3333-4444-555555555555"
CHAN = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
OTHER = "99999999-9999-9999-9999-999999999999"


def test_secrets_round_trip_and_hide_the_content() -> None:
    block = config_io.encrypt_secrets(
        "a long passphrase", {"credentials": {"x": {"secret": "hunter2"}}}
    )
    assert "hunter2" not in str(block)
    assert (
        config_io.decrypt_secrets("a long passphrase", block)["credentials"]["x"]["secret"]
        == "hunter2"
    )


def test_wrong_passphrase_and_short_passphrase() -> None:
    block = config_io.encrypt_secrets("a long passphrase", {"a": 1})
    with pytest.raises(ConfigError) as wrong:
        config_io.decrypt_secrets("another passphrase", block)
    assert wrong.value.code == "wrong_passphrase"
    with pytest.raises(ConfigError) as short:
        config_io.encrypt_secrets("short", {})
    assert short.value.code == "passphrase_too_short"


def test_a_document_cannot_ask_for_an_unbounded_key_derivation() -> None:
    block = config_io.encrypt_secrets("a long passphrase", {})
    block["n"] = 2**30
    with pytest.raises(ConfigError) as exc:
        config_io.decrypt_secrets("a long passphrase", block)
    assert exc.value.code == "unsupported_secrets"
    with pytest.raises(ConfigError):
        config_io.decrypt_secrets("a long passphrase", {"kdf": "md5"})


def test_two_exports_of_the_same_secrets_differ() -> None:
    a = config_io.encrypt_secrets("a long passphrase", {"a": 1})
    b = config_io.encrypt_secrets("a long passphrase", {"a": 1})
    assert a["salt"] != b["salt"] and a["ciphertext"] != b["ciphertext"]


def test_ids_become_names_and_back_and_unknown_ones_stay() -> None:
    refs = Refs(node={NODE: "web01"}, channel={CHAN: "Mail"})
    steps = {"steps": [{"params": {"node_id": NODE, "channel_id": CHAN, "keep": OTHER, "n": 3}}]}
    tokens = refs.tokenize(steps)
    assert tokens["steps"][0]["params"] == {
        "node_id": "@node:web01",
        "channel_id": "@channel:Mail",
        "keep": OTHER,  # an id nobody knows is left as it is
        "n": 3,
    }
    resolver = Resolver()
    resolver.maps = {"node": {"web01": "NEW-NODE-ID"}, "channel": {"mail": "NEW-CHAN-ID"}}
    back = resolver.resolve(tokens)
    assert back["steps"][0]["params"]["node_id"] == "NEW-NODE-ID"
    assert back["steps"][0]["params"]["channel_id"] == "NEW-CHAN-ID"  # case-insensitive names


def test_a_missing_name_resolves_to_none_and_is_reported() -> None:
    resolver = Resolver()
    resolver.maps = {"node": {}}
    assert resolver.resolve({"node_id": "@node:gone"}) == {"node_id": None}
    assert resolver.missing == {"node gone"}


def test_check_names_may_hold_a_slash_in_the_check_part() -> None:
    resolver = Resolver()
    resolver.maps = {"check": {"web01/disk /var": "CID"}}
    assert resolver.resolve("@check:web01/disk /var") == "CID"


def test_document_checks() -> None:
    ok = {"format": "janus-ops-config", "version": 1, "sections": {}}
    assert config_io.check_document(ok) is ok
    for bad in (None, {}, {"format": "x"}, {**ok, "version": 99}, {**ok, "sections": []}):
        with pytest.raises(ConfigError):
            config_io.check_document(bad)


def test_build_document_needs_a_passphrase_only_with_secrets() -> None:
    plain = config_io.Exported({"environments": []}, {}, [])
    assert "secrets" not in config_io.build_document(plain, None)
    with_secrets = config_io.Exported({}, {"credentials": {"x": {"secret": "s"}}}, [])
    with pytest.raises(ConfigError) as exc:
        config_io.build_document(with_secrets, None)
    assert exc.value.code == "passphrase_required"
    assert "secrets" in config_io.build_document(with_secrets, "a long passphrase")
