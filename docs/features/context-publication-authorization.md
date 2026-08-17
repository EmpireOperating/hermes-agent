# Context publication authorization

Hermes exposes a generic, fail-closed boundary for publishing Browser-sourced
prompt and image context. The boundary runs before `build_turn_context()`, so a
protected turn cannot enter transcript persistence or model input until a
host-owned, single-use acceptance has been atomically consumed.

This is a generic enforcement seam. It does not implement Empire Care policy,
start a support session, observe a browser, or authorize remote input.

## Protocols

- Canonical envelope: `hermes.browser.context-publication-envelope.v1`
- Metadata-only preparation: `hermes.browser.context-publication-preparation.v1`
- Acceptance request: `hermes.context-publication-authorization.v1`

The envelope is recursively key-sorted JSON encoded as exact UTF-8. Text is not
normalized, image order is significant, and complete canonical image data URLs
contribute to SHA-256. Hermes independently validates and hashes the final
message at consumption.

A preparation contains bounded browser/profile/navigation metadata and the
envelope digest. It contains no prompt text or image bytes and is not authority.

## Dedicated API operations

Protected publication never uses a caller-controlled bypass flag on ordinary
chat routes.

1. `POST /api/sessions/{session_id}/context-publications/authorize`
   - Body: `{ "preparation": { ... } }`
   - Hermes resolves the active profile, conversation root, and current session
     tip.
   - Every host-registered policy provider must allow the metadata-only
     proposal.
   - Hermes returns an opaque acceptance token with an exclusive Unix-millisecond
     `expiresAt`.
2. `POST /api/sessions/{session_id}/context-publications`
   - Body: `{ "message": ..., "publicationAuthorization": { ... } }`

There is deliberately no protected streaming route in v1: final consumption
must complete under the turn lease before response headers are committed.

Ordinary `/chat` and `/chat/stream` reject a
`publicationAuthorization` field and remain separate unrestricted operations.
Protected routes reject per-turn `system_message` and `instructions`, because
those fields would add model context not bound by the v1 envelope. A reviewed
host may persist trusted session instructions separately.

## Policy provider

Plugins register metadata-only policy with:

```python
from hermes_cli.context_publication import (
    ContextPublicationDecision,
    ContextPublicationPolicyProvider,
)

class AttendedPolicy(ContextPublicationPolicyProvider):
    name = "attended-policy"

    def evaluate(self, proposal):
        # proposal contains preparation metadata and host bindings, never raw
        # prompt text or image bytes.
        return ContextPublicationDecision(
            allow=True,
            proposal_sha256=proposal.proposal_sha256,
            ttl_seconds=15,
        )

ctx.register_context_publication_policy_provider(AttendedPolicy())
```

Policy is profile scoped and unanimous. Provider absence, denial, exception,
malformed output, digest disagreement, or timeout fails closed. Decision workers
are bounded and provider callbacks run without the registry mutation lock.
Hermes owns provider generations; duplicate provider names are rejected, and
unload invalidates and burns outstanding acceptance tokens.

## Atomic consumption

The acceptance table lives in the profile's canonical `state.db` and contains
token hashes, exact payload and proposal digests, immutable session-incarnation
bindings, provider generations, and lifecycle timestamps. It never stores raw
preparation metadata, origins, URLs, prompt text, image bytes, or acceptance
tokens. Deleting a session cascades deletion of its outstanding acceptances.

Consumption occurs only for an existing, persistence-enabled session backed by a
state store that implements the durable turn-lease protocol. Hermes fails closed
before consuming authority when any of those prerequisites is absent. Once
admitted, consumption occurs after Hermes owns the durable session turn lease and
has unconditionally resolved and reloaded the latest session tip, but before
conversation setup. One SQLite
`BEGIN IMMEDIATE` transaction verifies exclusive expiry, process/profile/root/
tip/incarnation bindings, exact proposal digest, provider generations, and a
conditional pending-to-consumed update. The final message is re-canonicalized
and re-hashed before that transaction.

Exactly one concurrent consumer succeeds. Replay, payload substitution, stale
preparation, host drift, provider drift, process restart, and expiry fail
closed and known-token binding failures burn authority. If model publication
later fails, consumed authority is not restored.
