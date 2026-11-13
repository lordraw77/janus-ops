"""Shared helpers for integration tests (TimescaleDB + lldap: scripts/integration-test.sh)."""

import asyncio
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx2 as httpx
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from janus_ops.config import Settings
from janus_ops.main import create_app

from .conftest import make_settings

DB_URL = os.environ.get("JANUS_TEST_DATABASE_URL", "")
LDAP_URL = os.environ.get("JANUS_TEST_LDAP_URL", "")
LLDAP_HTTP = os.environ.get("JANUS_TEST_LLDAP_HTTP", "")
LLDAP_ADMIN_PASSWORD = os.environ.get("JANUS_TEST_LLDAP_ADMIN_PASSWORD", "")
CONFIGURED = bool(DB_URL and LDAP_URL and LLDAP_HTTP)
BASE_DN = "dc=janus-ops,dc=local"
SECRET_KEY = "ab" * 32

PASSWORDS = {
    "alice": "alice-password-123",  # admin
    "bob": "bob-password-1234",  # operator
    "carol": "carol-password-12",  # auditor
    "dave": "dave-password-123",  # no group
}


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": DB_URL,
        "ldap_url": LDAP_URL,
        "ldap_base_dn": BASE_DN,
        "ldap_bind_dn": f"uid=janus-svc,ou=people,{BASE_DN}",
        "ldap_bind_password": "svc-password-1234",
        "ldap_timeout_s": 2,
        "lldap_http_url": LLDAP_HTTP,
        "ldap_manager_password": "manager-password-1234",
        "secret_key": SECRET_KEY,
    }
    values.update(overrides)
    return make_settings(**values)


def run_sql(stmt: str, params: dict[str, Any] | None = None) -> list[Any]:
    async def _go() -> list[Any]:
        engine = create_async_engine(DB_URL)
        try:
            async with engine.begin() as conn:
                result = await conn.execute(text(stmt), params or {})
                return list(result.fetchall()) if result.returns_rows else []
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def make_client(**overrides: Any) -> TestClient:
    return TestClient(create_app(settings(**overrides)), base_url="https://testserver")


def login(c: TestClient, user: str, password: str | None = None) -> httpx.Response:
    return c.post(
        "/api/v1/auth/login",
        json={"username": user, "password": password or PASSWORDS[user]},
    )


@contextmanager
def signed_in(user: str, password: str | None = None, **overrides: Any) -> Iterator[TestClient]:
    with make_client(**overrides) as c:
        r = login(c, user, password)
        assert r.status_code == 200, r.text
        assert r.json() == {"mfa_required": False}
        yield c
