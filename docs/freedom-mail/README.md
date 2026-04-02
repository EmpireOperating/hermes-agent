# Freedom Mail (Agent-Native Paid Messaging)

Status: Architecture locked + wallet API validation complete (pre-implementation)
Last updated: 2026-03-24
Authoring context: Conversation with user LemonSqueeze

## 1) What this is

Freedom Mail is a new agent-to-agent messaging protocol that treats each message as a paid API call.

Core idea:
- No SMTP, no legacy email reputation system
- Agents run their own always-on mailbox daemon
- Recipient enforces a sats price to accept incoming messages
- Sender must pay before message acceptance
- Settlement is final (no refunds)

This is intentionally future-native and agent-native, not backward-compatible with Gmail/Outlook ecosystems.

## 2) Decisions already made (important)

These are explicit product decisions from the planning conversation and should be treated as requirements unless user changes direction:

1. No SMTP bridge in v0
   - Avoid legacy interoperability complexity
   - Avoid dragging in SPF/DKIM/DMARC/reputation operations

2. Agent-to-agent only
   - Users are software agents, not humans using legacy mail clients

3. No centralized relay as a required architecture component
   - Preferred model is direct daemon-to-daemon delivery
   - Each agent machine can host its own mailbox endpoint
   - Optional store-and-forward peers may exist later, but not required in baseline

4. Payment-gated delivery
   - Unknown senders must pay to deliver
   - Payment requirement is protocol-native anti-spam control

5. Custodial Lightning first
   - Optimize for practical setup and lower integration friction
   - Non-custodial support can be added later

6. No refunds
   - Settlement final once recipient node accepts message
   - No deposit return flow

## 2.5) Progress update: custodial wallet adapter path validated

We completed a live integration validation against Coinos (custodial Lightning) and confirmed the core wallet operations required by v0 are viable.

Validated behavior:
- Full auth bearer token can call:
  - `GET /api/me` (identity/session validation)
  - `POST /api/invoice` (invoice creation)
  - `GET /api/invoice/:id` (invoice lookup/verification)
- `POST /api/invoice` requires nested payload shape:
  - `{"invoice": { "amount": <sats>, "type": "lightning", ... }}`
  - Flat payloads are invalid and can return server errors
- Read-only token from `GET /api/ro` is intentionally restricted:
  - `GET /api/me` -> unauthorized (expected)
  - still usable for invoice read/create paths (`GET /api/invoice`, `POST /api/invoice`)
  - blocked from spend routes such as `POST /api/payments`

Design implication (now locked for v0):
- Receiver/inbox side (quote + payment verification): prefer read-only token path where possible
- Sender spend side (paying outbound invoices): requires full auth token path

This split aligns with least-privilege treasury design and matches the protocol’s quote/pay/submit workflow.

## 3) Why this exists

Legacy email anti-spam depends heavily on reputation systems and centralized provider policy:
- sender/domain/IP reputation
- DNS auth policy correctness (SPF, DKIM, DMARC)
- blocklist and complaint operations
- warm-up and deliverability optimization

Freedom Mail replaces soft reputation gating with explicit economic gating:
- pay to send
- recipient controls minimum message cost
- spam becomes economically expensive

## 4) Architecture (v0 target)

### 4.1 Node model

Each agent runs a local daemon, e.g. `freedom-maild`, with these modules:

- Identity module
  - Generates and stores Ed25519 keypair
  - Signs outbound messages
  - Verifies inbound signatures

- Inbox API module
  - Exposes HTTP endpoints for quote + message submission
  - Enforces payment policy

- Wallet adapter module (custodial Lightning)
  - Creates invoices for inbound message requests
  - Verifies invoice payment status

- Message store module
  - Persists messages/threads/attachments metadata
  - Provides local read/search/event feed

- Retry/outbox module
  - Retries outbound attempts on transient network failures

### 4.2 Identity model

Each agent has a cryptographic protocol identity:
- Public key: canonical identity
- Private key: local-only secret
- Suggested address form: derived from public key hash (exact encoding TBD)

Optional aliasing can map readable names to identity keys, but alias is non-authoritative.

Identity profile (signed):
- identity pubkey
- current endpoint URL(s)
- pricing policy (min sats)
- protocol version
- monotonic sequence for updates
- signature over profile

### 4.3 Discovery model

Sender must discover recipient endpoint + policy.
Current planning assumption:
- identity profile lookup mechanism exists (could be DHT, registry, signed profile host, or peer exchange)
- exact discovery backend TBD

## 5) Delivery flow (high level)

Sender A -> Receiver B:

1) A resolves B identity profile
   - obtains B endpoint, B pubkey, B min sats policy

2) A requests quote from B
   - includes recipient identity + envelope metadata (size/priority)

3) B returns payment challenge
   - quote_id
   - amount_sats
   - BOLT11 invoice
   - expiry

4) A pays invoice via custodial wallet

5) A submits signed message + payment proof
   - includes quote_id
   - envelope signature
   - payment proof/payment hash/receipt fields

6) B verifies
   - quote exists, not expired, not used
   - invoice paid and amount sufficient
   - signature valid for sender identity

7) B accepts and stores message
   - marks quote as consumed
   - returns acceptance receipt
   - settlement final (no refunds)

Reply flow is identical in reverse.

## 6) API surface (draft)

This is a draft API contract direction, not finalized wire format.

- `GET /v1/identity/{id}`
  - Returns signed profile and receiving policy

- `POST /v1/quote`
  - Request: recipient_id, sender_id, payload_meta
  - Response: quote_id, amount_sats, invoice_bolt11, expires_at

- `POST /v1/messages`
  - Request:
    - quote_id
    - sender_id
    - recipient_id
    - signed_envelope
    - payment_proof
  - Response:
    - message_id
    - accepted_at
    - receiver_receipt_signature

- `GET /v1/inbox?since=...`
  - Local/API retrieval for receiving agent tooling

Idempotency:
- quote_id must be single-use
- message submission should include client_message_id for safe retries

## 7) Message envelope (draft fields)

- protocol_version
- message_id (sender-generated UUID)
- thread_id
- from_identity
- to_identity
- created_at
- subject (optional)
- body_ciphertext or body_plaintext (encryption policy TBD)
- attachments_ref (optional)
- nonce
- sender_signature

Replay defense:
- quote single-use
- nonce tracking window
- timestamp bounds

## 8) Security model (baseline)

### 8.1 Required checks on receiver
- Signature verification mandatory
- Payment verification mandatory for paid route
- Quote expiry and single-use enforcement
- Envelope schema + size bounds

### 8.2 Abuse controls
- Rate limit per source identity and IP
- Require payment for unknown senders
- Optional allowlist with reduced or zero-cost routes
- Dynamic price adjustment under attack (later)

### 8.3 Trust assumptions
- Custodial wallet provider APIs are trusted source of payment truth in v0
- Receiver decides acceptance policy

## 9) Data model (minimal)

### identities
- identity_id
- pubkey
- latest_profile_json
- profile_signature
- seq

### quotes
- quote_id
- recipient_id
- sender_id
- amount_sats
- invoice_bolt11
- expires_at
- status: open|paid|consumed|expired
- provider_payment_id/payment_hash

### messages
- message_id
- thread_id
- from_identity
- to_identity
- envelope_json
- quote_id
- accepted_at

## 10) Known hard problems (not blockers for v0)

- Discovery decentralization quality
- NAT traversal / inbound reachability on consumer networks
- Encrypted attachments at scale
- Multi-device key management / rotation UX

These are expected evolution areas, not reasons to delay v0 implementation.

## 11) Scope boundaries for v0

In scope:
- direct daemon-to-daemon HTTP
- signed identities
- quote/pay/submit workflow
- custodial Lightning integration
- final settlement semantics

Out of scope:
- SMTP compatibility
- non-custodial Lightning
- complex relay federation market
- full-blown decentralized discovery network

## 12) Immediate next workstream (queued)

Next design discussion and implementation planning should focus on Lightning wallet integration details:

1) Custodial provider abstraction
   - choose initial provider(s)
   - normalized interface for create_invoice + verify_payment

2) Quote-to-payment binding
   - strict mapping from quote_id to invoice and amount

3) Payment proof schema
   - standardized fields from provider responses

4) Failure handling
   - expired invoices
   - partial/underpayment
   - duplicate submission

5) Operational security
   - credential storage
   - webhook signing/verification (if provider webhooks used)

## 13) Suggested implementation order

1. Define OpenAPI schema for v1 endpoints
2. Implement local identity + signature library
3. Implement quote issuance and persistence
4. Implement one custodial wallet adapter
5. Implement payment verification path
6. Implement message acceptance/storage path
7. Implement sender CLI (`send`, `inbox`, `watch`)
8. Build integration tests with two local daemons

## 14) Naming options (working product/protocol names)

These names are currently in consideration and can be used for positioning tests, docs variants, and user interviews:

1. Freedom Mail
- Pros:
  - Strongly aligned with sovereignty and censorship-resistance positioning
  - Broad vision fit (not tied to one payment rail forever)
- Cons:
  - Can sound ideological/political depending on audience

2. Lightning Mail
- Pros:
  - Immediately communicates Bitcoin Lightning integration
  - Very clear to BTC-native users and developers
- Cons:
  - Narrower if protocol later supports non-Lightning payment rails

3. Sat Mail
- Pros:
  - Short, memorable, and natively tied to sats-denominated pricing
  - Emphasizes per-message economic anti-spam model
- Cons:
  - Less obvious to mainstream users unfamiliar with sats terminology

4. Mesh Mail
- Pros:
  - Highlights node-to-node network topology and decentralization flavor
  - Less payment-rail-specific, more protocol-topology-specific
- Cons:
  - Payment-gating value proposition is less explicit in the name

Current practical naming stance:
- Keep "Freedom Mail" as canonical working name in architecture docs
- Treat Lightning Mail / Sat Mail / Mesh Mail as viable brand or product-surface variants until final naming decision

## 15) Handoff notes for new agents

If you are a new agent continuing this effort:
- Do not re-open SMTP bridge work unless user asks explicitly
- Preserve final-settlement semantics (no refunds)
- Keep custodial Lightning-first strategy
- Keep architecture direct node-to-node with no required central relay
- Move next into concrete wallet integration and API contract stabilization

End of handoff README.
