"""Contract tests for the generic non-interactive (bearer-token) auth seam.

Covers Task 2.0a: the reusable token-auth capability in the dashboard auth
framework — NOT the drain plugin (that's 2.0b/2.1). Asserts the ABC capability
flag, the registry filter, bearer extraction, provider stacking (verify_token),
and the route-agnostic middleware seam's fail-closed / 503 / pass-through
behaviour.
"""
from __future__ import annotations

import asyncio
from typing import Optional

import pytest

from hermes_cli.dashboard_auth import (
    DashboardAuthProvider,
    LoginStart,
    Session,
    TokenPrincipal,
    clear_providers,
    list_providers,
    list_session_providers,
    list_token_providers,
    register_provider,
)
from hermes_cli.dashboard_auth.base import ProviderError
from hermes_cli.dashboard_auth import token_auth


# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------


class _OAuthOnly(DashboardAuthProvider):
    """A pure interactive provider — never token-authable."""

    name = "oauth-only"
    display_name = "OAuth Only"

    def start_login(self, *, redirect_uri):
        return LoginStart(redirect_url="x", cookie_payload={})

    def complete_login(self, *, code, state, code_verifier, redirect_uri):
        return Session("u", "e", "n", "o", self.name, 0, "a", "r")

    def verify_session(self, *, access_token):
        return None

    def refresh_session(self, *, refresh_token):
        return Session("u", "e", "n", "o", self.name, 0, "a", "r")

    def revoke_session(self, *, refresh_token):
        return None


class _TokenProvider(_OAuthOnly):
    """A token provider that accepts exactly one secret."""

    name = "tok"
    display_name = "Token Provider"
    supports_token = True

    def __init__(self, *, secret: str = "good-secret", scopes=("drain",)):
        self._secret = secret
        self._scopes = tuple(scopes)

    def verify_token(self, *, token: str) -> Optional[TokenPrincipal]:
        if token == self._secret:
            return TokenPrincipal(
                principal=self.name, provider=self.name, scopes=self._scopes
            )
        return None


class _UnreachableTokenProvider(_OAuthOnly):
    name = "tok-down"
    display_name = "Unreachable Token Provider"
    supports_token = True

    def verify_token(self, *, token: str) -> Optional[TokenPrincipal]:
        raise ProviderError("backing store down")


class _BuggyTokenProvider(_OAuthOnly):
    name = "tok-buggy"
    display_name = "Buggy Token Provider"
    supports_token = True

    def verify_token(self, *, token: str) -> Optional[TokenPrincipal]:
        raise RuntimeError("kaboom")


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_state():
    clear_providers()
    token_auth.clear_token_routes()
    yield
    clear_providers()
    token_auth.clear_token_routes()


class _FakeURL:
    def __init__(self, path):
        self.path = path


class _FakeClient:
    host = "1.2.3.4"


class _FakeRequest:
    """Minimal Request stand-in for the seam (no real Starlette needed)."""

    def __init__(self, path="/api/gateway/drain", headers=None):
        self.url = _FakeURL(path)
        self.headers = headers or {}
        self.client = _FakeClient()

        class _State:
            pass

        self.state = _State()


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# ABC + registry
# --------------------------------------------------------------------------


def test_oauth_provider_defaults_supports_token_false():
    assert _OAuthOnly().supports_token is False




class _NonInteractiveProvider(_TokenProvider):
    """A token-only credential with no interactive session."""

    name = "svc-cred"
    display_name = "Service Credential"
    supports_session = False


# --------------------------------------------------------------------------
# Bearer extraction
# --------------------------------------------------------------------------




# --------------------------------------------------------------------------
# authenticate_token (provider stacking)
# --------------------------------------------------------------------------


def test_authenticate_token_accepts_valid():
    register_provider(_TokenProvider(secret="good-secret"))
    req = _FakeRequest(headers={"authorization": "Bearer good-secret"})
    principal, unreachable = token_auth.authenticate_token(req)
    assert unreachable is None
    assert principal is not None
    assert principal.provider == "tok"
    assert principal.scopes == ("drain",)


def test_authenticate_token_rejects_wrong_secret():
    register_provider(_TokenProvider(secret="good-secret"))
    req = _FakeRequest(headers={"authorization": "Bearer wrong"})
    principal, unreachable = token_auth.authenticate_token(req)
    assert principal is None
    assert unreachable is None


def test_authenticate_token_stacks_first_match_wins():
    register_provider(_TokenProvider(secret="aaa"))
    second = _TokenProvider(secret="bbb")
    second.name = "tok2"
    register_provider(second)
    req = _FakeRequest(headers={"authorization": "Bearer bbb"})
    principal, _ = token_auth.authenticate_token(req)
    assert principal is not None and principal.provider == "tok2"


def test_authenticate_token_unreachable_then_valid_provider_wins():
    register_provider(_UnreachableTokenProvider())
    register_provider(_TokenProvider(secret="good"))
    req = _FakeRequest(headers={"authorization": "Bearer good"})
    principal, unreachable = token_auth.authenticate_token(req)
    # A later provider accepting the token beats the earlier outage.
    assert principal is not None and principal.provider == "tok"
    assert unreachable is None


def test_authenticate_token_buggy_provider_does_not_crash():
    register_provider(_BuggyTokenProvider())
    register_provider(_TokenProvider(secret="good"))
    req = _FakeRequest(headers={"authorization": "Bearer good"})
    principal, unreachable = token_auth.authenticate_token(req)
    assert principal is not None and principal.provider == "tok"


# --------------------------------------------------------------------------
# Middleware seam (route-agnostic)
# --------------------------------------------------------------------------


async def _call_next_ok(request):
    from fastapi.responses import JSONResponse

    return JSONResponse({"ok": True}, status_code=200)






def test_legacy_unowned_registration_is_source_compatible_but_inert(caplog):
    registration = token_auth.register_token_route("/api/legacy-machine")

    assert registration is None
    assert not token_auth.is_token_route("/api/legacy-machine")
    assert "ignored unowned token route" in caplog.text


def test_seam_rejects_wrong_token_401():
    register_provider(_TokenProvider(secret="good"))
    token_auth.register_token_route("/api/gateway/drain", provider="tok")
    req = _FakeRequest(
        path="/api/gateway/drain", headers={"authorization": "Bearer bad"}
    )
    resp = _run(token_auth.token_auth_middleware(req, _call_next_ok))
    assert resp.status_code == 401


def test_route_provider_binding_rejects_a_token_from_another_provider():
    other = _TokenProvider(secret="shared", scopes=("empire.poll",))
    other.name = "empire-client"
    register_provider(other)
    register_provider(_TokenProvider(secret="drain-secret", scopes=("drain",)))
    token_auth.register_token_route(
        "/api/gateway/drain", provider="tok", required_scopes=("drain",)
    )
    req = _FakeRequest(
        path="/api/gateway/drain", headers={"authorization": "Bearer shared"}
    )

    resp = _run(token_auth.token_auth_middleware(req, _call_next_ok))

    assert resp.status_code == 401
    assert not hasattr(req.state, "token_authenticated")


def test_route_scope_binding_rejects_an_authenticated_under_scoped_principal():
    register_provider(_TokenProvider(secret="good", scopes=("poll",)))
    token_auth.register_token_route(
        "/api/client/decision", provider="tok", required_scopes=("decision",)
    )
    req = _FakeRequest(
        path="/api/client/decision", headers={"authorization": "Bearer good"}
    )

    resp = _run(token_auth.token_auth_middleware(req, _call_next_ok))

    assert resp.status_code == 403
    assert not hasattr(req.state, "token_authenticated")


def test_route_provider_and_scope_binding_accepts_only_the_exact_principal():
    register_provider(_TokenProvider(secret="good", scopes=("decision", "body:abc")))
    token_auth.register_token_route(
        "/api/client/decision",
        provider="tok",
        required_scopes=("decision", "body:abc"),
    )
    req = _FakeRequest(
        path="/api/client/decision", headers={"authorization": "Bearer good"}
    )

    resp = _run(token_auth.token_auth_middleware(req, _call_next_ok))

    assert resp.status_code == 200
    assert req.state.token_authenticated is True
    assert req.state.token_principal.provider == "tok"


def test_conflicting_token_route_registration_poisoning_is_fail_closed_and_disposable():
    first_provider = _TokenProvider(secret="first-token")
    first_provider.name = "first"
    second_provider = _TokenProvider(secret="second-token")
    second_provider.name = "second"
    register_provider(first_provider)
    register_provider(second_provider)
    first = token_auth.register_token_route("/api/shared", provider="first")
    second = token_auth.register_token_route("/api/shared", provider="second")
    request = _FakeRequest(
        path="/api/shared", headers={"authorization": "Bearer first-token"}
    )

    response = _run(token_auth.token_auth_middleware(request, _call_next_ok))

    assert response.status_code == 503
    assert not hasattr(request.state, "token_authenticated")

    second.dispose()
    request = _FakeRequest(
        path="/api/shared", headers={"authorization": "Bearer first-token"}
    )
    response = _run(token_auth.token_auth_middleware(request, _call_next_ok))
    assert response.status_code == 200
    assert request.state.token_authenticated is True

    first.dispose()
    assert not token_auth.is_token_route("/api/shared")


def test_scope_order_is_canonical_and_profile_registrations_are_isolated():
    first = token_auth.register_token_route(
        "/api/scoped",
        provider="tok",
        required_scopes=("read", "write"),
        scope="profile-a",
    )
    second = token_auth.register_token_route(
        "/api/scoped",
        provider="tok",
        required_scopes=("write", "read"),
        scope="profile-a",
    )
    other = token_auth.register_token_route(
        "/api/scoped", provider="other", scope="profile-b"
    )

    assert token_auth.is_token_route("/api/scoped", scope="profile-a")
    policy, conflict = token_auth._token_route_policy("/api/scoped", scope="profile-a")
    assert conflict is False
    assert policy is not None
    assert policy.required_scopes == frozenset({"read", "write"})
    assert token_auth._token_route_policy("/api/scoped", scope="profile-b")[0].provider == "other"

    first.dispose()
    second.dispose()
    other.dispose()


def test_provider_cannot_spoof_another_provider_in_its_principal():
    provider = _TokenProvider(secret="good")
    provider.name = "owner"

    def spoofed(*, token):
        assert token == "good"
        return TokenPrincipal(provider="other", principal="machine")

    provider.verify_token = spoofed
    register_provider(provider)
    token_auth.register_token_route("/api/owned", provider="owner")
    request = _FakeRequest(
        path="/api/owned", headers={"authorization": "Bearer good"}
    )

    response = _run(token_auth.token_auth_middleware(request, _call_next_ok))

    assert response.status_code == 401
    assert not hasattr(request.state, "token_authenticated")


