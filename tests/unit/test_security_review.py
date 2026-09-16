"""Pre-cloud security review — contracts that must stay fail-closed."""

import logging
import sys

import httpx
from app.core.config import Settings, get_settings
from app.core.logging import RedactingFilter
from app.core.redact import redact
from app.core.risk import PolicyDecision, RiskAction, RiskLevel, decide
from app.core.write_flags import named_write_may_auto, write_flag_enabled
from app.main import app
from fastapi.testclient import TestClient


def test_cors_allowlist_has_no_wildcard() -> None:
    origins = get_settings().cors_origin_list()
    assert origins
    assert "*" not in origins
    assert "https://www.assafweb.com" in origins
    client = TestClient(app)
    denied = client.get("/health", headers={"Origin": "https://evil.example"})
    assert denied.headers.get("access-control-allow-origin") != "https://evil.example"
    allowed = client.get("/health", headers={"Origin": "https://www.assafweb.com"})
    assert allowed.headers.get("access-control-allow-origin") == "https://www.assafweb.com"


def test_redact_strips_secrets_and_pii() -> None:
    cleaned = redact(
        {
            "email": "a@b.com",
            "phone": "050123",
            "api_key": "sk-live",
            "token": "secret",
            "text": "ok",
            "nested": {"composio_api_key": "cmp", "database_url": "postgres://x"},
        }
    )
    assert cleaned["email"] == "[redacted]"
    assert cleaned["phone"] == "[redacted]"
    assert cleaned["api_key"] == "[redacted]"
    assert cleaned["token"] == "[redacted]"
    assert cleaned["text"] == "ok"
    assert (
        redact("HTTP Request: POST https://api.telegram.org/bot123:AAFakeToken/sendMessage")
        == "HTTP Request: POST https://api.telegram.org/bot[redacted]/sendMessage"
    )
    assert cleaned["nested"]["composio_api_key"] == "[redacted]"
    assert cleaned["nested"]["database_url"] == "[redacted]"


def test_redact_scrubs_bot_token_from_a_non_string_log_arg() -> None:
    """httpx logs `request.url` as an `httpx.URL` object, not a pre-formatted
    string. `redact` used to only pattern-match str/dict/list, so this exact
    object type fell through untouched and leaked the token into CloudWatch."""
    url = httpx.URL("https://api.telegram.org/bot123456:AAFakeTokenValue/sendMessage")
    cleaned = redact(url)
    assert isinstance(cleaned, str)
    assert "AAFakeTokenValue" not in cleaned
    assert cleaned == "https://api.telegram.org/bot[redacted]/sendMessage"

    # A non-URL, non-string arg (e.g. a status code formatted with %d) must
    # come back completely unchanged, in type and value, or %-formatting
    # breaks downstream.
    assert redact(200) == 200
    assert isinstance(redact(200), int)


def test_redacting_filter_scrubs_the_telegram_token_httpx_actually_logs() -> None:
    """End-to-end reproduction of the real leak: httpx's own request logger
    calls `logger.info('HTTP Request: %s %s "%s %d %s"', method, url, ...)`
    with `url` as an object, not a string. Prove the token never survives to
    the formatted log line CloudWatch would receive."""
    token = "123456:AAFakeTokenValueThatMustNeverAppearInLogs"
    url = httpx.URL(f"https://api.telegram.org/bot{token}/sendMessage")
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg='HTTP Request: %s %s "%s %d %s"',
        args=("POST", url, "HTTP/1.1", 200, "OK"),
        exc_info=None,
    )
    assert RedactingFilter().filter(record) is True
    formatted = record.getMessage()
    assert token not in formatted
    assert "AAFakeTokenValueThatMustNeverAppearInLogs" not in formatted
    # The rest of the line -- proof this scrubs the token, not the log line.
    expected = (
        'HTTP Request: POST https://api.telegram.org/bot[redacted]/sendMessage "HTTP/1.1 200 OK"'
    )
    assert formatted == expected


def test_redacting_filter_scrubs_a_non_string_record_msg() -> None:
    """Gap 1: `record.msg` used to only be scrubbed when `isinstance(msg,
    str)` was already true. An object passed directly as `msg` (no `%s`
    args at all) skipped that branch entirely and reached `getMessage()`
    with its secret intact once stringified there."""
    token = "123456:AAFakeTokenInNonStringMsg"
    msg_obj = httpx.URL(f"https://api.telegram.org/bot{token}/sendMessage")
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=0,
        msg=msg_obj, args=(), exc_info=None,
    )
    assert not isinstance(record.msg, str)  # the exact shape the gate used to skip
    assert RedactingFilter().filter(record) is True
    formatted = record.getMessage()
    assert token not in formatted
    assert formatted == "https://api.telegram.org/bot[redacted]/sendMessage"


def test_redacting_filter_scrubs_exception_message_in_final_formatted_output() -> None:
    """Gap 2: an exception whose own message embeds a secret (exactly how a
    provider client error would carry one) must not reach the formatted log
    line. Goes through a real `logging.Formatter`, not just attribute
    inspection, because that -- not `record.exc_info` alone -- is what a
    handler actually writes to CloudWatch."""
    token = "123456:AAFakeTokenInException"
    try:
        raise RuntimeError(f"failed calling https://api.telegram.org/bot{token}/sendMessage")
    except RuntimeError:
        exc_info = sys.exc_info()

    record = logging.LogRecord(
        name="test", level=logging.ERROR, pathname=__file__, lineno=0,
        msg="owner turn failed", args=(), exc_info=exc_info,
    )
    original_exception = exc_info[1]
    assert RedactingFilter().filter(record) is True
    formatted = logging.Formatter("%(message)s").format(record)

    assert token not in formatted
    assert "Traceback (most recent call last):" in formatted  # structure intact
    assert "RuntimeError" in formatted  # exception type still visible
    # Scrubbed at the record level only -- the caller's own exception object,
    # which other code up the stack may still hold, is never mutated.
    assert token in str(original_exception)
    assert token in original_exception.args[0]


def test_redacting_filter_leaves_a_normal_exception_traceback_intact() -> None:
    """A plain exception with nothing sensitive in it must format exactly as
    before -- proof this fix cannot be confused with "we stopped logging
    errors"."""
    try:
        raise ValueError("plain failure, nothing sensitive here")
    except ValueError:
        exc_info = sys.exc_info()

    record = logging.LogRecord(
        name="test", level=logging.ERROR, pathname=__file__, lineno=0,
        msg="something broke", args=(), exc_info=exc_info,
    )
    assert RedactingFilter().filter(record) is True
    formatted = logging.Formatter("%(message)s").format(record)

    assert "something broke" in formatted
    assert "ValueError" in formatted
    assert "plain failure, nothing sensitive here" in formatted
    assert "Traceback (most recent call last):" in formatted


def test_r4_approval_r5_deny_not_flag_overridable() -> None:
    assert (
        decide(
            RiskAction(name="meta_write", risk=RiskLevel.R4_FINANCIAL_MARKETING),
            kill_switch=False,
        )
        == PolicyDecision.APPROVAL
    )
    assert (
        decide(
            RiskAction(name="delete_data", risk=RiskLevel.R5_DESTRUCTIVE),
            kill_switch=False,
        )
        == PolicyDecision.DENY
    )
    assert named_write_may_auto(enabled=True, risk=RiskLevel.R4_FINANCIAL_MARKETING) is False
    assert named_write_may_auto(enabled=True, risk=RiskLevel.R5_DESTRUCTIVE) is False
    settings = Settings()
    assert write_flag_enabled(settings, "gmail_send") is False
    assert write_flag_enabled(settings, "meta_write") is False
    assert write_flag_enabled(settings, "auto_followup") is False
    assert write_flag_enabled(settings, "browser_automation") is False
    assert write_flag_enabled(settings, "dynamic_tool_discovery") is False
