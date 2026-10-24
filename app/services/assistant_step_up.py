"""The step-up token that lets the owner issue an assistant key (V2_PG_CONTRACTS.md §17.1).

Issuing a key needs proof of a fresh Google re-authentication (FORK-STEP-UP). ``POST
/v1/owner/auth/step-up`` verifies the Google token (``owner_google_login.verify_step_up_id_token``)
and hands back a short token; ``POST /assistant-credentials`` then presents it in ``X-Step-Up``.

The token is ``su1.<auth_time>.<exp>.<mac>`` where ``mac`` is HMAC-SHA256 under
``os_step_up_secret`` over ``assaf-os-step-up.v1|<session_hash>|<auth_time>|<exp>``. The session
hash is the MAC input and is never in the token, so a token minted for one browser session is
useless in any other, and it is valid for ten minutes from the moment it was minted.

Nothing here logs a token, a session hash or the secret; a refusal is the ``LedgerError`` code
alone (``step_up_required``). A missing or short secret is ``ledger_unavailable`` (it is "required
at use", §3.3).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from dataclasses import dataclass
from typing import Any

from app.services.os_ledger import api

STEP_UP_TTL_SECONDS = 600
MIN_SECRET_BYTES = 32
_VERSION = "su1"
_DOMAIN = "assaf-os-step-up.v1"
_TOKEN = re.compile(r"su1\.([0-9]{1,12})\.([0-9]{1,12})\.([0-9a-f]{64})")
_SESSION_HASH = re.compile(r"[0-9a-f]{64}")
_LOG = logging.getLogger("mia.os_step_up")


@dataclass(frozen=True)
class StepUpToken:
    token: str = ""
    expires_at: int = 0  # epoch seconds

    def __repr__(self) -> str:  # the token is a credential
        return f"StepUpToken(expires_at={self.expires_at})"


def _key(settings: Any) -> bytes:
    raw = getattr(settings, "os_step_up_secret", "")
    secret = raw.encode("utf-8") if isinstance(raw, str) else b""
    if len(secret) < MIN_SECRET_BYTES:
        _LOG.warning("os_step_up op=secret outcome=refused reason=step_up_secret_unset")
        raise api.LedgerError("ledger_unavailable", reason="step_up_secret_unset")
    return secret


def _mac(secret: bytes, session_hash: str, auth_time: int, exp: int) -> str:
    message = f"{_DOMAIN}|{session_hash}|{auth_time}|{exp}".encode()
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def _refuse(reason: str) -> api.LedgerError:
    _LOG.info("os_step_up op=verify outcome=step_up_required reason=%s", reason)
    return api.LedgerError("step_up_required", reason=reason)


def mint(*, session_hash: str, auth_time: int, now: int, settings: Any) -> StepUpToken:
    """A token for ``session_hash``, valid from ``now`` for ten minutes."""
    secret = _key(settings)
    if (type(session_hash) is not str or _SESSION_HASH.fullmatch(session_hash) is None
            or type(auth_time) is not int or type(now) is not int or auth_time < 0 or now < 0):
        raise api.LedgerError("ledger_unavailable", reason="step_up_input_invalid")
    exp = now + STEP_UP_TTL_SECONDS
    return StepUpToken(f"{_VERSION}.{auth_time}.{exp}.{_mac(secret, session_hash, auth_time, exp)}",
                       exp)


def verify(token: Any, *, session_hash: str, now: int, settings: Any) -> int:
    """The ``auth_time`` the token proves, or ``step_up_required``.

    The secret is read first, so an unconfigured deployment is ``ledger_unavailable`` for every
    token, never a quiet "invalid".
    """
    secret = _key(settings)
    match = _TOKEN.fullmatch(token) if type(token) is str else None
    if match is None:
        raise _refuse("step_up_malformed")
    auth_time, exp, supplied = int(match.group(1)), int(match.group(2)), match.group(3)
    if type(session_hash) is not str or _SESSION_HASH.fullmatch(session_hash) is None:
        raise _refuse("step_up_session_invalid")
    if not hmac.compare_digest(supplied, _mac(secret, session_hash, auth_time, exp)):
        raise _refuse("step_up_mac")
    if not now < exp:
        raise _refuse("step_up_expired")
    return auth_time
