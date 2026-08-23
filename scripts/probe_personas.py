"""Local probe: replay persona conversations through extract -> NBA -> reply.

Prints the actual action and reply per turn so the sequence can be judged before it
is written into an eval dataset. Not a test.

Run: uv run python scripts/probe_personas.py [website]
"""

from __future__ import annotations

import sys

from app.domain.extract import extract_sales_signals
from app.domain.sales import SalesState, mark_action_delivered, select_next_action
from app.graph.replies import reply_for

PERSONAS: dict[str, list[str]] = {
    "shoe_store_inventory_he": [
        "אני מוכר נעליים יש לי עיסוק רק במלאי",
        "להכניס הכל לשיטס",
        "נעליים מידות דגמים",
        "כל יום בערך שעה",
        "אני מחליט לבד",
    ],
    "clinic_missed_calls_en": [
        "We run a clinic and miss calls all day.",
        "we call everyone back by hand from a list",
        "about two hours every day",
        "I decide this quarter",
        "let's book a meeting",
    ],
    "ecommerce_support_overload_en": [
        "We have an online store and support is overwhelming.",
        "we copy paste order details into a spreadsheet",
        "every day, maybe three hours",
        "I decide this quarter",
    ],
    "restaurant_reservations_he": [
        "יש לנו מסעדה ווואטסאפ עמוס כל היום",
        "אני רושם הזמנות ידנית ביומן",
        "כל יום, שעתיים",
        "אני הבעלים",
    ],
    "real_estate_leads_en": [
        "I'm in real estate and I forget to follow up with leads all day.",
        "I update the sheet by hand after every call",
        "about an hour a day",
    ],
    "one_word_he": ["היי", "מלאי", "כן", "לא יודע", "שיטס"],
    "hebrew_slang": [
        "אחי יש לי בלגן שלם עם ההזמנות",
        "מעתיק הכל לאקסל בערב",
        "כל יום שעה בערך",
    ],
    "mixed_he_en": [
        "יש לי online store והכל ידני",
        "אני מזין orders לשיטס",
        "כל יום",
    ],
    "direct_price_question_he": ["כמה זה עולה?"],
    "skeptical_buyer_en": ["I don't trust AI to talk to my customers."],
    "requests_human_he": ["אני רוצה לדבר עם אסף"],
    "student_non_buyer_en": ["I'm a student, this is a school project"],
    "ready_to_book_en": ["let's book a meeting"],
    "comparing_vendors_en": ["we already use another system"],
    "enterprise_committee_en": ["I need to ask my partner before any change"],
}


def main() -> int:
    channel = "website" if "website" in sys.argv[1:] else None
    for name, messages in PERSONAS.items():
        print(f"\n=== {name} (channel={channel}) ===")
        sales = SalesState(lead_id=name)
        for text in messages:
            sales = extract_sales_signals(sales, text)
            action = select_next_action(sales, channel=channel)
            sales = mark_action_delivered(sales, action)
            reply = reply_for("website", action, sales=sales)
            print(f"  > {text}")
            print(f"  {action.value:<18} | {reply}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
