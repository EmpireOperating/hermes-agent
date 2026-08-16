"""Plugin registration for generic context-publication policy providers."""

from __future__ import annotations

from hermes_cli.context_publication import (
    ContextPublicationDecision,
    ContextPublicationPolicyProvider,
    get_context_publication_policy_registry,
)
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest


class _Provider(ContextPublicationPolicyProvider):
    name = "case-policy"

    def evaluate(self, proposal):
        return ContextPublicationDecision(
            allow=True,
            proposal_sha256=proposal.proposal_sha256,
        )


class _Manager:
    scope_key = "/tmp/profile-a"

    def __init__(self) -> None:
        self.release = None

    def _track_registration(self, _manifest, _kind, _key, release):
        self.release = release
        return object()


def test_plugin_context_registers_and_unloads_policy_provider() -> None:
    registry = get_context_publication_policy_registry()
    registry.clear_for_tests()
    manager = _Manager()
    ctx = PluginContext(
        PluginManifest(name="empire-policy", version="0.0.1", description="test"),
        manager,
    )

    handle = ctx.register_context_publication_policy_provider(_Provider())

    assert handle is not None
    assert registry.current_generations("/tmp/profile-a") == {"case-policy": 1}
    assert manager.release is not None
    manager.release()
    assert registry.current_generations("/tmp/profile-a") == {}


def test_real_plugin_manager_uses_resolved_profile_scope(tmp_path) -> None:
    registry = get_context_publication_policy_registry()
    registry.clear_for_tests()
    scope = str(tmp_path.resolve())
    manager = PluginManager(scope_key=scope)
    ctx = PluginContext(
        PluginManifest(name="empire-policy", version="0.0.1", description="test"),
        manager,
    )

    handle = ctx.register_context_publication_policy_provider(_Provider())

    assert handle is not None
    assert manager.scope_key == scope
    assert registry.current_generations(scope) == {"case-policy": 1}
    handle.dispose()
    assert registry.current_generations(scope) == {}
