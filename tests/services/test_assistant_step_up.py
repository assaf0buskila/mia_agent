"""V2-37 step-up (V2_PG_CONTRACTS.md §17.1, FORK-STEP-UP): the Google proof and the HMAC token.

* ``owner_google_login.verify_step_up_id_token`` reuses the strict Google verification and then
  reads ``auth_time`` from the very token it verified. It refuses an ``auth_time`` older than
  600 s (a silent SSO ``iat`` does not prove a re-authentication) and returns the ``auth_time``.
  Real RS256 tokens are signed with a throw-away key and the JWKS cache is replaced, so the whole
  verification path runs.
* ``assistant_step_up`` mints and checks the 10-minute token: HMAC(``os_step_up_secret``) over
  ``session_hash|auth_time|exp``, so it is valid for the session it was minted for and no other.
"""

from __future__ import annotations

import json
import time

import jwt
import pytest
from app.core.config import MiaEnv, Settings
from app.services import assistant_step_up as step_up
from app.services import owner_google_login as google
from app.services.os_ledger import api
from cryptography.hazmat.primitives.asymmetric import rsa

OWNER = "123"
CLIENT = "client.example"
SUBJECT = "109876543210"
NONCE = "n" * 64
NOW = int(time.time())  # PyJWT checks exp/iat against the real clock
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
JWK = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key())) | {"kid": "k1"}


class Jwks:
    def get(self, kid):
        if kid != "k1":
            raise google.GoogleOwnerLoginDenied("owner authentication required")
        return JWK


@pytest.fixture(autouse=True)
def jwks(monkeypatch):
    monkeypatch.setattr(google, "_JWKS", Jwks())


def settings(**over) -> Settings:
    values = dict(
        _env_file=None, env=MiaEnv.TEST, telegram_owner_user_ids=OWNER,
        owner_os_google_enabled=True, owner_os_google_client_id=CLIENT,
        owner_os_google_subject=SUBJECT, owner_os_google_owner_id=OWNER,
        os_step_up_secret="s" * 32)
    values.update(over)
    return Settings(**values)


def token(*, auth_time=NOW - 5, nonce=NONCE, drop=(), **over) -> str:
    claims = {"iss": "https://accounts.google.com", "sub": SUBJECT, "aud": CLIENT,
              "iat": NOW - 5, "exp": NOW + 300, "nonce": nonce, "auth_time": auth_time}
    claims.update(over)
    for name in drop:
        claims.pop(name, None)
    return jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": "k1"})


# ---------------------------------------------------------------------------------------------
# verify_step_up_id_token


def test_a_fresh_reauthentication_returns_its_auth_time():
    assert google.verify_step_up_id_token(
        token(auth_time=NOW - 5), settings=settings(), now=NOW, nonce=NONCE) == NOW - 5


def test_the_token_own_nonce_is_used_when_no_nonce_is_given():
    assert google.verify_step_up_id_token(
        token(nonce="own"), settings=settings(), now=NOW) == NOW - 5


@pytest.mark.parametrize("age,ok", [(0, True), (599, True), (600, True), (601, False),
                                    (3600, False)])
def test_an_auth_time_older_than_600_seconds_is_refused(age, ok):
    # iat stays fresh (a silent SSO token is fresh too); only auth_time says when he really signed in
    supplied = token(auth_time=NOW - age)
    if ok:
        assert google.verify_step_up_id_token(supplied, settings=settings(), now=NOW,
                                              nonce=NONCE) == NOW - age
    else:
        with pytest.raises(google.GoogleOwnerLoginDenied):
            google.verify_step_up_id_token(supplied, settings=settings(), now=NOW, nonce=NONCE)


@pytest.mark.parametrize("bad", ["missing", "string", "float", "bool", "future"])
def test_a_missing_or_malformed_or_future_auth_time_is_refused(bad):
    kwargs = {
        "missing": dict(drop=("auth_time",)), "string": dict(auth_time=str(NOW - 5)),
        "float": dict(auth_time=float(NOW - 5) + 0.5), "bool": dict(auth_time=True),
        "future": dict(auth_time=NOW + 31),
    }[bad]
    with pytest.raises(google.GoogleOwnerLoginDenied):
        google.verify_step_up_id_token(token(**kwargs), settings=settings(), now=NOW,
                                       nonce=NONCE)


def test_a_wrong_nonce_wrong_subject_wrong_audience_or_bad_signature_is_refused():
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode({"iss": "https://accounts.google.com", "sub": SUBJECT, "aud": CLIENT,
                         "iat": NOW - 5, "exp": NOW + 300, "nonce": NONCE,
                         "auth_time": NOW - 5}, other, algorithm="RS256", headers={"kid": "k1"})
    for supplied, nonce in ((token(), "different"), (token(sub="999"), NONCE),
                            (token(aud="other.example"), NONCE), (forged, NONCE),
                            ("not-a-jwt", NONCE), ("", NONCE)):
        with pytest.raises(google.GoogleOwnerLoginDenied):
            google.verify_step_up_id_token(supplied, settings=settings(), now=NOW, nonce=nonce)


@pytest.mark.parametrize("change", [
    {"owner_os_google_client_id": ""}, {"owner_os_google_subject": ""},
    {"owner_os_google_owner_id": ""}, {"owner_os_google_owner_id": "999"},
    {"owner_os_google_owner_id": "12a"}, {"owner_os_google_enabled": False}])
def test_a_missing_google_binding_refuses(change):
    with pytest.raises(google.GoogleOwnerLoginDenied):
        google.verify_step_up_id_token(token(), settings=settings(**change), now=NOW,
                                       nonce=NONCE)


def test_the_helper_is_a_public_name_of_the_google_module():
    assert "verify_step_up_id_token" in {n for n in dir(google) if not n.startswith("_")}


# ---------------------------------------------------------------------------------------------
# the step-up token


SESSION = "a" * 64


def mint(**over):
    values = dict(session_hash=SESSION, auth_time=NOW - 5, now=NOW, settings=settings())
    values.update(over)
    return step_up.mint(**values)


def check(value, **over):
    values = dict(session_hash=SESSION, now=NOW, settings=settings())
    values.update(over)
    return step_up.verify(value, **values)


def test_a_token_is_valid_for_ten_minutes_for_the_session_it_was_minted_for():
    minted = mint()
    assert minted.expires_at == NOW + 600
    assert check(minted.token) == NOW - 5
    assert check(minted.token, now=NOW + 599) == NOW - 5


def test_an_expired_token_is_refused():
    minted = mint()
    for now in (NOW + 600, NOW + 601, NOW + 86_400):
        with pytest.raises(api.LedgerError) as caught:
            check(minted.token, now=now)
        assert caught.value.code == "step_up_required"


def test_a_token_from_another_session_is_refused():
    minted = mint()
    with pytest.raises(api.LedgerError) as caught:
        check(minted.token, session_hash="b" * 64)
    assert caught.value.code == "step_up_required"


def test_a_tampered_or_malformed_token_is_refused():
    minted = mint().token
    version, auth_time, exp, mac = minted.split(".")
    flipped = mac[:-1] + ("0" if mac[-1] != "0" else "1")
    candidates = [
        f"{version}.{auth_time}.{exp}.{flipped}",
        f"{version}.{int(auth_time) + 1}.{exp}.{mac}",
        f"{version}.{auth_time}.{int(exp) + 1000}.{mac}",
        f"su9.{auth_time}.{exp}.{mac}", f"{version}.{auth_time}.{exp}", "", "x", "." * 3,
        minted + ".x", minted.upper(), f"{version}.-1.{exp}.{mac}", " " + minted,
    ]
    for supplied in candidates:
        with pytest.raises(api.LedgerError) as caught:
            check(supplied)
        assert caught.value.code == "step_up_required", supplied


def test_a_token_minted_under_another_secret_is_refused():
    minted = mint(settings=settings(os_step_up_secret="t" * 32))
    with pytest.raises(api.LedgerError) as caught:
        check(minted.token)
    assert caught.value.code == "step_up_required"


@pytest.mark.parametrize("secret", ["", "short", "s" * 31])
def test_a_missing_or_short_secret_is_ledger_unavailable_for_mint_and_verify(secret):
    unset = settings(os_step_up_secret=secret)
    for call in (lambda: mint(settings=unset), lambda: check("su1.1.2." + "0" * 64, settings=unset)):
        with pytest.raises(api.LedgerError) as caught:
            call()
        assert caught.value.code == "ledger_unavailable"


def test_the_token_carries_no_session_hash_and_the_hmac_is_hex():
    minted = mint().token
    assert SESSION not in minted
    assert minted.startswith("su1.") and len(minted.split(".")[3]) == 64
