"""FRITZ!Box over TR-064: envelope, SOAP parsing, digest flow, grading, discovery (no box)."""

import httpx2
import pytest

from janus_ops.monitoring import fritz
from janus_ops.monitoring.checks import PARAMS, FritzParams

RESPONSE = (
    '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
    "<u:GetCommonLinkPropertiesResponse"
    ' xmlns:u="urn:dslforum-org:service:WANCommonInterfaceConfig:1">'
    "<NewWANAccessType>Ethernet</NewWANAccessType><NewPhysicalLinkStatus>Up</NewPhysicalLinkStatus>"
    "<NewLayer1DownstreamMaxBitRate>1000000000</NewLayer1DownstreamMaxBitRate>"
    "</u:GetCommonLinkPropertiesResponse></s:Body></s:Envelope>"
)
FAULT = (
    '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
    "<s:Fault><detail><UPnPError><errorCode>401</errorCode></UPnPError></detail></s:Fault>"
    "</s:Body></s:Envelope>"
)


def test_envelope_escapes_arguments() -> None:
    body = fritz.envelope("urn:x:Svc:1", "Set", {"NewValue": "a<b&c"})
    assert "<NewValue>a&lt;b&amp;c</NewValue>" in body
    assert 'xmlns:u="urn:x:Svc:1"' in body


def test_parse_reads_the_out_arguments() -> None:
    data = fritz.parse_response(RESPONSE, "GetCommonLinkProperties")
    assert data["NewPhysicalLinkStatus"] == "Up"
    assert data["NewWANAccessType"] == "Ethernet"


def test_a_soap_fault_is_a_stable_code() -> None:
    with pytest.raises(fritz.FritzError) as exc:
        fritz.parse_response(FAULT, "GetInfo")
    assert exc.value.code == "soap:401"


def test_poll_answers_the_digest_challenge() -> None:
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Digest"):
            return httpx2.Response(
                401, headers={"WWW-Authenticate": 'Digest realm="fritz", nonce="abc", qop="auth"'}
            )
        seen.append(request.url.path)
        action = request.headers["SOAPACTION"].split("#")[1].strip('"')
        values = {
            "GetInfo": "<NewModelName>FRITZ!Box 4690</NewModelName><NewUpTime>90000</NewUpTime>",
            "GetCommonLinkProperties": "<NewPhysicalLinkStatus>Up</NewPhysicalLinkStatus>",
            "GetTotalBytesReceived": "<NewTotalBytesReceived>2147483648</NewTotalBytesReceived>",
            "GetTotalBytesSent": "<NewTotalBytesSent>0</NewTotalBytesSent>",
            "GetStatusInfo": "<NewConnectionStatus>Connected</NewConnectionStatus>",
            "GetExternalIPAddress": "<NewExternalIPAddress>203.0.113.7</NewExternalIPAddress>",
        }[action]
        xml = (
            '<?xml version="1.0"?><s:Envelope xmlns:s="x"><s:Body>'
            f'<u:{action}Response xmlns:u="urn:x">{values}</u:{action}Response>'
            "</s:Body></s:Envelope>"
        )
        return httpx2.Response(200, text=xml)

    target = fritz.Target(host="192.168.0.1", port=49000, user="admin", password="pw")
    import asyncio

    data = asyncio.run(fritz.poll(target, transport=httpx2.MockTransport(handler)))
    assert data["NewConnectionStatus"] == "Connected"
    assert data["NewModelName"] == "FRITZ!Box 4690"
    assert data["NewExternalIPAddress"] == "203.0.113.7"
    assert seen and all(p.startswith("/upnp/control/") for p in seen)


def test_grading_by_item() -> None:
    up = {
        "NewPhysicalLinkStatus": "Up",
        "NewWANAccessType": "Ethernet",
        "NewLayer1DownstreamMaxBitRate": "1000000000",
        "NewLayer1UpstreamMaxBitRate": "50000000",
    }
    assert fritz.grade("wan_link", up).state == "ok"
    assert fritz.grade("wan_link", {**up, "NewPhysicalLinkStatus": "Down"}).state == "crit"
    assert fritz.grade("wan_link", {**up, "NewPhysicalLinkStatus": "Initializing"}).state == "warn"
    conn = {"NewConnectionStatus": "Connected", "NewExternalIPAddress": "203.0.113.7"}
    assert fritz.grade("wan_connection", conn).state == "ok"
    assert fritz.grade("wan_connection", {"NewConnectionStatus": "Connecting"}).state == "warn"
    assert fritz.grade("wan_connection", {"NewConnectionStatus": "Disconnected"}).state == "crit"
    assert "1000 Mbit/s" in fritz.grade("wan_link", up).output


def test_discovery_offers_the_items() -> None:
    assert fritz.discover({}) == ["wan_link", "wan_connection", "uptime", "traffic"]


def test_the_check_kind_is_registered() -> None:
    assert PARAMS["fritzbox"] is FritzParams
    assert FritzParams.model_validate({"item": "uptime"}).item == "uptime"
    with pytest.raises(ValueError):
        FritzParams.model_validate({"item": "anything"})
