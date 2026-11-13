from fastapi.testclient import TestClient

from janus_ops import __version__


def test_health_is_ok_without_database(client: TestClient) -> None:
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "version": __version__}


def test_ready_reports_unreachable_database(client: TestClient) -> None:
    r = client.get("/api/v1/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "unavailable"
    assert body["checks"] == {"database": "unreachable"}


def test_ready_response_leaks_no_connection_details(client: TestClient) -> None:
    text = client.get("/api/v1/ready").text
    for secret in ("secret-pw", "127.0.0.1", "asyncpg"):
        assert secret not in text


def test_unknown_api_path_is_json_404(client: TestClient) -> None:
    for method in ("get", "post", "delete"):
        r = getattr(client, method)("/api/v1/does-not-exist")
        assert r.status_code == 404
        assert r.json() == {"detail": "Not Found"}


def test_security_headers_present(client: TestClient) -> None:
    r = client.get("/api/v1/health")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"


def test_openapi_published_under_api(client: TestClient) -> None:
    r = client.get("/api/openapi.json")
    assert r.status_code == 200
    assert "/api/v1/health" in r.json()["paths"]


def test_no_ui_routes_when_static_dir_unset(client: TestClient) -> None:
    assert client.get("/").status_code == 404


def test_ui_index_served(ui_client: TestClient) -> None:
    r = ui_client.get("/")
    assert r.status_code == 200
    assert "janus-ui-index" in r.text


def test_ui_trailing_slash_page_served(ui_client: TestClient) -> None:
    r = ui_client.get("/inventory/")
    assert r.status_code == 200
    assert "janus-ui-inventory" in r.text


def test_ui_unknown_page_uses_export_404(ui_client: TestClient) -> None:
    r = ui_client.get("/nope/")
    assert r.status_code == 404
    assert "janus-ui-404" in r.text


def test_api_still_json_when_ui_mounted(ui_client: TestClient) -> None:
    r = ui_client.get("/api/v1/nope")
    assert r.status_code == 404
    assert r.json() == {"detail": "Not Found"}
    assert ui_client.get("/api/v1/health").status_code == 200
