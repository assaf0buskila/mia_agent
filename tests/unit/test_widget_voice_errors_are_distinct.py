"""A voice failure must say which failure it was.

The widget used one string, `MIC_ERR`, for six different client-side failures, and it
was byte-identical to the server's `voice_fail` copy. A denied microphone, a silent
recording, a dropped upload, a rate limit and a genuine "could not transcribe" all
rendered the same sentence, and nothing was logged — so "I got the same error" could
not distinguish seven causes.

The rate limit is the one that actively hurt. `LIMITS_PER_IP["voice"]` is 12 per 900s
and `LIMITS_PER_SESSION["voice"]` is 8. A 429 rendered as "try again", so the visitor
retried, spent more of the quota, and was guaranteed the same message.
"""

from __future__ import annotations

from pathlib import Path

WIDGET = (
    Path(__file__).resolve().parents[2] / "app" / "web" / "ask_mia.js"
).read_text(encoding="utf-8")


def _const(name: str) -> str:
    marker = f"var {name} = '"
    start = WIDGET.index(marker) + len(marker)
    return WIDGET[start : WIDGET.index("'", start)]


def test_each_voice_failure_has_its_own_message() -> None:
    messages = {
        name: _const(name)
        for name in ("MIC_ERR", "MIC_PERM", "MIC_NA", "MIC_EMPTY", "MIC_NET", "MIC_BUSY")
    }
    for name, text in messages.items():
        assert text.strip(), name
    # Distinct copy is the whole point: identical strings are what made this
    # undiagnosable in the first place.
    assert len(set(messages.values())) == len(messages), messages


def test_a_rate_limit_does_not_tell_the_visitor_to_retry() -> None:
    """429 is the one case where "try again" guarantees another 429."""
    busy = _const("MIC_BUSY")
    assert "חכו" in busy, busy  # wait
    assert busy != _const("MIC_ERR")
    # The handler must actually branch on the status code.
    assert "err && err.status" in WIDGET
    assert "429" in WIDGET


def test_the_upload_handler_maps_the_codes_it_can_get() -> None:
    # Anchor on the voice upload specifically; the text path has its own retryOnce.
    handler = WIDGET[WIDGET.index("return postVoice(blob);") :]
    handler = handler[: handler.index(".finally(")]
    for code, const in (("429", "MIC_BUSY"), ("415", "MIC_NA"), ("400", "MIC_EMPTY")):
        assert code in handler, code
        assert const in handler, const


def test_an_empty_recording_is_not_reported_as_a_bad_one() -> None:
    """No captured frames means the mic gave us nothing, not that Mia misheard."""
    stop_handler = WIDGET[WIDGET.index("rec.onstop = function () {") :]
    stop_handler = stop_handler[: stop_handler.index("sendVoice(blob);")]
    assert "MIC_EMPTY" in stop_handler
    assert "MIC_ERR" not in stop_handler
