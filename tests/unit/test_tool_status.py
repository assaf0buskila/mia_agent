import pytest
from app.domain.tools import (
    ALLOWLISTED_TOOL_STATUSES,
    ToolOutcome,
    tool_status_from_http,
)
from pydantic import ValidationError


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (401, "unauthorized"),
        (403, "unauthorized"),
        (429, "rate_limited"),
        (400, "malformed"),
        (422, "malformed"),
        (500, "retryable"),
        (502, "retryable"),
        (None, "retryable"),
        (True, "error"),
        ("401", "error"),
        (200, "error"),
    ],
)
def test_tool_status_from_http_classifier(status_code: object, expected: str) -> None:
    assert tool_status_from_http(status_code) == expected


def test_tool_outcome_accepts_new_statuses() -> None:
    for status in (
        "unauthorized",
        "rate_limited",
        "malformed",
        "retryable",
        "partial",
        "stale",
    ):
        outcome = ToolOutcome(tool="instagram_insights", status=status, result_count=0)
        assert outcome.status == status


def test_tool_outcome_still_rejects_bad_status() -> None:
    with pytest.raises(ValidationError, match="unknown tool status"):
        ToolOutcome(tool="sheets_mirror", status="bad", result_count=0)


def test_allowlisted_statuses_include_legacy_and_new() -> None:
    assert {"ok", "denied", "empty", "error"}.issubset(ALLOWLISTED_TOOL_STATUSES)
    assert "success" not in ALLOWLISTED_TOOL_STATUSES
