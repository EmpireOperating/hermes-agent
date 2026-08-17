"""Generic single-use context-publication authorization contracts."""

from __future__ import annotations

import os
import threading
import sqlite3

import pytest

import hermes_cli.context_publication as context_publication

from hermes_state import SessionDB
from hermes_cli.context_publication import (
    CONTEXT_PUBLICATION_ENVELOPE_PROTOCOL,
    BrowserPublicationPreparation,
    ContextPublicationAuthorizationService,
    ContextPublicationDecision,
    ContextPublicationError,
    ContextPublicationPolicyRegistry,
    ContextPublicationPolicyProvider,
    ContextPublicationAuthorizationRequest,
    HostPublicationBinding,
    canonicalize_context_publication,
    consume_context_publication_for_turn,
)


def test_canonicalization_matches_browser_v1_golden_vector() -> None:
    content = [
        {"type": "text", "text": "final prompt"},
        {
            "type": "image_url",
            "image_url": {
                "url": "data:image/png;base64,AA==",
                "detail": "auto",
            },
        },
    ]

    result = canonicalize_context_publication(content)

    assert result.protocol == CONTEXT_PUBLICATION_ENVELOPE_PROTOCOL
    assert result.canonical == (
        '{"content":[{"text":"final prompt","type":"text"},'
        '{"image_url":{"detail":"auto","url":"data:image/png;base64,AA=="},'
        '"type":"image_url"}],'
        '"protocol":"hermes.browser.context-publication-envelope.v1"}'
    )
    assert result.payload_sha256 == (
        "20a5f660dd11136d6366cc7d3f4087e1ef4bb847d79a9949db282ada1de076f4"
    )
    assert result.payload_byte_length == 197
    assert result.content_kind == "multimodal"
    assert result.image_count == 1


def test_canonicalization_matches_browser_lone_surrogate_vector() -> None:
    result = canonicalize_context_publication("x\ud800y")

    assert result.canonical == (
        '{"content":"x\\ud800y",'
        '"protocol":"hermes.browser.context-publication-envelope.v1"}'
    )
    assert result.payload_byte_length == 82
    assert result.payload_sha256 == (
        "dc2e6bdc26205c618907fdbb919df53b4bee2564e192360c79ae86a2f6750cb8"
    )


@pytest.mark.parametrize(
    "content",
    [
        "",
        [{"type": "text", "text": ""}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA==", "detail": "auto"}}],
        [{"type": "text", "text": "prompt"}],
        [{"type": "text", "text": "prompt"}, {"type": "image_url", "image_url": {"url": "https://example.com/a.png", "detail": "auto"}}],
        [{"type": "text", "text": "prompt"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,A", "detail": "auto"}}],
    ],
)
def test_canonicalization_rejects_noncanonical_content(content: object) -> None:
    with pytest.raises(ContextPublicationError, match="content_invalid"):
        canonicalize_context_publication(content)


class _AllowProvider(ContextPublicationPolicyProvider):
    name = "case-policy"

    def __init__(self) -> None:
        self.proposals = []

    def evaluate(self, proposal):
        self.proposals.append(proposal)
        return ContextPublicationDecision(
            allow=True,
            proposal_sha256=proposal.proposal_sha256,
            ttl_seconds=15,
        )


class _MismatchedProvider(ContextPublicationPolicyProvider):
    name = "mismatched-policy"

    def evaluate(self, proposal):
        del proposal
        return ContextPublicationDecision(
            allow=True,
            proposal_sha256="0" * 64,
        )


class _DenyProvider(ContextPublicationPolicyProvider):
    name = "deny-policy"

    def evaluate(self, proposal):
        return ContextPublicationDecision(
            allow=False,
            proposal_sha256=proposal.proposal_sha256,
        )


class _ErrorProvider(ContextPublicationPolicyProvider):
    name = "error-policy"

    def evaluate(self, proposal):
        del proposal
        raise RuntimeError("provider failed")


class _MalformedProvider(ContextPublicationPolicyProvider):
    name = "malformed-policy"

    def evaluate(self, proposal) -> ContextPublicationDecision:
        del proposal
        return {"allow": True}  # type: ignore[return-value]


class _EqualitySpoof:
    def __eq__(self, other):
        del other
        return True

    def __ne__(self, other):
        del other
        return False


class _SpoofedDigestProvider(ContextPublicationPolicyProvider):
    name = "spoofed-digest"

    def evaluate(self, proposal) -> ContextPublicationDecision:
        del proposal
        return ContextPublicationDecision(
            allow=True,
            proposal_sha256=_EqualitySpoof(),  # type: ignore[arg-type]
        )


class _BlockingProvider(ContextPublicationPolicyProvider):
    name = "blocking-policy"

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def evaluate(self, proposal):
        self.started.set()
        self.release.wait()
        return ContextPublicationDecision(
            allow=True,
            proposal_sha256=proposal.proposal_sha256,
        )


def _preparation(payload_sha256: str, payload_byte_length: int) -> BrowserPublicationPreparation:
    return BrowserPublicationPreparation.from_mapping(
        {
            "protocol": "hermes.browser.context-publication-preparation.v1",
            "envelopeProtocol": CONTEXT_PUBLICATION_ENVELOPE_PROTOCOL,
            "profileEpochId": "profile-epoch-1",
            "windowId": 7,
            "tabId": 11,
            "documentId": "document-1",
            "navigationId": "navigation-1",
            "navigationEpochId": "navigation-epoch-1",
            "origin": "https://mesh.example",
            "observedAt": 1_000_000,
            "payloadSha256": payload_sha256,
            "payloadByteLength": payload_byte_length,
            "contentKind": "text",
            "imageCount": 0,
        }
    )


def _host(
    service: ContextPublicationAuthorizationService,
    *,
    root: str = "root-1",
    tip: str = "tip-1",
) -> HostPublicationBinding:
    db = service.state_db
    if db.get_session(root) is None:
        db.create_session(root, "test")
    if db.get_session(tip) is None:
        db.create_session(tip, "test", parent_session_id=root)
    generation = db.ensure_context_publication_session_generation(tip)
    return HostPublicationBinding("default", root, tip, generation)


@pytest.mark.parametrize(
    "field",
    ["windowId", "tabId", "observedAt", "payloadByteLength", "imageCount"],
)
def test_preparation_rejects_values_outside_browser_safe_integer_range(field) -> None:
    publication = canonicalize_context_publication("private prompt")
    raw = _preparation(
        publication.payload_sha256,
        publication.payload_byte_length,
    ).as_dict()
    raw[field] = 9_007_199_254_740_992

    with pytest.raises(ContextPublicationError, match="preparation_invalid"):
        BrowserPublicationPreparation.from_mapping(raw)


def test_authorization_is_single_use_and_provider_receives_metadata_only(tmp_path) -> None:
    clock = [1_000.0]
    registry = ContextPublicationPolicyRegistry()
    provider = _AllowProvider()
    registry.register(provider, scope="default")
    service = ContextPublicationAuthorizationService(
        tmp_path / "context-publication.db",
        registry=registry,
        clock=lambda: clock[0],
        token_factory=lambda: "acceptance-token-1",
        process_id="process-1",
    )
    publication = canonicalize_context_publication("private prompt")
    preparation = _preparation(
        publication.payload_sha256,
        publication.payload_byte_length,
    )
    host = _host(service)

    acceptance = service.authorize(preparation, host)

    assert acceptance.token == "acceptance-token-1"
    assert acceptance.expires_at == 1_015.0
    assert len(provider.proposals) == 1
    assert not hasattr(provider.proposals[0], "content")
    assert service.consume(
        acceptance.token,
        preparation,
        "private prompt",
        host,
    ) == "private prompt"
    with pytest.raises(ContextPublicationError, match="authorization_replayed"):
        service.consume(
            acceptance.token,
            preparation,
            "private prompt",
            host,
        )


def test_expiry_is_exclusive_and_expired_attempt_burns_authority(tmp_path) -> None:
    clock = [1_000.0]
    registry = ContextPublicationPolicyRegistry()
    registry.register(_AllowProvider(), scope="default")
    publication = canonicalize_context_publication("private prompt")
    preparation = _preparation(
        publication.payload_sha256,
        publication.payload_byte_length,
    )
    service = ContextPublicationAuthorizationService(
        tmp_path / "state.db",
        registry=registry,
        clock=lambda: clock[0],
        token_factory=lambda: "acceptance-token-1",
        process_id="process-1",
    )
    host = _host(service)
    acceptance = service.authorize(preparation, host)
    clock[0] = acceptance.expires_at

    with pytest.raises(ContextPublicationError, match="authorization_expired"):
        service.consume(acceptance.token, preparation, "private prompt", host)
    with pytest.raises(ContextPublicationError, match="authorization_replayed"):
        service.consume(acceptance.token, preparation, "private prompt", host)


def test_binding_mismatch_burns_authority(tmp_path) -> None:
    registry = ContextPublicationPolicyRegistry()
    registry.register(_AllowProvider(), scope="default")
    publication = canonicalize_context_publication("private prompt")
    preparation = _preparation(
        publication.payload_sha256,
        publication.payload_byte_length,
    )
    service = ContextPublicationAuthorizationService(
        tmp_path / "state.db",
        registry=registry,
        clock=lambda: 1_000.0,
        token_factory=lambda: "acceptance-token-1",
        process_id="process-1",
    )
    host = _host(service)
    acceptance = service.authorize(preparation, host)

    with pytest.raises(ContextPublicationError, match="authorization_binding_drift"):
        service.consume(acceptance.token, preparation, "different prompt", host)
    with pytest.raises(ContextPublicationError, match="authorization_replayed"):
        service.consume(acceptance.token, preparation, "private prompt", host)


def test_deleted_and_recreated_session_cannot_reuse_authority(tmp_path) -> None:
    registry = ContextPublicationPolicyRegistry()
    registry.register(_AllowProvider(), scope="default")
    publication = canonicalize_context_publication("private prompt")
    preparation = _preparation(
        publication.payload_sha256,
        publication.payload_byte_length,
    )
    service = ContextPublicationAuthorizationService(
        tmp_path / "state.db",
        registry=registry,
        clock=lambda: 1_000.0,
        token_factory=lambda: "acceptance-token-1",
        process_id="process-1",
    )
    old_host = _host(service)
    acceptance = service.authorize(preparation, old_host)

    assert service.state_db.delete_session("tip-1") is True
    new_host = _host(service)
    assert new_host.session_generation != old_host.session_generation
    with pytest.raises(ContextPublicationError, match="authorization_invalid"):
        service.consume(
            acceptance.token,
            preparation,
            "private prompt",
            new_host,
        )


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        (None, "policy_unavailable"),
        (_DenyProvider(), "policy_denied"),
        (_ErrorProvider(), "policy_error"),
        (_MalformedProvider(), "policy_malformed"),
    ],
)
def test_policy_failures_are_unanimous_and_fail_closed(
    tmp_path, provider, expected
) -> None:
    registry = ContextPublicationPolicyRegistry()
    if provider is not None:
        registry.register(_AllowProvider(), scope="default")
        registry.register(provider, scope="default")
    publication = canonicalize_context_publication("private prompt")
    preparation = _preparation(
        publication.payload_sha256,
        publication.payload_byte_length,
    )
    db_path = tmp_path / "state.db"
    service = ContextPublicationAuthorizationService(
        db_path,
        registry=registry,
        clock=lambda: 1_000.0,
        process_id="process-1",
    )

    with pytest.raises(ContextPublicationError, match=expected):
        service.authorize(preparation, _host(service))
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM context_publication_acceptances"
        ).fetchone()[0] == 0


def test_policy_decision_rejects_custom_equality_digest(tmp_path) -> None:
    registry = ContextPublicationPolicyRegistry()
    registry.register(_SpoofedDigestProvider(), scope="default")
    service = ContextPublicationAuthorizationService(
        tmp_path / "context.db",
        registry=registry,
        clock=lambda: 1_000.0,
        process_id="process-1",
    )
    publication = canonicalize_context_publication("private prompt")

    with pytest.raises(ContextPublicationError, match="policy_conflict"):
        service.authorize(
            _preparation(
                publication.payload_sha256,
                publication.payload_byte_length,
            ),
            _host(service),
        )


def test_policy_decision_must_echo_exact_proposal_digest(tmp_path) -> None:
    registry = ContextPublicationPolicyRegistry()
    registry.register(_MismatchedProvider(), scope="default")
    publication = canonicalize_context_publication("private prompt")
    preparation = _preparation(
        publication.payload_sha256,
        publication.payload_byte_length,
    )
    db_path = tmp_path / "state.db"
    service = ContextPublicationAuthorizationService(
        db_path,
        registry=registry,
        clock=lambda: 1_000.0,
        process_id="process-1",
    )

    with pytest.raises(ContextPublicationError, match="policy_conflict"):
        service.authorize(preparation, _host(service))
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM context_publication_acceptances"
        ).fetchone()[0] == 0


def test_policy_timeout_fails_closed_without_minting_authority(tmp_path) -> None:
    registry = ContextPublicationPolicyRegistry(decision_timeout_seconds=0.01)
    provider = _BlockingProvider()
    registry.register(provider, scope="default")
    publication = canonicalize_context_publication("private prompt")
    preparation = _preparation(
        publication.payload_sha256,
        publication.payload_byte_length,
    )
    service = ContextPublicationAuthorizationService(
        tmp_path / "context-publication.db",
        registry=registry,
        clock=lambda: 1_000.0,
        token_factory=lambda: "acceptance-token-1",
        process_id="process-1",
    )

    try:
        with pytest.raises(ContextPublicationError, match="policy_timeout"):
            service.authorize(
                preparation,
                _host(service),
            )
    finally:
        provider.release.set()


def test_policy_lifecycle_drift_during_decision_mints_no_authority(tmp_path) -> None:
    registry = ContextPublicationPolicyRegistry(decision_timeout_seconds=1.0)
    provider = _BlockingProvider()
    registration = registry.register(provider, scope="default")
    publication = canonicalize_context_publication("private prompt")
    preparation = _preparation(
        publication.payload_sha256,
        publication.payload_byte_length,
    )
    db_path = tmp_path / "state.db"
    service = ContextPublicationAuthorizationService(
        db_path,
        registry=registry,
        clock=lambda: 1_000.0,
        process_id="process-1",
    )
    host = _host(service)
    outcome: list[str] = []

    def authorize() -> None:
        try:
            service.authorize(preparation, host)
        except ContextPublicationError as exc:
            outcome.append(str(exc))

    thread = threading.Thread(target=authorize)
    thread.start()
    assert provider.started.wait(timeout=1)
    registration.dispose()
    provider.release.set()
    thread.join(timeout=2)

    assert outcome == ["policy_lifecycle_drift"]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM context_publication_acceptances"
        ).fetchone()[0] == 0


def test_hung_policy_workers_are_bounded(tmp_path) -> None:
    registry = ContextPublicationPolicyRegistry(
        decision_timeout_seconds=0.01,
        max_providers=1,
        max_outstanding_decisions=1,
    )
    provider = _BlockingProvider()
    registry.register(provider, scope="default")
    publication = canonicalize_context_publication("private prompt")
    preparation = _preparation(
        publication.payload_sha256,
        publication.payload_byte_length,
    )
    service = ContextPublicationAuthorizationService(
        tmp_path / "state.db",
        registry=registry,
        clock=lambda: 1_000.0,
        process_id="process-1",
    )
    host = _host(service)

    try:
        with pytest.raises(ContextPublicationError, match="policy_timeout"):
            service.authorize(preparation, host)
        with pytest.raises(ContextPublicationError, match="policy_capacity"):
            service.authorize(preparation, host)
    finally:
        provider.release.set()


def test_stale_preparation_fails_before_policy_evaluation(tmp_path) -> None:
    registry = ContextPublicationPolicyRegistry()
    provider = _AllowProvider()
    registry.register(provider, scope="default")
    publication = canonicalize_context_publication("private prompt")
    stale = BrowserPublicationPreparation.from_mapping(
        {
            **_preparation(
                publication.payload_sha256,
                publication.payload_byte_length,
            ).as_dict(),
            "observedAt": 980_000,
        }
    )
    service = ContextPublicationAuthorizationService(
        tmp_path / "context-publication.db",
        registry=registry,
        clock=lambda: 1_000.0,
        process_id="process-1",
        max_preparation_age_seconds=10.0,
    )

    with pytest.raises(ContextPublicationError, match="preparation_stale"):
        service.authorize(
            stale,
            _host(service),
        )

    assert provider.proposals == []


def test_token_collision_retries_without_rechecking_policy(tmp_path) -> None:
    registry = ContextPublicationPolicyRegistry()
    provider = _AllowProvider()
    registry.register(provider, scope="default")
    tokens = iter(
        [
            "same-acceptance-token",
            "same-acceptance-token",
            "different-acceptance-token",
        ]
    )
    publication = canonicalize_context_publication("private prompt")
    preparation = _preparation(
        publication.payload_sha256,
        publication.payload_byte_length,
    )
    service = ContextPublicationAuthorizationService(
        tmp_path / "context-publication.db",
        registry=registry,
        clock=lambda: 1_000.0,
        token_factory=lambda: next(tokens),
        process_id="process-1",
    )
    host = _host(service)

    first = service.authorize(preparation, host)
    second = service.authorize(preparation, host)

    assert first.token == "same-acceptance-token"
    assert second.token == "different-acceptance-token"
    assert len(provider.proposals) == 2


def test_duplicate_provider_rejected_and_unload_invalidates_authority(tmp_path) -> None:
    registry = ContextPublicationPolicyRegistry()
    registration = registry.register(_AllowProvider(), scope="default")
    publication = canonicalize_context_publication("private prompt")
    preparation = _preparation(
        publication.payload_sha256,
        publication.payload_byte_length,
    )
    service = ContextPublicationAuthorizationService(
        tmp_path / "context-publication.db",
        registry=registry,
        clock=lambda: 1_000.0,
        token_factory=lambda: "acceptance-token-1",
        process_id="process-1",
    )
    host = _host(service)
    acceptance = service.authorize(preparation, host)

    with pytest.raises(TypeError, match="already registered"):
        registry.register(_AllowProvider(), scope="default")

    registration.dispose()
    with pytest.raises(ContextPublicationError, match="authorization_provider_drift"):
        service.consume(
            acceptance.token,
            preparation,
            "private prompt",
            host,
        )
    registry.register(_AllowProvider(), scope="default")
    with pytest.raises(ContextPublicationError, match="authorization_replayed"):
        service.consume(
            acceptance.token,
            preparation,
            "private prompt",
            host,
        )


def test_concurrent_consumption_is_atomic_and_store_omits_raw_context(tmp_path) -> None:
    registry = ContextPublicationPolicyRegistry()
    registry.register(_AllowProvider(), scope="default")
    publication = canonicalize_context_publication("private prompt")
    preparation = _preparation(
        publication.payload_sha256,
        publication.payload_byte_length,
    )
    db_path = tmp_path / "context-publication.db"
    service = ContextPublicationAuthorizationService(
        db_path,
        registry=registry,
        clock=lambda: 1_000.0,
        token_factory=lambda: "acceptance-token-1",
        process_id="process-1",
    )
    host = _host(service)
    acceptance = service.authorize(preparation, host)
    peer = ContextPublicationAuthorizationService(
        SessionDB(db_path),
        registry=registry,
        clock=lambda: 1_000.0,
        process_id="process-1",
    )
    barrier = threading.Barrier(8)
    outcomes = []

    def consume(consumer) -> None:
        barrier.wait()
        try:
            consumer.consume(
                acceptance.token,
                preparation,
                "private prompt",
                host,
            )
            outcomes.append("consumed")
        except ContextPublicationError as exc:
            outcomes.append(str(exc))

    threads = [
        threading.Thread(target=consume, args=(service if index % 2 else peer,))
        for index in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert outcomes.count("consumed") == 1
    assert outcomes.count("authorization_replayed") == 7
    with sqlite3.connect(db_path) as conn:
        stored = repr(conn.execute(
            "SELECT * FROM context_publication_acceptances"
        ).fetchall())
    assert "private prompt" not in stored
    assert "acceptance-token-1" not in stored
    assert "mesh.example" not in stored
    assert "navigation-1" not in stored
    assert "document-1" not in stored


def test_process_epoch_changes_after_fork() -> None:
    if not hasattr(os, "fork"):
        pytest.skip("fork is unavailable")
    parent_epoch = context_publication._CONTEXT_PUBLICATION_PROCESS_ID
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(read_fd)
            context_publication._ensure_context_publication_process_epoch()
            os.write(
                write_fd,
                context_publication._CONTEXT_PUBLICATION_PROCESS_ID.encode("ascii"),
            )
        finally:
            os._exit(0)

    os.close(write_fd)
    child_epoch = os.read(read_fd, 256).decode("ascii")
    os.close(read_fd)
    _, status = os.waitpid(pid, 0)

    assert os.waitstatus_to_exitcode(status) == 0
    assert child_epoch
    assert child_epoch != parent_epoch


def test_turn_consumption_resolves_host_binding_before_conversation(monkeypatch) -> None:
    publication = canonicalize_context_publication("private prompt")
    preparation = _preparation(
        publication.payload_sha256,
        publication.payload_byte_length,
    )
    request = ContextPublicationAuthorizationRequest.from_mapping(
        {
            "protocol": "hermes.context-publication-authorization.v1",
            "token": "acceptance-token-1",
            "preparation": preparation.as_dict(),
        }
    )
    calls = []

    class _Service:
        def consume(self, token, observed_preparation, content, host):
            calls.append((token, observed_preparation, content, host))
            return content

    class _StateDB:
        def get_context_publication_session_generation(self, session_id):
            assert session_id == "tip-1"
            return "session-generation-1"

    class _Agent:
        session_id = "tip-1"
        _session_db = _StateDB()

        def _conversation_root_id(self):
            return "root-1"

    monkeypatch.setattr(
        "hermes_cli.context_publication.get_context_publication_authorization_service",
        lambda _state_db=None: _Service(),
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name",
        lambda: "default",
    )

    consume_context_publication_for_turn(_Agent(), "private prompt", request)

    assert calls == [
        (
            "acceptance-token-1",
            preparation,
            "private prompt",
            HostPublicationBinding(
                "default", "root-1", "tip-1", "session-generation-1"
            ),
        )
    ]
