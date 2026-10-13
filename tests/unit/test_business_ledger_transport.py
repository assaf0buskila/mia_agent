"""Only the signed POST and one bounded Google response GET are permitted."""

from urllib.request import Request

import pytest
from app.core.config import MiaEnv, Settings
from app.services import business_ledger as port


def test_google_response_redirect_drops_post_body_and_cannot_chain():
    handler = port.GoogleResponseRedirect()
    request = Request("https://script.google.com/macros/s/synthetic/exec", data=b"synthetic")
    redirected = handler.redirect_request(
        request, None, 302, "Found", {}, "https://script.googleusercontent.com/synthetic"
    )
    assert redirected.get_method() == "GET"
    assert redirected.data is None
    assert redirected.header_items() == [("Accept", "application/json")]
    with pytest.raises(port.LedgerError):
        handler.redirect_request(
            redirected, None, 302, "Found", {}, "https://script.googleusercontent.com/second"
        )


@pytest.mark.parametrize("url", [
    "http://script.googleusercontent.com/response",
    "https://attacker.invalid/response",
    "https://user:secret@script.googleusercontent.com/response",
    "https://script.googleusercontent.com:8443/response",
])
def test_google_response_redirect_rejects_unapproved_destinations(url):
    request = Request("https://script.google.com/macros/s/synthetic/exec", data=b"synthetic")
    with pytest.raises(port.LedgerError):
        port.GoogleResponseRedirect().redirect_request(request, None, 302, "Found", {}, url)


def test_transport_has_finite_budget_and_timeout_does_not_retry(monkeypatch):
    calls = []

    class Opener:
        def open(self, request, *, timeout):
            calls.append((request.get_method(), timeout))
            raise TimeoutError("synthetic exception text must not leave the port")

    monkeypatch.setattr(port, "build_opener", lambda *_: Opener())
    with pytest.raises(port.LedgerError, match="^source_unavailable$"):
        port._post("https://script.google.com/macros/s/synthetic/exec", b"synthetic")
    assert calls == [("POST", 15)]


@pytest.mark.parametrize("environment", [MiaEnv.DEV, MiaEnv.PROD])
def test_ledger_rejects_non_test_environment_before_transport(monkeypatch, environment):
    calls = []
    monkeypatch.setattr(port, "_post", lambda *args: calls.append(args))
    settings = Settings(
        _env_file=None,
        env=environment,
        owner_os_enabled=True,
        business_ledger_test_enabled=True,
        owner_os_workspace_id="synthetic-ledger",
        business_ledger_workspace_id="synthetic-ledger",
        telegram_owner_user_ids="123456789",
        business_ledger_script_url="https://script.google.com/macros/s/" + "A" * 32 + "/exec",
        business_ledger_hmac_secret="synthetic-test-only-secret-value-12345",
    )
    with pytest.raises(port.LedgerError, match="^operation_unavailable$"):
        port.BusinessLedger(settings, "123456789")
    assert calls == []
