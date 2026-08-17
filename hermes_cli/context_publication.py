"""Host-owned primitives for exact Browser context publication.

The public surface in this module is generic: Browser clients canonicalize one
prompt-plus-image envelope, while Hermes verifies the same bytes before any
protected publication can enter transcript persistence or model input.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import queue
import re
import secrets
import sqlite3
import threading
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

CONTEXT_PUBLICATION_ENVELOPE_PROTOCOL = (
    "hermes.browser.context-publication-envelope.v1"
)
CONTEXT_PUBLICATION_PREPARATION_PROTOCOL = (
    "hermes.browser.context-publication-preparation.v1"
)
CONTEXT_PUBLICATION_AUTHORIZATION_PROTOCOL = (
    "hermes.context-publication-authorization.v1"
)
CONTEXT_PUBLICATION_PROPOSAL_PROTOCOL = (
    "hermes.context-publication-proposal.v1"
)

_MAX_TEXT_BYTES = 1_048_576
_MAX_IMAGE_DATA_URL_BYTES = 15_000_000
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_DATA_URL_RE = re.compile(
    r"^data:image/[a-z0-9.+-]+;base64,([a-z0-9+/]+={0,2})$",
    re.IGNORECASE,
)


class ContextPublicationError(ValueError):
    """A bounded context-publication request failed closed."""


@dataclass(frozen=True, slots=True)
class CanonicalContextPublication:
    protocol: str
    content: Any
    canonical: str
    payload_sha256: str
    payload_byte_length: int
    content_kind: str
    image_count: int


def _utf8_bytes(value: str) -> bytes:
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ContextPublicationError("content_invalid") from exc


def _browser_utf8_bytes(value: str) -> bytes:
    """Match TextEncoder's replacement of unpaired UTF-16 surrogates."""
    normalized: list[str] = []
    index = 0
    while index < len(value):
        codepoint = ord(value[index])
        if 0xD800 <= codepoint <= 0xDBFF and index + 1 < len(value):
            low = ord(value[index + 1])
            if 0xDC00 <= low <= 0xDFFF:
                normalized.append(
                    chr(0x10000 + ((codepoint - 0xD800) << 10) + low - 0xDC00)
                )
                index += 2
                continue
        if 0xD800 <= codepoint <= 0xDFFF:
            normalized.append("\ufffd")
        else:
            normalized.append(value[index])
        index += 1
    return "".join(normalized).encode("utf-8")


def _json_dumps_browser_stable(value: Any) -> str:
    """Match Browser Companion stableStringify for Unicode edge cases."""
    serialized = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    output: list[str] = []
    index = 0
    while index < len(serialized):
        codepoint = ord(serialized[index])
        if 0xD800 <= codepoint <= 0xDBFF:
            if index + 1 < len(serialized):
                low = ord(serialized[index + 1])
                if 0xDC00 <= low <= 0xDFFF:
                    output.append(
                        chr(0x10000 + ((codepoint - 0xD800) << 10) + low - 0xDC00)
                    )
                    index += 2
                    continue
            output.append(f"\\u{codepoint:04x}")
        elif 0xDC00 <= codepoint <= 0xDFFF:
            output.append(f"\\u{codepoint:04x}")
        else:
            output.append(serialized[index])
        index += 1
    return "".join(output)


def _exact_dict(value: Any, keys: set[str]) -> dict[str, Any] | None:
    if type(value) is not dict or set(value) != keys:  # noqa: E721 - exact schema
        return None
    if not all(type(key) is str for key in value):  # noqa: E721 - exact schema
        return None
    return value


def _valid_image_data_url(value: Any) -> bool:
    if type(value) is not str:  # noqa: E721 - reject string subclasses
        return False
    encoded_value = _browser_utf8_bytes(value)
    if len(encoded_value) > _MAX_IMAGE_DATA_URL_BYTES:
        return False
    match = _IMAGE_DATA_URL_RE.fullmatch(value)
    if match is None:
        return False
    encoded = match.group(1)
    if not encoded or len(encoded) % 4:
        return False
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return False
    return base64.b64encode(decoded).decode("ascii") == encoded


def _sanitize_content(content: Any) -> str | list[dict[str, Any]]:
    if type(content) is str:  # noqa: E721 - exact transport shape
        if not content or len(_browser_utf8_bytes(content)) > _MAX_TEXT_BYTES:
            raise ContextPublicationError("content_invalid")
        return content

    if type(content) is not list or not 2 <= len(content) <= 7:  # noqa: E721
        raise ContextPublicationError("content_invalid")

    text_part = _exact_dict(content[0], {"type", "text"})
    if (
        text_part is None
        or text_part["type"] != "text"
        or type(text_part["text"]) is not str  # noqa: E721
        or not text_part["text"]
        or len(_browser_utf8_bytes(text_part["text"])) > _MAX_TEXT_BYTES
    ):
        raise ContextPublicationError("content_invalid")

    sanitized: list[dict[str, Any]] = [
        {"type": "text", "text": text_part["text"]}
    ]
    for raw_part in content[1:]:
        image_part = _exact_dict(raw_part, {"type", "image_url"})
        image_url = (
            _exact_dict(image_part["image_url"], {"url", "detail"})
            if image_part is not None
            else None
        )
        if (
            image_part is None
            or image_part["type"] != "image_url"
            or image_url is None
            or image_url["detail"] != "auto"
            or not _valid_image_data_url(image_url["url"])
        ):
            raise ContextPublicationError("content_invalid")
        sanitized.append(
            {
                "type": "image_url",
                "image_url": {"url": image_url["url"], "detail": "auto"},
            }
        )
    return sanitized


def canonicalize_context_publication(content: Any) -> CanonicalContextPublication:
    """Validate and canonicalize one Browser publication envelope.

    Canonical JSON matches the Browser Companion v1 contract: object keys are
    recursively sorted, arrays preserve order, UTF-8 bytes are exact, and no
    whitespace or Unicode normalization is introduced.
    """

    sanitized = _sanitize_content(content)
    envelope = {
        "protocol": CONTEXT_PUBLICATION_ENVELOPE_PROTOCOL,
        "content": sanitized,
    }
    canonical = _json_dumps_browser_stable(envelope)
    canonical_bytes = _utf8_bytes(canonical)
    return CanonicalContextPublication(
        protocol=CONTEXT_PUBLICATION_ENVELOPE_PROTOCOL,
        content=sanitized,
        canonical=canonical,
        payload_sha256=hashlib.sha256(canonical_bytes).hexdigest(),
        payload_byte_length=len(canonical_bytes),
        content_kind="multimodal" if isinstance(sanitized, list) else "text",
        image_count=(len(sanitized) - 1 if isinstance(sanitized, list) else 0),
    )


_PREPARATION_KEYS = {
    "protocol",
    "envelopeProtocol",
    "profileEpochId",
    "windowId",
    "tabId",
    "documentId",
    "navigationId",
    "navigationEpochId",
    "origin",
    "observedAt",
    "payloadSha256",
    "payloadByteLength",
    "contentKind",
    "imageCount",
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PROVIDER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _identifier(value: Any) -> bool:
    return type(value) is str and 1 <= len(value) <= 256  # noqa: E721


def _nonnegative_integer(value: Any) -> bool:
    return (
        type(value) is int  # noqa: E721
        and 0 <= value <= 9_007_199_254_740_991
    )


def _origin(value: Any) -> bool:
    if type(value) is not str or len(value) > 2048:  # noqa: E721
        return False
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return False
    if parsed.path not in {"", "/"}:
        return False
    expected = f"{parsed.scheme}://{parsed.netloc}"
    return value == expected


@dataclass(frozen=True, slots=True)
class BrowserPublicationPreparation:
    protocol: str
    envelope_protocol: str
    profile_epoch_id: str
    window_id: int
    tab_id: int
    document_id: str
    navigation_id: str
    navigation_epoch_id: str
    origin: str
    observed_at: int
    payload_sha256: str
    payload_byte_length: int
    content_kind: str
    image_count: int

    @classmethod
    def from_mapping(cls, value: Any) -> "BrowserPublicationPreparation":
        raw = _exact_dict(value, _PREPARATION_KEYS)
        if raw is None:
            raise ContextPublicationError("preparation_invalid")
        if (
            raw["protocol"] != CONTEXT_PUBLICATION_PREPARATION_PROTOCOL
            or raw["envelopeProtocol"] != CONTEXT_PUBLICATION_ENVELOPE_PROTOCOL
            or not _identifier(raw["profileEpochId"])
            or not _nonnegative_integer(raw["windowId"])
            or not _nonnegative_integer(raw["tabId"])
            or not _identifier(raw["documentId"])
            or not _identifier(raw["navigationId"])
            or not _identifier(raw["navigationEpochId"])
            or not _origin(raw["origin"])
            or not _nonnegative_integer(raw["observedAt"])
            or raw["observedAt"] > 9_007_199_254_740_991
            or type(raw["payloadSha256"]) is not str  # noqa: E721
            or _SHA256_RE.fullmatch(raw["payloadSha256"]) is None
            or not _nonnegative_integer(raw["payloadByteLength"])
            or raw["payloadByteLength"] > 100_000_000
            or raw["contentKind"] not in {"text", "multimodal"}
            or not _nonnegative_integer(raw["imageCount"])
            or raw["imageCount"] > 6
            or (raw["contentKind"] == "text" and raw["imageCount"] != 0)
            or (raw["contentKind"] == "multimodal" and raw["imageCount"] < 1)
        ):
            raise ContextPublicationError("preparation_invalid")
        return cls(
            protocol=raw["protocol"],
            envelope_protocol=raw["envelopeProtocol"],
            profile_epoch_id=raw["profileEpochId"],
            window_id=raw["windowId"],
            tab_id=raw["tabId"],
            document_id=raw["documentId"],
            navigation_id=raw["navigationId"],
            navigation_epoch_id=raw["navigationEpochId"],
            origin=raw["origin"],
            observed_at=raw["observedAt"],
            payload_sha256=raw["payloadSha256"],
            payload_byte_length=raw["payloadByteLength"],
            content_kind=raw["contentKind"],
            image_count=raw["imageCount"],
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "envelopeProtocol": self.envelope_protocol,
            "profileEpochId": self.profile_epoch_id,
            "windowId": self.window_id,
            "tabId": self.tab_id,
            "documentId": self.document_id,
            "navigationId": self.navigation_id,
            "navigationEpochId": self.navigation_epoch_id,
            "origin": self.origin,
            "observedAt": self.observed_at,
            "payloadSha256": self.payload_sha256,
            "payloadByteLength": self.payload_byte_length,
            "contentKind": self.content_kind,
            "imageCount": self.image_count,
        }


@dataclass(frozen=True, slots=True)
class HostPublicationBinding:
    profile_name: str
    session_root_id: str
    session_tip_id: str
    session_generation: str

    def __post_init__(self) -> None:
        if not all(
            _identifier(value)
            for value in (
                self.profile_name,
                self.session_root_id,
                self.session_tip_id,
                self.session_generation,
            )
        ):
            raise ContextPublicationError("host_binding_invalid")


@dataclass(frozen=True, slots=True)
class ContextPublicationProposal:
    preparation: BrowserPublicationPreparation
    host: HostPublicationBinding
    process_id: str
    proposal_sha256: str

    @classmethod
    def create(
        cls,
        preparation: BrowserPublicationPreparation,
        host: HostPublicationBinding,
        process_id: str,
    ) -> "ContextPublicationProposal":
        if not _identifier(process_id):
            raise ContextPublicationError("host_binding_invalid")
        canonical = _json_dumps_browser_stable(
            {
                "protocol": CONTEXT_PUBLICATION_PROPOSAL_PROTOCOL,
                "preparation": preparation.as_dict(),
                "host": {
                    "profileName": host.profile_name,
                    "sessionRootId": host.session_root_id,
                    "sessionTipId": host.session_tip_id,
                    "sessionGeneration": host.session_generation,
                    "processId": process_id,
                },
            }
        )
        digest = hashlib.sha256(_utf8_bytes(canonical)).hexdigest()
        return cls(
            preparation=preparation,
            host=host,
            process_id=process_id,
            proposal_sha256=digest,
        )


@dataclass(frozen=True, slots=True)
class ContextPublicationDecision:
    allow: bool
    proposal_sha256: str
    ttl_seconds: float | None = None


class ContextPublicationPolicyProvider(ABC):
    """Metadata-only policy provider for protected context publication."""

    name: str

    @abstractmethod
    def evaluate(
        self, proposal: ContextPublicationProposal
    ) -> ContextPublicationDecision:
        """Return one fail-closed decision without receiving raw content."""


@dataclass(frozen=True, slots=True)
class ContextPublicationAcceptance:
    token: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class ContextPublicationAuthorizationRequest:
    protocol: str
    token: str
    preparation: BrowserPublicationPreparation

    @classmethod
    def from_mapping(cls, value: Any) -> "ContextPublicationAuthorizationRequest":
        raw = _exact_dict(value, {"protocol", "token", "preparation"})
        if (
            raw is None
            or raw["protocol"] != CONTEXT_PUBLICATION_AUTHORIZATION_PROTOCOL
            or type(raw["token"]) is not str  # noqa: E721
            or not 16 <= len(raw["token"]) <= 256
        ):
            raise ContextPublicationError("authorization_invalid")
        return cls(
            protocol=raw["protocol"],
            token=raw["token"],
            preparation=BrowserPublicationPreparation.from_mapping(
                raw["preparation"]
            ),
        )


class ContextPublicationPolicyRegistration:
    def __init__(self, dispose) -> None:
        self._dispose = dispose
        self._disposed = False

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._dispose()


@dataclass(frozen=True, slots=True)
class ContextPublicationPolicySnapshot:
    scope: str
    entries: tuple[tuple[str, ContextPublicationPolicyProvider, int], ...]

    @property
    def generations(self) -> dict[str, int]:
        return {name: generation for name, _provider, generation in self.entries}


class ContextPublicationPolicyRegistry:
    """Profile-scoped, generation-owned policy registry."""

    def __init__(
        self,
        *,
        decision_timeout_seconds: float = 2.0,
        max_providers: int = 8,
        max_outstanding_decisions: int = 16,
    ) -> None:
        self.decision_timeout_seconds = float(decision_timeout_seconds)
        self.max_providers = int(max_providers)
        self.max_outstanding_decisions = int(max_outstanding_decisions)
        if (
            not math.isfinite(self.decision_timeout_seconds)
            or self.decision_timeout_seconds <= 0
            or self.max_providers <= 0
            or self.max_outstanding_decisions < self.max_providers
        ):
            raise ValueError("context publication policy limits are invalid")
        self._lock = threading.RLock()
        self._slots: dict[str, dict[str, tuple[Any, int]]] = {}
        self._next_generation = 0
        self._decision_capacity = threading.BoundedSemaphore(
            self.max_outstanding_decisions
        )

    def _generation(self) -> int:
        self._next_generation += 1
        return self._next_generation

    def register(
        self, provider: Any, *, scope: str
    ) -> ContextPublicationPolicyRegistration:
        name = getattr(provider, "name", None)
        evaluate = getattr(provider, "evaluate", None)
        if (
            not isinstance(provider, ContextPublicationPolicyProvider)
            or type(name) is not str  # noqa: E721
            or _PROVIDER_NAME_RE.fullmatch(name) is None
            or not callable(evaluate)
            or not _identifier(scope)
        ):
            raise TypeError("context publication policy provider is invalid")
        with self._lock:
            slots = self._slots.setdefault(scope, {})
            if name in slots:
                raise TypeError(
                    f"context publication policy provider already registered: {name}"
                )
            if len(slots) >= self.max_providers:
                raise TypeError("too many context publication policy providers")
            slots[name] = (provider, self._generation())

        def _dispose() -> None:
            with self._lock:
                slots = self._slots.get(scope)
                if not slots:
                    return
                current = slots.get(name)
                if current is None or current[0] is not provider:
                    return
                slots.pop(name, None)
                self._generation()
                if not slots:
                    self._slots.pop(scope, None)

        return ContextPublicationPolicyRegistration(_dispose)

    def current_generations(self, scope: str) -> dict[str, int]:
        with self._lock:
            return {
                name: generation
                for name, (_, generation) in self._slots.get(scope, {}).items()
            }

    def clear_for_tests(self) -> None:
        with self._lock:
            self._slots.clear()
            self._next_generation = 0

    @contextmanager
    def locked(self):
        with self._lock:
            yield

    def snapshot(self, scope: str) -> ContextPublicationPolicySnapshot:
        with self._lock:
            slots = self._slots.get(scope, {})
            if not slots:
                raise ContextPublicationError("policy_unavailable")
            return ContextPublicationPolicySnapshot(
                scope=scope,
                entries=tuple(
                    (name, provider, generation)
                    for name, (provider, generation) in sorted(slots.items())
                ),
            )

    def snapshot_matches_locked(
        self, snapshot: ContextPublicationPolicySnapshot
    ) -> bool:
        slots = self._slots.get(snapshot.scope, {})
        current = tuple(
            (name, provider, generation)
            for name, (provider, generation) in sorted(slots.items())
        )
        return current == snapshot.entries

    def evaluate_snapshot(
        self,
        snapshot: ContextPublicationPolicySnapshot,
        proposal: ContextPublicationProposal,
    ) -> float:
        acquired = 0
        for _entry in snapshot.entries:
            if not self._decision_capacity.acquire(blocking=False):
                for _ in range(acquired):
                    self._decision_capacity.release()
                raise ContextPublicationError("policy_capacity")
            acquired += 1

        results: queue.Queue[tuple[str, str, Any]] = queue.Queue(
            maxsize=len(snapshot.entries)
        )

        def _evaluate(
            name: str,
            provider: ContextPublicationPolicyProvider,
        ) -> None:
            try:
                results.put_nowait(("decision", name, provider.evaluate(proposal)))
            except BaseException as exc:
                results.put_nowait(("error", name, exc))
            finally:
                self._decision_capacity.release()

        for name, provider, _generation in snapshot.entries:
            threading.Thread(
                target=_evaluate,
                args=(name, provider),
                name=f"context-policy-{name}",
                daemon=True,
            ).start()

        deadline = time.monotonic() + self.decision_timeout_seconds
        ttls: list[float] = []
        for _ in snapshot.entries:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ContextPublicationError("policy_timeout")
            try:
                outcome, _name, value = results.get(timeout=remaining)
            except queue.Empty as exc:
                raise ContextPublicationError("policy_timeout") from exc
            if outcome == "error":
                raise ContextPublicationError("policy_error") from value
            decision = value
            if type(decision) is not ContextPublicationDecision:  # noqa: E721
                raise ContextPublicationError("policy_malformed")
            if type(decision.allow) is not bool:  # noqa: E721
                raise ContextPublicationError("policy_malformed")
            if (
                type(decision.proposal_sha256) is not str
                or _SHA256_RE.fullmatch(decision.proposal_sha256) is None
                or decision.proposal_sha256 != proposal.proposal_sha256
            ):
                raise ContextPublicationError("policy_conflict")
            if not decision.allow:
                raise ContextPublicationError("policy_denied")
            if decision.ttl_seconds is not None:
                ttl = decision.ttl_seconds
                if (
                    type(ttl) not in {int, float}
                    or isinstance(ttl, bool)
                    or not math.isfinite(float(ttl))
                    or float(ttl) <= 0
                ):
                    raise ContextPublicationError("policy_malformed")
                ttls.append(float(ttl))
        return min(ttls) if ttls else math.inf

    def current_generations_locked(self, scope: str) -> dict[str, int]:
        slots = self._slots.get(scope, {})
        return {name: generation for name, (_, generation) in slots.items()}

    def generations_match_locked(
        self, scope: str, expected: dict[str, int]
    ) -> bool:
        slots = self._slots.get(scope, {})
        current = {name: generation for name, (_, generation) in slots.items()}
        return current == expected


_CONTEXT_PUBLICATION_POLICY_REGISTRY = ContextPublicationPolicyRegistry()


def get_context_publication_policy_registry() -> ContextPublicationPolicyRegistry:
    _ensure_context_publication_process_epoch()
    return _CONTEXT_PUBLICATION_POLICY_REGISTRY


class ContextPublicationAuthorizationService:
    """Durable at-most-once authorization issuer and consumer."""

    def __init__(
        self,
        state_db: Any,
        *,
        registry: ContextPublicationPolicyRegistry,
        clock=time.time,
        token_factory=None,
        process_id: str | None = None,
        max_ttl_seconds: float = 30.0,
        max_pending: int = 128,
        max_preparation_age_seconds: float = 10.0,
        max_future_skew_seconds: float = 2.0,
        policy_scope_key: str | None = None,
    ) -> None:
        if hasattr(state_db, "create_context_publication_acceptance"):
            self.state_db = state_db
        else:
            from hermes_state import SessionDB

            self.state_db = SessionDB(Path(state_db))
        self.registry = registry
        self.clock = clock
        self.token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        self.process_id = process_id or secrets.token_urlsafe(18)
        self.max_ttl_seconds = float(max_ttl_seconds)
        self.max_pending = int(max_pending)
        self.max_preparation_age_seconds = float(max_preparation_age_seconds)
        self.max_future_skew_seconds = float(max_future_skew_seconds)
        self.policy_scope_key = policy_scope_key
        if (
            not _identifier(self.process_id)
            or not math.isfinite(self.max_ttl_seconds)
            or self.max_ttl_seconds <= 0
            or self.max_pending <= 0
            or not math.isfinite(self.max_preparation_age_seconds)
            or self.max_preparation_age_seconds <= 0
            or not math.isfinite(self.max_future_skew_seconds)
            or self.max_future_skew_seconds < 0
            or (
                self.policy_scope_key is not None
                and (
                    type(self.policy_scope_key) is not str
                    or not self.policy_scope_key
                    or len(self.policy_scope_key) > 4096
                )
            )
        ):
            raise ValueError("context publication service configuration is invalid")

    @staticmethod
    def _token_hash(token: Any) -> str:
        if type(token) is not str or not 16 <= len(token) <= 256:  # noqa: E721
            raise ContextPublicationError("authorization_invalid")
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def authorize(
        self,
        preparation: BrowserPublicationPreparation,
        host: HostPublicationBinding,
    ) -> ContextPublicationAcceptance:
        proposal = ContextPublicationProposal.create(
            preparation,
            host,
            self.process_id,
        )
        now = float(self.clock())
        if not math.isfinite(now):
            raise ContextPublicationError("clock_invalid")
        observed_at = preparation.observed_at / 1000.0
        if (
            observed_at <= now - self.max_preparation_age_seconds
            or observed_at > now + self.max_future_skew_seconds
        ):
            raise ContextPublicationError("preparation_stale")
        policy_scope = self.policy_scope_key or host.profile_name
        snapshot = self.registry.snapshot(policy_scope)
        provider_ttl = self.registry.evaluate_snapshot(snapshot, proposal)
        ttl = min(self.max_ttl_seconds, provider_ttl)
        expires_at = now + ttl
        with self.registry.locked():
            if not self.registry.snapshot_matches_locked(snapshot):
                raise ContextPublicationError("policy_lifecycle_drift")
            generations = snapshot.generations
            generations_json = json.dumps(
                generations,
                sort_keys=True,
                separators=(",", ":"),
            )
            for _attempt in range(4):
                token = self.token_factory()
                token_hash = self._token_hash(token)
                try:
                    self.state_db.create_context_publication_acceptance(
                        token_hash=token_hash,
                        profile_name=host.profile_name,
                        process_id=self.process_id,
                        session_root_id=host.session_root_id,
                        session_tip_id=host.session_tip_id,
                        session_generation=host.session_generation,
                        payload_sha256=preparation.payload_sha256,
                        payload_byte_length=preparation.payload_byte_length,
                        proposal_sha256=proposal.proposal_sha256,
                        provider_generations_json=generations_json,
                        issued_at=now,
                        expires_at=expires_at,
                        max_pending=self.max_pending,
                    )
                except sqlite3.IntegrityError:
                    continue
                except ValueError as exc:
                    raise ContextPublicationError(str(exc)) from exc
                return ContextPublicationAcceptance(
                    token=token,
                    expires_at=expires_at,
                )
            raise ContextPublicationError("authorization_token_collision")

    def consume(
        self,
        token: str,
        preparation: BrowserPublicationPreparation,
        content: Any,
        host: HostPublicationBinding,
    ) -> Any:
        publication = canonicalize_context_publication(content)
        proposal = ContextPublicationProposal.create(
            preparation,
            host,
            self.process_id,
        )
        token_hash = self._token_hash(token)
        now = float(self.clock())
        if not math.isfinite(now):
            raise ContextPublicationError("clock_invalid")
        policy_scope = self.policy_scope_key or host.profile_name
        with self.registry.locked():
            generations_json = json.dumps(
                self.registry.current_generations_locked(policy_scope),
                sort_keys=True,
                separators=(",", ":"),
            )
            outcome = self.state_db.consume_context_publication_acceptance(
                token_hash=token_hash,
                profile_name=host.profile_name,
                process_id=self.process_id,
                session_root_id=host.session_root_id,
                session_tip_id=host.session_tip_id,
                session_generation=host.session_generation,
                payload_sha256=publication.payload_sha256,
                payload_byte_length=publication.payload_byte_length,
                proposal_sha256=proposal.proposal_sha256,
                provider_generations_json=generations_json,
                binding_matches=(
                    publication.protocol == preparation.envelope_protocol
                    and publication.payload_sha256 == preparation.payload_sha256
                    and publication.payload_byte_length
                    == preparation.payload_byte_length
                    and publication.content_kind == preparation.content_kind
                    and publication.image_count == preparation.image_count
                ),
                now=now,
            )
        if outcome != "consumed":
            raise ContextPublicationError(outcome)
        return publication.content


_CONTEXT_PUBLICATION_PROCESS_PID = os.getpid()
_CONTEXT_PUBLICATION_PROCESS_ID = secrets.token_urlsafe(18)
_CONTEXT_PUBLICATION_SERVICES: dict[str, ContextPublicationAuthorizationService] = {}
_CONTEXT_PUBLICATION_SERVICES_LOCK = threading.Lock()


def _reset_context_publication_after_fork() -> None:
    global _CONTEXT_PUBLICATION_PROCESS_PID
    global _CONTEXT_PUBLICATION_PROCESS_ID
    global _CONTEXT_PUBLICATION_POLICY_REGISTRY
    global _CONTEXT_PUBLICATION_SERVICES
    global _CONTEXT_PUBLICATION_SERVICES_LOCK

    _CONTEXT_PUBLICATION_PROCESS_PID = os.getpid()
    _CONTEXT_PUBLICATION_PROCESS_ID = secrets.token_urlsafe(18)
    _CONTEXT_PUBLICATION_POLICY_REGISTRY = ContextPublicationPolicyRegistry()
    _CONTEXT_PUBLICATION_SERVICES = {}
    _CONTEXT_PUBLICATION_SERVICES_LOCK = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_context_publication_after_fork)


def _ensure_context_publication_process_epoch() -> None:
    if os.getpid() != _CONTEXT_PUBLICATION_PROCESS_PID:
        _reset_context_publication_after_fork()


def get_context_publication_authorization_service(
    state_db: Any | None = None,
) -> ContextPublicationAuthorizationService:
    """Return the process-stable service for the active Hermes profile."""
    from hermes_constants import get_hermes_home, hermes_home_key

    _ensure_context_publication_process_epoch()
    home = get_hermes_home()
    path = str((home / "state.db").resolve())
    with _CONTEXT_PUBLICATION_SERVICES_LOCK:
        service = _CONTEXT_PUBLICATION_SERVICES.get(path)
        if service is None:
            service = ContextPublicationAuthorizationService(
                state_db or path,
                registry=get_context_publication_policy_registry(),
                process_id=_CONTEXT_PUBLICATION_PROCESS_ID,
                policy_scope_key=hermes_home_key(home),
            )
            _CONTEXT_PUBLICATION_SERVICES[path] = service
        return service


def consume_context_publication_for_turn(
    agent: Any,
    content: Any,
    authorization: ContextPublicationAuthorizationRequest,
) -> Any:
    """Consume acceptance after host session resolution, before turn setup."""
    from hermes_cli.profiles import get_active_profile_name

    tip = str(getattr(agent, "session_id", "") or "")
    root = str(agent._conversation_root_id() or "")
    generation = agent._session_db.get_context_publication_session_generation(tip)
    if not generation:
        raise ContextPublicationError("authorization_session_drift")
    host = HostPublicationBinding(
        profile_name=get_active_profile_name(),
        session_root_id=root,
        session_tip_id=tip,
        session_generation=generation,
    )
    return get_context_publication_authorization_service(agent._session_db).consume(
        authorization.token,
        authorization.preparation,
        content,
        host,
    )
