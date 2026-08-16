"""Route-agnostic non-interactive (bearer-token) auth seam for the dashboard.

This is the generic API-token capability (decisions.md Q-C): a reusable seam
that ANY service-to-service / machine-credential provider plugs into, NOT a
drain-specific hook. The drain bearer-secret plugin is merely the first
consumer.

How it fits the existing auth framework:

  * The interactive gate (``gated_auth_middleware``) authenticates a human
    via a session cookie on every non-public route. A service caller has no
    cookie — it presents a bearer token in the ``Authorization`` header on a
    single request. That is what this seam verifies.

  * A route opts in by registering its exact path, owning provider, and optional
    required scopes via :func:`register_token_route`. Only registered paths are
    token-authable, and credentials from one provider cannot authenticate a
    different provider's route.

  * :func:`token_auth_middleware` runs OUTERMOST (installed last in
    ``web_server.py``). For a token route it fully owns the auth decision:
    authenticate via the stacked token providers, attach the verified
    :class:`~hermes_cli.dashboard_auth.base.TokenPrincipal` to
    ``request.state.token_principal`` + set ``request.state.token_authenticated``,
    and pass through; otherwise reject (401 unauthenticated, or 503 when a
    provider's backing store was unreachable). The downstream cookie/session
    gates honour ``token_authenticated`` and skip enforcement, so a
    token-authed service request is never bounced to ``/login``.

  * Fails closed: a token route with no registered owning provider, no token,
    an unrecognised token, or insufficient scopes gets rejected — never an open
    pass-through.

Provider stacking remains available to direct callers of ``authenticate_token``;
the middleware filters the stack to the provider that owns the matched route.
A provider outage surfaces as 503 only when that owning provider cannot decide.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable, Optional, Tuple

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from hermes_constants import hermes_home_key

from hermes_cli.dashboard_auth import list_token_providers
from hermes_cli.dashboard_auth.audit import AuditEvent, audit_log
from hermes_cli.dashboard_auth.base import ProviderError, TokenPrincipal

_log = logging.getLogger(__name__)

@dataclass(frozen=True)
class TokenRoutePolicy:
    """Exact machine-auth ownership and authorization requirements for a route."""

    provider: str
    required_scopes: frozenset[str] = frozenset()


@dataclass
class TokenRouteRegistration:
    """Disposable ownership of one profile-scoped token-route policy."""

    scope: str
    path: str
    _registration_id: object = field(repr=False)
    _disposed: bool = field(default=False, init=False, repr=False)

    @property
    def active(self) -> bool:
        return not self._disposed

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        with _lock:
            key = (self.scope, self.path)
            registrations = _token_routes.get(key)
            if registrations is None:
                return
            registrations.pop(self._registration_id, None)
            if not registrations:
                _token_routes.pop(key, None)


# Route policies are profile-scoped and reference-counted by registration handle.
# A path with differing live policies is poisoned and denied until the conflict
# is removed; no registration can silently retain or steal first-writer authority.
_token_routes: dict[tuple[str, str], dict[object, TokenRoutePolicy]] = {}
_lock = threading.Lock()


def register_token_route(
    path: str,
    *,
    provider: Optional[str] = None,
    required_scopes: Iterable[str] = (),
    scope: Optional[str] = None,
) -> Optional[TokenRouteRegistration]:
    """Register a profile-scoped exact token route and return its cleanup handle.

    Every active token route must name its owning token provider. Calls using the
    original one-argument API are accepted for source compatibility but register
    nothing, leaving the route under ordinary interactive authentication until
    the caller migrates to an explicit provider. Required scopes are
    canonicalized as a set. Conflicting policies make the route fail closed
    until the conflicting registration is disposed.
    """
    if provider is None:
        _log.warning(
            "dashboard-auth: ignored unowned token route %r; pass provider= explicitly",
            path,
        )
        return None
    if isinstance(required_scopes, (str, bytes)):
        raise ValueError("invalid token route policy")
    try:
        scopes = frozenset(required_scopes)
    except TypeError as exc:
        raise ValueError("invalid token route policy") from exc
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or "?" in path
        or "#" in path
        or not isinstance(provider, str)
        or not provider
        or any(not isinstance(item, str) or not item for item in scopes)
        or not isinstance(scope, (str, type(None)))
        or scope == ""
    ):
        raise ValueError("invalid token route policy")
    route_scope = scope or hermes_home_key()
    registration_id = object()
    policy = TokenRoutePolicy(provider=provider, required_scopes=scopes)
    with _lock:
        _token_routes.setdefault((route_scope, path), {})[registration_id] = policy
    return TokenRouteRegistration(route_scope, path, registration_id)


def _token_route_policy(
    path: str, *, scope: Optional[str] = None
) -> tuple[Optional[TokenRoutePolicy], bool]:
    with _lock:
        registrations = _token_routes.get((scope or hermes_home_key(), path), {})
        policies = set(registrations.values())
    if not policies:
        return None, False
    if len(policies) != 1:
        return None, True
    return next(iter(policies)), False


def is_token_route(path: str, *, scope: Optional[str] = None) -> bool:
    """True if ``path`` has any live token-route registration in the profile."""
    policy, conflict = _token_route_policy(path, scope=scope)
    return policy is not None or conflict


def clear_token_routes() -> None:
    """Test-only: drop all registered token routes."""
    with _lock:
        _token_routes.clear()


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


def extract_bearer_token(request: Request) -> str:
    """Return the bearer token from the ``Authorization`` header, or "".

    Accepts ``<scheme> <token>`` where scheme is "bearer" (case-insensitive).
    Returns an empty string for a missing/malformed header or a non-bearer
    scheme — the caller treats "" as "no token presented".
    """
    auth = request.headers.get("authorization", "")
    parts = auth.split(" ", 1)
    if len(parts) == 2 and parts[0].strip().lower() == "bearer":
        return parts[1].strip()
    return ""


def authenticate_token(
    request: Request,
    *,
    provider_name: Optional[str] = None,
) -> Tuple[Optional[TokenPrincipal], Optional[str]]:
    """Try every token provider against the request's bearer token.

    Returns ``(principal, unreachable_provider_name)``:
      * ``(TokenPrincipal, None)`` — a provider recognised and accepted the token.
      * ``(None, None)`` — no token, or no provider recognised it (reject 401).
      * ``(None, name)`` — no provider accepted it AND at least one provider's
        backing store was unreachable (the caller surfaces 503, not 401, so a
        transient outage doesn't read as "bad credentials").

    Never raises: a provider ``ProviderError`` is caught and remembered.
    """
    token = extract_bearer_token(request)
    if not token:
        return None, None
    unreachable: Optional[str] = None
    for provider in list_token_providers():
        if provider_name is not None and provider.name != provider_name:
            continue
        try:
            principal = provider.verify_token(token=token)
        except ProviderError as e:
            _log.warning(
                "dashboard-auth: token provider %r unreachable during verify: %s",
                provider.name, e,
            )
            if unreachable is None:
                unreachable = provider.name
            continue
        except Exception as e:  # noqa: BLE001 — a buggy provider must not 500 the gate
            _log.warning(
                "dashboard-auth: token provider %r raised during verify: %s",
                provider.name, e,
            )
            continue
        if principal is not None:
            if principal.provider != provider.name:
                _log.warning(
                    "dashboard-auth: token provider %r returned principal for %r",
                    provider.name,
                    principal.provider,
                )
                continue
            return principal, None
    return None, unreachable


async def token_auth_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Outermost auth seam for token-authable routes.

    No-op pass-through for any path not registered via
    :func:`register_token_route`. For a registered path, token auth is the
    only accepted scheme:

      * valid token  → attach principal + ``token_authenticated`` flag, pass through.
      * unreachable  → 503 (provider backing store down; not "bad credentials").
      * otherwise    → 401 unauthenticated.

    Runs before the cookie/session gates (installed last in ``web_server.py``).
    The cookie gates honour ``request.state.token_authenticated`` and skip
    enforcement, so a token-authed request is never redirected to ``/login``.
    """
    path = request.url.path
    policy, conflict = _token_route_policy(path)
    if conflict:
        audit_log(
            AuditEvent.TOKEN_AUTH_FAILURE,
            reason="route_policy_conflict",
            path=path,
            ip=_client_ip(request),
        )
        return JSONResponse(
            {"error": "unavailable", "detail": "Token route unavailable"},
            status_code=503,
        )
    if policy is None:
        return await call_next(request)

    principal, unreachable = authenticate_token(
        request, provider_name=policy.provider
    )
    if principal is not None:
        if not set(policy.required_scopes).issubset(principal.scopes):
            audit_log(
                AuditEvent.TOKEN_AUTH_FAILURE,
                provider=principal.provider,
                reason="insufficient_scope",
                path=path,
                ip=_client_ip(request),
            )
            return JSONResponse(
                {"error": "forbidden", "detail": "Forbidden"},
                status_code=403,
            )
        request.state.token_principal = principal
        request.state.token_authenticated = True
        return await call_next(request)

    if unreachable:
        audit_log(
            AuditEvent.TOKEN_AUTH_FAILURE,
            provider=unreachable,
            reason="provider_unreachable",
            path=path,
            ip=_client_ip(request),
        )
        return JSONResponse(
            {"detail": f"Auth provider {unreachable!r} unreachable"},
            status_code=503,
        )

    audit_log(
        AuditEvent.TOKEN_AUTH_FAILURE,
        reason="no_provider_recognises_token",
        path=path,
        ip=_client_ip(request),
    )
    return JSONResponse(
        {"error": "unauthenticated", "detail": "Unauthorized"},
        status_code=401,
    )
