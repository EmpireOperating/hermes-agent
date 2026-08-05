"""Trusted-transport dashboard auth contract tests."""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.dashboard_auth import clear_providers, register_provider
from hermes_cli.dashboard_auth.base import (
    DashboardAuthProvider,
    LoginStart,
    Session,
)


class TrustedRequestProvider(DashboardAuthProvider):
    """Minimal provider that proves request-identity auth uses normal sessions."""

    name = "trusted-request"
    display_name = "Trusted request (test only)"
    supports_session = False
    supports_request_identity = True

    def start_login(self, *, redirect_uri: str) -> LoginStart:
        raise NotImplementedError

    def complete_login(
        self, *, code: str, state: str, code_verifier: str, redirect_uri: str
    ) -> Session:
        raise NotImplementedError

    def verify_session(self, *, access_token: str) -> Session | None:
        return None

    def refresh_session(self, *, refresh_token: str) -> Session:
        raise NotImplementedError

    def revoke_session(self, *, refresh_token: str) -> None:
        return None

    def authenticate_request(self, *, request: Any) -> Session | None:
        if request.headers.get("x-test-trusted-user") != "alice@example.test":
            return None
        return Session(
            user_id="trusted-alice",
            email="alice@example.test",
            display_name="Alice",
            org_id="",
            provider=self.name,
            expires_at=4_102_444_800,
            access_token="",
            refresh_token="",
        )


@pytest.fixture
def trusted_request_client():
    clear_providers()
    register_provider(TrustedRequestProvider())
    previous = {
        "bound_host": getattr(web_server.app.state, "bound_host", None),
        "bound_port": getattr(web_server.app.state, "bound_port", None),
        "auth_required": getattr(web_server.app.state, "auth_required", None),
    }
    web_server.app.state.bound_host = "trusted-proxy.example.test"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True
    try:
        yield TestClient(web_server.app, base_url="https://trusted-proxy.example.test")
    finally:
        clear_providers()
        for key, value in previous.items():
            setattr(web_server.app.state, key, value)


def test_trusted_request_identity_unlocks_protected_api(trusted_request_client):
    response = trusted_request_client.get(
        "/api/auth/me",
        headers={"x-test-trusted-user": "alice@example.test"},
    )

    assert response.status_code == 200
    assert response.json()["user_id"] == "trusted-alice"
    assert response.json()["provider"] == "trusted-request"


def test_missing_request_identity_stays_unauthenticated(trusted_request_client):
    response = trusted_request_client.get("/api/auth/me")

    assert response.status_code == 401
