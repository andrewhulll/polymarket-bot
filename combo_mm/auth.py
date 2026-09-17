"""Authentication structure for the Polymarket US gRPC API.

Contract (per https://docs.polymarket.us/grpc-api/overview):

- Private Key JWT: sign a JWT with the firm's RSA private key (RS256),
  exchange it at Auth0 for an access token. Preprod:
  ``pmx-preprod.us.auth0.com``; prod: ``pmx-prod.us.auth0.com``. Onboarding
  provides public-key registration, ``client_id``, and ``audience``.
- The access token goes in the ``authorization`` gRPC metadata as
  ``Bearer <token>``. Tokens expire every 3 minutes -- this module
  auto-refreshes. Key rotation is supported: submit the new public key while
  the old one is still valid; both keys are accepted during the transition.
- Required scopes: ``read:orders`` (RFQ stream), ``read:dropcopy``
  (Drop Copy).

This module builds the STRUCTURE now; no live credentials exist, so every
network or cryptographic operation is stubbed and raises
:class:`CredentialsNotConfigured` with a clear message. Going live needs
``cryptography`` (RS256 signing) and ``grpcio`` + generated
``polymarket.v1`` stubs -- none of which are runtime dependencies of this
package (stdlib only).

SECURITY: private key material is NEVER written to source, logs, or
fixtures. The key is loaded from a path (or secret-store reference) at
runtime, held in memory only, and never logged.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

__all__ = [
    "CredentialsNotConfigured",
    "AuthConfig",
    "ErrorAction",
    "GrpcError",
    "map_grpc_error",
    "call_with_auth_retry",
    "TokenProvider",
    # gRPC status codes we map (string names; avoids a grpcio dependency)
    "UNAUTHENTICATED",
    "PERMISSION_DENIED",
    "FAILED_PRECONDITION",
    "RESOURCE_EXHAUSTED",
    "UNAVAILABLE",
    "DEADLINE_EXCEEDED",
]

# gRPC status code names (kept as strings so this module stays stdlib-only).
UNAUTHENTICATED = "UNAUTHENTICATED"
PERMISSION_DENIED = "PERMISSION_DENIED"
FAILED_PRECONDITION = "FAILED_PRECONDITION"
RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"
UNAVAILABLE = "UNAVAILABLE"
DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"

#: Token lifetime per the contract (3 minutes); refresh happens earlier.
TOKEN_LIFETIME_S = 180.0
#: Refresh this far ahead of expiry so a token never dies mid-call.
REFRESH_SKEW_S = 30.0


class CredentialsNotConfigured(RuntimeError):
    """Raised when an auth operation needs live credentials that don't exist."""


@dataclass
class AuthConfig:
    """What Andrew must supply to go live (see README 'Going live')."""

    auth0_domain: str = ""      # e.g. pmx-preprod.us.auth0.com
    client_id: str = ""
    audience: str = ""
    # Path to the firm's RSA private key (PEM), or a secret-store reference
    # such as "vault:polymarket/private_key". NEVER inline key material here.
    private_key_path: str = ""
    key_id: str = "primary"     # identifies this key during rotation
    # (key_id -> key path); both keys are valid during a rotation transition.
    rotation_keys: Dict[str, str] = field(default_factory=dict)
    scopes: Tuple[str, ...] = ("read:orders", "read:dropcopy")

    def validate(self) -> "AuthConfig":
        """Raise CredentialsNotConfigured listing exactly what is missing."""
        missing = [
            name for name, value in (
                ("auth0_domain", self.auth0_domain),
                ("client_id", self.client_id),
                ("audience", self.audience),
                ("private_key_path", self.private_key_path),
            )
            if not value
        ]
        if missing:
            raise CredentialsNotConfigured(
                "auth not configured; missing: " + ", ".join(missing)
                + ". See README 'Going live'."
            )
        return self

    def metadata(self, token: str) -> List[Tuple[str, str]]:
        """gRPC metadata pairs for an authenticated call."""
        return [("authorization", f"Bearer {token}")]


@dataclass(frozen=True)
class ErrorAction:
    """What the pipeline should do about a gRPC error."""

    action: str   # "refresh_and_retry_once" | "fatal" | "backoff" | "backoff_reconnect"
    reason: str


class GrpcError(Exception):
    """A gRPC call failure carrying its status code (stdlib stand-in).

    The live transport will raise the real ``grpc.RpcError``; this class
    lets the retry policy be unit-tested without grpcio.
    """

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = (code or "").upper()
        self.detail = detail


def map_grpc_error(code: str, detail: str = "") -> ErrorAction:
    """Map a gRPC status code to the pipeline's recovery action.

    - ``UNAUTHENTICATED``: token expired/invalid -> refresh the token and
      retry the call ONCE; a second failure is fatal (don't loop).
    - ``PERMISSION_DENIED``: missing scope (e.g. ``read:orders``) -> FATAL
      config error. Never retry-loop: it cannot self-heal.
    - ``FAILED_PRECONDITION``: RFQs blocked / participant setup not ready ->
      FATAL. Retrying is pointless until onboarding completes.
    - ``RESOURCE_EXHAUSTED``: stream-per-second limit -> backoff.
    - ``UNAVAILABLE`` / ``DEADLINE_EXCEEDED``: transient -> backoff + reconnect.
    """
    code = (code or "").upper()
    if code == UNAUTHENTICATED:
        return ErrorAction("refresh_and_retry_once",
                           f"token invalid/expired ({detail}); refresh and retry once")
    if code == PERMISSION_DENIED:
        return ErrorAction("fatal",
                           f"missing scope -- check read:orders/read:dropcopy ({detail})")
    if code == FAILED_PRECONDITION:
        return ErrorAction("fatal",
                           f"participant setup not ready / RFQs blocked ({detail})")
    if code == RESOURCE_EXHAUSTED:
        return ErrorAction("backoff",
                           f"rate limit hit, e.g. 1 stream/sec ({detail})")
    if code in (UNAVAILABLE, DEADLINE_EXCEEDED):
        return ErrorAction("backoff_reconnect",
                           f"transient transport failure ({detail})")
    return ErrorAction("backoff_reconnect", f"unmapped gRPC code {code} ({detail})")


def call_with_auth_retry(fn: Callable[[str], Any], provider: TokenProvider,
                         *, key_id: Optional[str] = None) -> Any:
    """Run ``fn(token)`` with the auth error policy applied.

    - First ``UNAUTHENTICATED``: invalidate the cached token, refresh, retry
      the call exactly ONCE. A second ``UNAUTHENTICATED`` propagates (never
      retry-loop: a persistently rejected token is a config/credential
      problem, not a transient one).
    - ``PERMISSION_DENIED`` / ``FAILED_PRECONDITION``: fatal immediately --
      no retry, no refresh; these cannot self-heal.
    - Any other ``GrpcError`` propagates to the caller's backoff/reconnect
      handling.
    """
    token = provider.get_token(key_id=key_id)
    try:
        return fn(token)
    except GrpcError as exc:
        action = map_grpc_error(exc.code, exc.detail)
        if action.action == "refresh_and_retry_once":
            log.warning("UNAUTHENTICATED: refreshing token and retrying once")
            provider.invalidate()
            return fn(provider.get_token(key_id=key_id))
        if action.action == "fatal":
            log.error("fatal gRPC error %s: %s", exc.code, action.reason)
        raise


class TokenProvider:
    """Caches the Auth0 access token and auto-refreshes it.

    The cryptographic and network operations are injected so this class is
    fully testable without credentials:

    - ``sign_jwt(key_id)`` -> signed Private-Key-JWT (str). The default stub
      raises :class:`CredentialsNotConfigured`.
    - ``exchange(signed_jwt)`` -> ``(access_token, expires_in_s)``. The
      default stub raises :class:`CredentialsNotConfigured`.

    Key rotation: pass ``key_id`` to select which registered key signs; both
    keys are valid during the transition, so callers can flip ``key_id`` to
    the new key as soon as onboarding confirms it.
    """

    def __init__(
        self,
        config: AuthConfig,
        *,
        sign_jwt: Optional[Callable[[str], str]] = None,
        exchange: Optional[Callable[[str], Tuple[str, float]]] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._config = config
        self._sign_jwt = sign_jwt or self._stub_sign
        self._exchange = exchange or self._stub_exchange
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._expires_at: float = 0.0

    # -- stubs (replaced by real implementations at go-live) -----------------
    @staticmethod
    def _stub_sign(key_id: str) -> str:
        raise CredentialsNotConfigured(
            "cannot sign Private Key JWT: no private key configured "
            f"(key_id={key_id}). Needs the 'cryptography' package and the "
            "firm's RSA private key. See README 'Going live'."
        )

    @staticmethod
    def _stub_exchange(signed_jwt: str) -> Tuple[str, float]:
        raise CredentialsNotConfigured(
            "cannot exchange JWT at Auth0: no auth0_domain/client_id/audience "
            "configured. See README 'Going live'."
        )

    # -- public API ----------------------------------------------------------
    def get_token(self, *, key_id: Optional[str] = None) -> str:
        """Return a valid Bearer token, refreshing if within the skew window."""
        with self._lock:
            now = self._clock()
            if self._token is not None and now < self._expires_at - REFRESH_SKEW_S:
                return self._token
            self._config.validate()
            kid = key_id or self._config.key_id
            log.info("refreshing Auth0 access token (key_id=%s)", kid)
            signed = self._sign_jwt(kid)
            token, lifetime_s = self._exchange(signed)
            # Never trust the server lifetime beyond the contract's 3 minutes.
            lifetime_s = min(float(lifetime_s or 0.0), TOKEN_LIFETIME_S)
            if not token or lifetime_s <= 0:
                raise CredentialsNotConfigured(
                    "Auth0 token exchange returned no usable token"
                )
            self._token = token
            self._expires_at = now + lifetime_s
            return token

    def invalidate(self) -> None:
        """Drop the cached token (e.g. after an UNAUTHENTICATED error)."""
        with self._lock:
            self._token = None
            self._expires_at = 0.0
