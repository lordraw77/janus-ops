"""Master key in an external secret manager (FR-86): the Transit box, AppRole renewal, the cache,
failures, and the move between backends' readers. A fake OpenBao stands in; no network."""

import pytest

from janus_ops.config import Settings
from janus_ops.crypto import CompositeBox, DecryptionError, SecretBox
from janus_ops.transit import (
    SecretManagerUnavailable,
    TransitBox,
    TransitClient,
    TransitConfig,
    build_box,
    build_kv,
)
from janus_ops.vault import Vault, VaultUnavailable

from ._bao import ROLE_ID, SECRET_ID, FakeBao, fake_bao

LOCAL_KEY = "11" * 32


def _client(bao: FakeBao, **kw: object) -> TransitClient:
    cfg = TransitConfig(addr=bao.addr, role_id=ROLE_ID, secret_id=SECRET_ID, **kw)  # type: ignore[arg-type]
    return TransitClient(cfg)


def _settings(bao: FakeBao, **kw: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "database_url": "postgresql+asyncpg://u:p@localhost/db",
        "secret_backend": "openbao-transit",
        "bao_addr": bao.addr,
        "bao_role_id": ROLE_ID,
        "bao_secret_id": SECRET_ID,
    }
    values.update(kw)
    return Settings(**values)  # type: ignore[arg-type]


def test_a_blob_round_trips_and_is_bound_to_its_owner() -> None:
    with fake_bao() as bao:
        box = TransitBox(_client(bao))
        blob = box.encrypt(b"s3cret", b"credential:1:secret")
        assert blob[:1] == b"T" and b"s3cret" not in blob
        assert box.decrypt(blob, b"credential:1:secret") == b"s3cret"
        with pytest.raises(DecryptionError):  # copied to another row
            box.decrypt(blob, b"credential:2:secret")


def test_a_tampered_blob_is_refused() -> None:
    with fake_bao() as bao:
        box = TransitBox(_client(bao))
        blob = bytearray(box.encrypt(b"x" * 40, b"a"))
        blob[-1] ^= 1
        with pytest.raises(DecryptionError):
            box.decrypt(bytes(blob), b"a")
        for bad in (b"", b"T", b"T\x01\x00", b"\x01" + b"0" * 40):
            with pytest.raises(DecryptionError):
                box.decrypt(bad, b"a")


def test_unwrapped_data_keys_are_cached_for_a_while() -> None:
    with fake_bao() as bao:
        box = TransitBox(_client(bao), cache_s=300)
        blob = box.encrypt(b"x", b"a")
        for _ in range(5):
            assert box.decrypt(blob, b"a") == b"x"
        assert bao.count("decrypt") == 1
        nocache = TransitBox(_client(bao), cache_s=0)
        before = bao.count("decrypt")
        for _ in range(3):
            nocache.decrypt(blob, b"a")
        assert bao.count("decrypt") - before == 3


def test_one_login_serves_many_calls_and_a_revoked_token_is_replaced() -> None:
    with fake_bao() as bao:
        box = TransitBox(_client(bao), cache_s=0)
        blob = box.encrypt(b"x", b"a")
        box.decrypt(blob, b"a")
        assert bao.count("login") == 1
        bao.revoke_tokens()  # the secret manager dropped the token
        assert box.decrypt(blob, b"a") == b"x"
        assert bao.count("login") == 2


def test_the_token_is_renewed_before_it_expires() -> None:
    with fake_bao() as bao:
        bao.lease = 1  # renewed at 80% of a second
        client = _client(bao)
        client.wrap(b"x")
        import time

        time.sleep(1)
        client.wrap(b"x")
        assert bao.count("login") == 2


def test_the_secret_manager_being_down_is_a_vault_error() -> None:
    with fake_bao() as bao:
        box = TransitBox(_client(bao, timeout_s=1), cache_s=0)
        blob = box.encrypt(b"x", b"a")
        bao.sealed = True
        with pytest.raises(VaultUnavailable):
            box.encrypt(b"x", b"a")
        with pytest.raises(VaultUnavailable):
            box.decrypt(blob, b"a")
        assert box.check() is False
        bao.sealed = False
        assert box.check() is True
    with pytest.raises(SecretManagerUnavailable):  # nobody listens any more
        box.encrypt(b"x", b"a")


def test_a_wrong_role_is_refused_without_leaking_anything() -> None:
    with fake_bao() as bao:
        client = TransitClient(TransitConfig(addr=bao.addr, role_id="nope", secret_id="nope"))
        with pytest.raises(SecretManagerUnavailable) as exc:
            client.wrap(b"x")
        assert exc.value.code == "login_refused"


def test_plain_http_is_for_loopback_only() -> None:
    with pytest.raises(ValueError, match="https"):
        TransitClient(TransitConfig(addr="http://bao.example:8200", role_id="r", secret_id="s"))
    TransitClient(TransitConfig(addr="https://bao.example:8200", role_id="r", secret_id="s"))


def test_the_vault_works_on_transit_and_records_the_backend() -> None:
    with fake_bao() as bao:
        vault = Vault(build_box(_settings(bao)))
        sealed = vault.seal("cred-1", {"secret": "hunter2"})
        assert sealed.kek_version == 2
        assert sealed.dek_wrapped[:1] == b"T"
        again = vault.reveal("cred-1", sealed.ciphertext, sealed.nonce, sealed.dek_wrapped)
        assert again == {"secret": "hunter2"}
        with pytest.raises(DecryptionError):  # the row's data key moved to another credential
            vault.reveal("cred-2", sealed.ciphertext, sealed.nonce, sealed.dek_wrapped)


def test_old_local_secrets_stay_readable_while_the_local_key_is_set() -> None:
    with fake_bao() as bao:
        local = Vault(SecretBox(LOCAL_KEY))
        old = local.seal("c1", {"secret": "from-the-past"})
        assert old.kek_version == 1
        both = Vault(build_box(_settings(bao, secret_key=LOCAL_KEY)))
        assert both.reveal("c1", old.ciphertext, old.nonce, old.dek_wrapped) == {
            "secret": "from-the-past"
        }
        new = both.seal("c2", {"secret": "fresh"})
        assert new.dek_wrapped[:1] == b"T"
        # without the local key an old secret cannot be read, and it says so
        only_transit = Vault(build_box(_settings(bao)))
        with pytest.raises(DecryptionError):
            only_transit.reveal("c1", old.ciphertext, old.nonce, old.dek_wrapped)


def test_the_target_of_a_move_decides_where_new_wraps_go() -> None:
    with fake_bao() as bao:
        s = _settings(bao, secret_key=LOCAL_KEY)
        to_local = build_box(s, primary="local")
        assert isinstance(to_local, CompositeBox) and to_local.kek_version == 1
        to_transit = build_box(s, primary="openbao-transit")
        assert isinstance(to_transit, CompositeBox) and to_transit.kek_version == 2
        blob = to_local.encrypt(b"x", b"a")
        assert blob[:1] == bytes([1]) and to_transit.decrypt(blob, b"a") == b"x"


def test_settings_need_the_secret_manager_details() -> None:
    with pytest.raises(ValueError, match="JANUS_BAO_ROLE_ID"):
        Settings(
            _env_file=None,
            database_url="postgresql+asyncpg://u:p@localhost/db",
            secret_backend="openbao-transit",
            bao_addr="https://bao:8200",
        )
    s = Settings(_env_file=None, database_url="postgresql+asyncpg://u:p@localhost/db")
    assert s.secret_backend == "local" and build_box(s) is None


# ---------------------------------------------------------------- KV (ADR-011 option b)
def _kv_vault(bao: FakeBao, **kw: object) -> Vault:
    s = _settings(bao, secret_backend="openbao-kv", **kw)
    return Vault(build_box(s), build_kv(s))


def test_a_secret_in_kv_never_reaches_the_database_row() -> None:
    with fake_bao() as bao:
        vault = _kv_vault(bao)
        sealed = vault.seal("11111111-2222", {"secret": "hunter2", "passphrase": "p"})
        assert sealed.kek_version == 3 and sealed.dek_wrapped == b"" and sealed.nonce == b""
        assert sealed.ciphertext.startswith(b"kv1:") and b"hunter2" not in sealed.ciphertext
        assert bao.kv["janus-ops/11111111-2222"] == [{"secret": "hunter2", "passphrase": "p"}]
        got = vault.reveal("11111111-2222", sealed.ciphertext, sealed.nonce, sealed.dek_wrapped)
        assert got == {"secret": "hunter2", "passphrase": "p"}


def test_an_update_makes_a_new_version_and_the_reference_pins_it() -> None:
    with fake_bao() as bao:
        vault = _kv_vault(bao)
        first = vault.seal("abc", {"secret": "one"})
        second = vault.seal("abc", {"secret": "two"})
        assert len(bao.kv["janus-ops/abc"]) == 2
        assert vault.reveal("abc", first.ciphertext, b"", b"") == {"secret": "one"}
        assert vault.reveal("abc", second.ciphertext, b"", b"") == {"secret": "two"}


def test_a_reference_cannot_be_used_for_another_record() -> None:
    with fake_bao() as bao:
        vault = _kv_vault(bao)
        sealed = vault.seal("owner-1", {"secret": "mine"})
        vault.seal("owner-2", {"secret": "theirs"})
        with pytest.raises(DecryptionError, match="another record"):
            vault.reveal("owner-2", sealed.ciphertext, b"", b"")
        for bad in (b"kv1:", b"kv1:{}", b'kv1:{"m": "secret", "p": "x", "v": "z"}'):
            with pytest.raises(DecryptionError):
                vault.reveal("owner-1", bad, b"", b"")


def test_a_missing_secret_and_an_unreachable_manager_are_different_errors() -> None:
    with fake_bao() as bao:
        vault = _kv_vault(bao)
        sealed = vault.seal("gone", {"secret": "x"})
        bao.kv.clear()
        with pytest.raises(DecryptionError, match="not in the secret manager"):
            vault.reveal("gone", sealed.ciphertext, b"", b"")
        bao.sealed = True
        with pytest.raises(VaultUnavailable):
            vault.reveal("gone", sealed.ciphertext, b"", b"")
        with pytest.raises(VaultUnavailable):
            vault.seal("new", {"secret": "x"})


def test_a_server_without_kv_cannot_read_a_kv_secret() -> None:
    with fake_bao() as bao:
        sealed = _kv_vault(bao).seal("abc", {"secret": "x"})
        with pytest.raises(VaultUnavailable):
            Vault(SecretBox(LOCAL_KEY)).reveal("abc", sealed.ciphertext, b"", b"")


def test_discard_removes_the_content_and_entities_lists_ids() -> None:
    with fake_bao() as bao:
        s = _settings(bao, secret_backend="openbao-kv")
        kv = build_kv(s)
        assert kv is not None
        vault = Vault(build_box(s), kv)
        vault.seal("aaa", {"secret": "1"})
        vault.seal("bbb", {"secret": "2"})
        assert kv.entities() == {"aaa", "bbb"}
        vault.discard("aaa")
        assert kv.entities() == {"bbb"}
        vault.discard("never-existed")  # not an error


def test_kv_is_off_unless_the_backend_says_so() -> None:
    with fake_bao() as bao:
        assert build_kv(_settings(bao)) is None
        assert build_kv(_settings(bao), force=True) is not None
        kv = build_kv(_settings(bao), force=True)
        assert kv is not None
        for bad in ("../escape", "a/b", "", "x" * 65):
            with pytest.raises(DecryptionError, match="invalid id"):
                kv.write(bad, {"secret": "x"})  # an id cannot reach outside its own path
