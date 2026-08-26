from app.domain.emotion import detect_emotional_cues, infer_emotional_cues
from app.domain.memory import ConversationTurn


def test_detect_frustrated_hebrew() -> None:
    assert detect_emotional_cues("נמאס לי מהעומס") == ("frustrated",)


def test_detect_overwhelmed_hebrew() -> None:
    assert detect_emotional_cues("הכל בלגן, אני מוצף") == ("overwhelmed",)


def test_detect_overwhelmed_english() -> None:
    assert detect_emotional_cues("We're overwhelmed and falling behind") == (
        "overwhelmed",
    )


def test_detect_skeptical() -> None:
    assert detect_emotional_cues("לא בטוח שזה יעבוד") == ("skeptical",)


def test_detect_empty_message() -> None:
    assert detect_emotional_cues("") == ()
    assert detect_emotional_cues("   ") == ()


def test_infer_carries_tone_from_prior_turn_on_short_answer() -> None:
    turns = (
        ConversationTurn(role="mia", text="איזה חלק נעשה ידנית?"),
        ConversationTurn(role="prospect", text="הכל בלגן, אני מוצף"),
        ConversationTurn(role="mia", text="כמה זמן זה לוקח?"),
    )
    assert infer_emotional_cues("כן", recent_turns=turns) == ("overwhelmed",)


def test_infer_prefers_current_message_tone() -> None:
    turns = (
        ConversationTurn(role="prospect", text="הכל בלגן"),
    )
    assert infer_emotional_cues("נשמע מעולה!", recent_turns=turns) == ("excited",)


def test_infer_carries_tone_when_turns_already_contain_the_current_message() -> None:
    """Production shape: `app/api/website.py` persists the inbound message *before*
    `graph.invoke`, so `recent_turns` already ends with the message being answered.
    Carry-forward must still reach past it instead of comparing only the last turn."""
    turns = (
        ConversationTurn(role="mia", text="איזה חלק נעשה ידנית?"),
        ConversationTurn(role="prospect", text="הכל בלגן, אני מוצף"),
        ConversationTurn(role="mia", text="כמה זמן זה לוקח?"),
        ConversationTurn(role="prospect", text="כן"),
    )
    assert infer_emotional_cues("כן", recent_turns=turns) == ("overwhelmed",)


def test_infer_stays_empty_for_a_neutral_conversation() -> None:
    """No manufactured empathy: ordinary text carries no tone, in either direction."""
    turns = (
        ConversationTurn(role="prospect", text="we run a bakery in haifa"),
        ConversationTurn(role="mia", text="how do orders reach you?"),
        ConversationTurn(role="prospect", text="כן"),
    )
    assert infer_emotional_cues("כן", recent_turns=turns) == ()
    assert infer_emotional_cues("we take orders on the phone", recent_turns=turns) == ()
