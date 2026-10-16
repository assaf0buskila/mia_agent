"""Closed vocabularies and bounds.

The expense values are a copy of the frozen Assaf OS schema, because this package is pure and
imports nothing from the services. tests/unit/statements/test_vocabulary_parity.py proves the
copy against app/prototypes/assaf_os_local_schema.json.
"""

from __future__ import annotations

SCHEMA_VERSION = "statement-batch.v1"

TAGS = ("business", "personal")
KINDS = ("charge", "credit")
MATCH_KINDS = ("exact", "prefix")

PATHS = ("לימוד", "אוטומציה", "משותף")
PLATFORMS = ("Google Ads", "Meta", "אחר")
CURRENCY = "ILS"

DEFAULT_CATEGORY = "לא מסווג"
DEFAULT_PATH = "משותף"
DEFAULT_PLATFORM = "אחר"

PAYMENT_STATUS_PAID = "שולם"
PAYMENT_STATUS_CANCELLED = "בוטל"
SOURCE_TYPE_ACTUAL_PAYMENT = "תשלום בפועל"

# A batch is a bounded document, not a stream: both caps are checked before any parsing work.
MAX_BATCH_BYTES = 128 * 1024
MAX_LINES = 300

MAX_DISPLAY_CHARS = 80
MAX_ACCOUNT_LABEL_CHARS = 40
MAX_CATEGORY_CHARS = 40
MAX_RULE_LABEL_CHARS = 80
MAX_KEY_CHARS = 160
MAX_ALIASES = 10
MAX_AMOUNT_MINOR = 10**10
MIN_PREFIX_CHARS = 3
MIN_PEPPER_BYTES = 16

# The assistant is unsure below this and a human should look; it never blocks anything alone.
LOW_CONFIDENCE = 0.6

# The card is charged around the 15th and Assaf imports by the 20th. Both are only defaults:
# the charge date is whatever the statement prints, and the deadline is a parameter.
DEFAULT_IMPORT_BY_DAY = 20
MAX_IMPORT_BY_DAY = 28
