"""The plugin context exposes register_dashboard_auth_provider.

Mirrors the image-gen / memory-provider hooks (see plugins.py:531 for prior
art).
"""
from __future__ import annotations

import pytest

from hermes_cli.dashboard_auth import clear_providers, get_provider
from hermes_cli.dashboard_auth import token_auth
from hermes_cli.dashboard_auth.base import (
    DashboardAuthProvider, LoginStart, Session,
)
from hermes_cli.plugins import PluginContext, PluginManifest, PluginRegistration


class _Stub(DashboardAuthProvider):
    name = "stub"
    display_name = "Stub IdP"

    def start_login(self, *, redirect_uri):
        return LoginStart(redirect_url="x", cookie_payload={})

    def complete_login(self, *, code, state, code_verifier, redirect_uri):
        return Session("u", "e", "n", "o", "stub", 0, "a", "r")

    def verify_session(self, *, access_token):
        return None

    def refresh_session(self, *, refresh_token):
        return Session("u", "e", "n", "o", "stub", 0, "a", "r")

    def revoke_session(self, *, refresh_token):
        return None


class _MinimalManager:
    """The fixture only needs whatever PluginContext touches at register-time.

    We don't import the real PluginManager because it pulls in the full
    plugin-discovery surface.  The hook we're testing only reads from
    ``ctx.manifest``, so the manager attributes don't matter — but we set
    the few that other PluginContext methods touch defensively.
    """

    _cli_ref = None
    _context_engine = None
    _tools: dict = {}
    scope_key = "test-profile"

    def _track_registration(self, manifest, kind, key, release):
        return PluginRegistration(
            kind=kind,
            key=key,
            release=release,
            plugin_key=manifest.name,
        )


@pytest.fixture(autouse=True)
def _isolated_registry():
    clear_providers()
    token_auth.clear_token_routes()
    yield
    token_auth.clear_token_routes()
    clear_providers()


def _make_ctx(name: str = "dashboard-auth-stub") -> PluginContext:
    manifest = PluginManifest(name=name, version="0.0.1", description="stub")
    return PluginContext(manifest=manifest, manager=_MinimalManager())  # type: ignore[arg-type]


def test_plugin_ctx_exposes_register_dashboard_auth_provider():
    ctx = _make_ctx()
    assert hasattr(ctx, "register_dashboard_auth_provider")
    assert hasattr(ctx, "register_dashboard_token_route")


def test_plugin_ctx_token_route_handle_cleans_up_profile_scoped_policy():
    ctx = _make_ctx()
    handle = ctx.register_dashboard_token_route(
        "/api/plugin/machine",
        provider="stub",
        required_scopes=("write", "read"),
    )

    assert token_auth.is_token_route(
        "/api/plugin/machine", scope=_MinimalManager.scope_key
    )
    assert not token_auth.is_token_route(
        "/api/plugin/machine", scope="other-profile"
    )

    handle.dispose()

    assert not token_auth.is_token_route(
        "/api/plugin/machine", scope=_MinimalManager.scope_key
    )


def test_plugin_ctx_token_route_tracking_failure_rolls_back_raw_registration():
    class FailingManager(_MinimalManager):
        def _track_registration(self, manifest, kind, key, release):
            raise RuntimeError("tracking unavailable")

    manifest = PluginManifest(name="dashboard-auth-failing", version="0.0.1")
    ctx = PluginContext(manifest=manifest, manager=FailingManager())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="tracking unavailable"):
        ctx.register_dashboard_token_route(
            "/api/plugin/failing", provider="stub", required_scopes=("write",)
        )

    assert not token_auth.is_token_route(
        "/api/plugin/failing", scope=FailingManager.scope_key
    )


def test_plugin_ctx_silently_ignores_non_provider(caplog):
    """Mirror image_gen behaviour: log warning, leave registry empty.

    We do NOT raise — a misbehaving plugin must not crash the host.
    """
    import logging
    ctx = _make_ctx("dashboard-auth-bad")
    with caplog.at_level(logging.WARNING):
        ctx.register_dashboard_auth_provider("not a provider")  # type: ignore[arg-type]
    assert get_provider("stub") is None
    assert any(
        "dashboard-auth-bad" in rec.message
        and "DashboardAuthProvider" in rec.message
        for rec in caplog.records
    )
