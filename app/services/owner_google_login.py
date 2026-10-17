"""Strict Google Identity Services ID-token verification for the pinned owner.

The public key source and algorithm are fixed here. Callers provide only the token,
expected client id, nonce and pinned subject; token fields never select a URL or algorithm.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import Message
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

import jwt
from jwt import InvalidTokenError, PyJWTError

GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = frozenset({"accounts.google.com", "https://accounts.google.com"})
MAX_TOKEN_BYTES = 6144
MAX_JWKS_BYTES = 64 * 1024
JWKS_TIMEOUT_SECONDS = 3.0
MAX_JWKS_CACHE_SECONDS = 3600
DEFAULT_JWKS_CACHE_SECONDS = 300
UNKNOWN_KID_REFRESH_COOLDOWN_SECONDS = 60
MAX_TOKEN_AGE_SECONDS = 300
CLOCK_SKEW_SECONDS = 30
_KID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MAX_AGE = re.compile(r"(?:^|,)\s*max-age=(\d+)\s*(?:,|$)", re.IGNORECASE)


class GoogleOwnerLoginDenied(PermissionError):
    """The supplied Google token cannot authenticate the pinned owner."""


class GoogleJwksUnavailable(RuntimeError):
    """The fixed Google public-key source could not be used safely."""


@dataclass(frozen=True)
class VerifiedGoogleOwner:
    owner_id: str
    subject: str
    proof_hash: str
    authenticated_at: datetime


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _cache_seconds(headers: Message) -> int:
    raw = headers.get("Cache-Control", "")
    match = _MAX_AGE.search(raw)
    if match is None:
        return DEFAULT_JWKS_CACHE_SECONDS
    return max(1, min(int(match.group(1)), MAX_JWKS_CACHE_SECONDS))


def _fetch_google_jwks() -> tuple[dict[str, Any], int]:
    """Fetch only Google's fixed JWKS URL with TLS verification and hard bounds."""
    request = Request(
        GOOGLE_JWKS_URL,
        headers={"Accept": "application/json", "User-Agent": "mia-owner-auth/1"},
        method="GET",
    )
    try:
        with build_opener(_NoRedirect()).open(request, timeout=JWKS_TIMEOUT_SECONDS) as response:
            if response.status != 200 or response.geturl() != GOOGLE_JWKS_URL:
                raise GoogleJwksUnavailable("google keys unavailable")
            raw = response.read(MAX_JWKS_BYTES + 1)
            if len(raw) > MAX_JWKS_BYTES:
                raise GoogleJwksUnavailable("google keys unavailable")
            cache_seconds = _cache_seconds(response.headers)
    except (HTTPError, URLError, OSError, TimeoutError) as exc:
        raise GoogleJwksUnavailable("google keys unavailable") from exc
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GoogleJwksUnavailable("google keys unavailable") from exc
    if not isinstance(document, dict):
        raise GoogleJwksUnavailable("google keys unavailable")
    return document, cache_seconds


class GoogleJwksCache:
    """Small process cache with a single forced refresh for an unknown key id."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._keys: dict[str, dict[str, Any]] = {}
        self._expires_at = 0.0
        self._last_unknown_refresh_at = float("-inf")

    @staticmethod
    def _validated_keys(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
        raw_keys = document.get("keys")
        if not isinstance(raw_keys, list) or len(raw_keys) > 16:
            raise GoogleJwksUnavailable("google keys unavailable")
        keys: dict[str, dict[str, Any]] = {}
        for item in raw_keys:
            if not isinstance(item, dict):
                raise GoogleJwksUnavailable("google keys unavailable")
            kid = item.get("kid")
            if (
                not isinstance(kid, str)
                or not _KID.fullmatch(kid)
                or item.get("kty") != "RSA"
                or item.get("alg") not in {None, "RS256"}
                or item.get("use") not in {None, "sig"}
                or not isinstance(item.get("n"), str)
                or not isinstance(item.get("e"), str)
                or kid in keys
            ):
                raise GoogleJwksUnavailable("google keys unavailable")
            keys[kid] = item
        if not keys:
            raise GoogleJwksUnavailable("google keys unavailable")
        return keys

    def _refresh(self, now: float) -> None:
        document, cache_seconds = _fetch_google_jwks()
        self._keys = self._validated_keys(document)
        self._expires_at = now + cache_seconds

    def get(self, kid: str) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            refreshed = False
            if now >= self._expires_at:
                self._refresh(now)
                refreshed = True
            key = self._keys.get(kid)
            if key is None and refreshed:
                self._last_unknown_refresh_at = now
            if (
                key is None
                and not refreshed
                and now - self._last_unknown_refresh_at >= UNKNOWN_KID_REFRESH_COOLDOWN_SECONDS
            ):
                # One bounded rotation refresh for a previously unseen key. The lock
                # is the single-flight boundary; the cooldown prevents kid-based fetch spam.
                self._last_unknown_refresh_at = now
                self._refresh(now)
                key = self._keys.get(kid)
            if key is None:
                raise GoogleOwnerLoginDenied("owner authentication required")
            return key


_JWKS = GoogleJwksCache()


def verify_google_owner_token(
    id_token: str,
    *,
    client_id: str,
    expected_subject: str,
    owner_id: str,
    nonce: str,
    now: int | None = None,
) -> VerifiedGoogleOwner:
    """Verify a Google-signed token and bind one pinned subject to one numeric owner."""
    if (
        not id_token
        or len(id_token.encode()) > MAX_TOKEN_BYTES
        or not client_id
        or not expected_subject
        or not owner_id.isascii()
        or not owner_id.isdigit()
        or not nonce
    ):
        raise GoogleOwnerLoginDenied("owner authentication required")
    try:
        header = jwt.get_unverified_header(id_token)
        kid = header.get("kid")
        if header.get("alg") != "RS256" or not isinstance(kid, str) or not _KID.fullmatch(kid):
            raise GoogleOwnerLoginDenied("owner authentication required")
        try:
            key = jwt.algorithms.RSAAlgorithm.from_jwk(_JWKS.get(kid))
        except GoogleOwnerLoginDenied:
            raise
        except PyJWTError as exc:
            raise GoogleJwksUnavailable("google keys unavailable") from exc
        claims = jwt.decode(
            id_token,
            key=key,
            algorithms=["RS256"],
            audience=client_id,
            issuer=list(GOOGLE_ISSUERS),
            leeway=CLOCK_SKEW_SECONDS,
            options={"require": ["iss", "sub", "aud", "exp", "iat", "nonce"]},
        )
        moment = int(time.time()) if now is None else now
        issued_at = int(claims["iat"])
        if issued_at > moment + CLOCK_SKEW_SECONDS or moment - issued_at > MAX_TOKEN_AGE_SECONDS:
            raise GoogleOwnerLoginDenied("owner authentication required")
        if claims["sub"] != expected_subject or claims["nonce"] != nonce:
            raise GoogleOwnerLoginDenied("owner authentication required")
        authorized_party = claims.get("azp")
        audience_claim = claims["aud"]
        if (
            isinstance(audience_claim, list)
            and len(audience_claim) > 1
            and authorized_party is None
        ):
            raise GoogleOwnerLoginDenied("owner authentication required")
        if authorized_party is not None and authorized_party != client_id:
            raise GoogleOwnerLoginDenied("owner authentication required")
    except GoogleOwnerLoginDenied:
        raise
    except (InvalidTokenError, KeyError, TypeError, ValueError) as exc:
        raise GoogleOwnerLoginDenied("owner authentication required") from exc
    proof_hash = hashlib.sha256(b"owner-os:google-proof:" + id_token.encode()).hexdigest()
    return VerifiedGoogleOwner(
        owner_id=owner_id,
        subject=expected_subject,
        proof_hash=proof_hash,
        authenticated_at=datetime.fromtimestamp(issued_at, tz=UTC),
    )
