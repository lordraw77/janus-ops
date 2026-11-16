"""SNMP (FR-82) logic without a network or a database: parameters, value classification, grading
of a reading, the authentication built for each SNMP version and security level."""

import struct

import pytest
from pydantic import ValidationError
from pysnmp.proto import rfc1902, rfc1905

from janus_ops.monitoring import checks, snmp_poll
from janus_ops.monitoring.checks import Check, SnmpParams, evaluate_snmp
from janus_ops.monitoring.snmp_poll import Reading, SnmpError, Target


def _check(
    comparison: str | None = None,
    warn: float | None = None,
    crit: float | None = None,
    scale: float = 1.0,
) -> Check:
    return Check(
        id="c1",
        node_id="n1",
        node_name="nas",
        address="10.0.0.5",
        kind="snmp",
        params={"oid": "1.3.6.1.2.1.1.7.0", "scale": scale},
        comparison=comparison,
        warn=warn,
        crit=crit,
    )


# ---------------------------------------------------------------- parameters
@pytest.mark.parametrize("oid", ["1.3.6.1.2.1.1.7.0", "1.3.6.1.4.1.2021.10.1.3.1", "2.999.1"])
def test_valid_numeric_oids(oid: str) -> None:
    assert SnmpParams.model_validate({"oid": oid}).oid == oid


@pytest.mark.parametrize(
    "oid",
    [
        "",
        "SNMPv2-MIB::sysDescr.0",  # names need MIB files, which are not loaded
        "1.3.6.1.2.1.1.7.0 ",
        "1.3..6",
        "3.1.2",  # the first arc is 0, 1 or 2
        ".1.3.6",
        "1.3.6.1.2.1.1.7.0; drop",
    ],
)
def test_invalid_oids(oid: str) -> None:
    with pytest.raises(ValidationError):
        SnmpParams.model_validate({"oid": oid})


def test_scale_and_timeout_are_bounded() -> None:
    assert SnmpParams.model_validate({"oid": "1.3.6", "scale": 0.1}).scale == 0.1
    with pytest.raises(ValidationError):
        SnmpParams.model_validate({"oid": "1.3.6", "scale": 0})
    with pytest.raises(ValidationError):
        SnmpParams.model_validate({"oid": "1.3.6", "timeout_s": 60})


def test_snmp_is_a_thresholded_kind_and_needs_no_agent() -> None:
    assert "snmp" in checks.THRESHOLDS
    assert "snmp" not in checks.NEEDS_AGENT
    assert "snmp" in checks.PARAMS


# ---------------------------------------------------------------- classification
def test_integers_are_numeric() -> None:
    reading = snmp_poll.classify(rfc1902.Integer32(72))
    assert reading.number == 72.0
    assert reading.text == "72"
    assert reading.error is None


@pytest.mark.parametrize(
    "value", [rfc1902.Counter64(10**12), rfc1902.Gauge32(5), rfc1902.TimeTicks(9)]
)
def test_other_integer_types_are_numeric(value: object) -> None:
    assert snmp_poll.classify(value).number is not None


def test_octet_strings_are_text() -> None:
    reading = snmp_poll.classify(rfc1902.OctetString("nas-01"))
    assert reading.number is None
    assert reading.text == "nas-01"
    assert reading.error is None


def test_long_text_is_cut() -> None:
    reading = snmp_poll.classify(rfc1902.OctetString("x" * 500))
    assert len(reading.text) == snmp_poll.MAX_TEXT


@pytest.mark.parametrize("value", [rfc1905.NoSuchObject(), rfc1905.NoSuchInstance()])
def test_missing_objects_are_errors(value: object) -> None:
    reading = snmp_poll.classify(value)
    assert reading.number is None
    assert reading.error == "no_such_object"


# ---------------------------------------------------------------- grading
def test_numeric_reading_is_graded_against_the_limits() -> None:
    assert evaluate_snmp(_check("gt", crit=80), Reading("50", 50.0)).state == "ok"
    assert evaluate_snmp(_check("gt", crit=80), Reading("90", 90.0)).state == "crit"
    assert evaluate_snmp(_check("gt", warn=60, crit=80), Reading("70", 70.0)).state == "warn"
    assert evaluate_snmp(_check("lt", crit=10), Reading("5", 5.0)).state == "crit"


def test_scale_is_applied_before_the_thresholds() -> None:
    # a device reporting tenths of a degree: 655 -> 65.5, over a limit of 60
    result = evaluate_snmp(_check("gt", crit=60, scale=0.1), Reading("655", 655.0))
    assert result.state == "crit"
    assert "65.5" in result.output
    assert result.perf == {"value": pytest.approx(65.5)}


def test_reading_without_limits_is_ok_and_shows_the_value() -> None:
    result = evaluate_snmp(_check(), Reading("nas-01", None))
    assert result.state == "ok"
    assert "nas-01" in result.output


def test_text_compared_with_limits_is_unknown_not_guessed() -> None:
    result = evaluate_snmp(_check("gt", crit=1), Reading("degraded", None))
    assert result.state == "unknown"
    assert "not numeric" in result.output


@pytest.mark.parametrize(
    ("code", "state"),
    [
        ("no_snmp_capability", "unknown"),
        ("vault_unavailable", "unknown"),
        ("no_such_object", "unknown"),
        ("timeout", "crit"),
        ("authentication_failed", "crit"),
        ("pdu_error", "crit"),
        ("something_new", "crit"),
    ],
)
def test_every_error_code_has_a_state(code: str, state: str) -> None:
    result = evaluate_snmp(_check("gt", crit=1), Reading("", None, error=code))
    assert result.state == state
    assert result.output.startswith("1.3.6.1.2.1.1.7.0")


# ---------------------------------------------------------------- authentication
def test_v2c_uses_the_community_string() -> None:
    auth = snmp_poll._auth(Target(host="h", port=161, version="v2c", community="public"))
    assert str(auth.communityName) == "public"
    assert auth.communityIndex != "public"  # pysnmp generates the index itself


def test_v2c_without_community_is_refused() -> None:
    with pytest.raises(SnmpError) as exc:
        snmp_poll._auth(Target(host="h", port=161, version="v2c", community=None))
    assert exc.value.code == "no_community"


def _v3(level: str, **extra: object) -> Target:
    values: dict[str, object] = {
        "host": "h",
        "port": 161,
        "version": "v3",
        "user": "janus",
        "security_level": level,
        "auth_protocol": "SHA256",
        "priv_protocol": "AES128",
        "auth_secret": "authpass-1234",
        "priv_secret": "privpass-1234",
    }
    values.update(extra)
    return Target(**values)  # type: ignore[arg-type]


def test_authpriv_gets_both_keys() -> None:
    user = snmp_poll._auth(_v3("authPriv"))
    assert user.userName == "janus"
    assert user.authentication_key is not None and user.privacy_key is not None


def test_authnopriv_gets_only_the_authentication_key() -> None:
    user = snmp_poll._auth(_v3("authNoPriv"))
    assert user.authentication_key is not None
    assert user.privacy_key is None


def test_noauthnopriv_gets_no_keys() -> None:
    user = snmp_poll._auth(_v3("noAuthNoPriv"))
    assert user.authentication_key is None and user.privacy_key is None


def test_md5_and_des_are_not_offered() -> None:
    # the protocol names come from the capability config, which only allows SHA and SHA256
    assert set(snmp_poll.AUTH) == {"SHA", "SHA256"}
    assert set(snmp_poll.PRIV) == {"AES128", "AES256"}


@pytest.mark.parametrize(
    ("level", "extra", "code"),
    [
        ("authPriv", {"priv_secret": None}, "no_priv_passphrase"),
        ("authPriv", {"auth_secret": None}, "no_auth_passphrase"),
        ("authNoPriv", {"auth_secret": None}, "no_auth_passphrase"),
        ("bogus", {}, "bad_security_level"),
        ("authPriv", {"user": None}, "no_user"),
    ],
)
def test_incomplete_v3_settings_are_refused(
    level: str, extra: dict[str, object], code: str
) -> None:
    with pytest.raises(SnmpError) as exc:
        snmp_poll._auth(_v3(level, **extra))
    assert exc.value.code == code


@pytest.mark.parametrize(
    ("indication", "code"),
    [
        ("No SNMP response received before timeout", "timeout"),
        ("Wrong SNMP PDU digest", "authentication_failed"),
        ("unknownUserName", "authentication_failed"),
        ("unknownEngineID", "engine_unknown"),
        ("something else entirely", "request_failed"),
    ],
)
def test_indications_map_to_stable_codes(indication: str, code: str) -> None:
    assert snmp_poll._indication_code(indication) == code


def test_opaque_float_is_a_number_as_the_ups_reports_it() -> None:
    # 0x9f78 (float), length 4, 42c80000 = 100.0: the UPS battery charge on a DS920+
    reading = snmp_poll.classify(rfc1902.Opaque(bytes.fromhex("9f780442c80000")))
    assert reading.number == 100.0
    assert reading.text == "100"


def test_opaque_double_is_a_number() -> None:
    raw = b"\x9f\x79\x08" + struct.pack(">d", 2.5)
    assert snmp_poll.classify(rfc1902.Opaque(raw)).number == 2.5


def test_unknown_opaque_stays_text() -> None:
    reading = snmp_poll.classify(rfc1902.Opaque(b"\x9f\x78\x03abc"))
    assert reading.number is None
