"""Shared wire types for V2-38 and the documented statement-format schema checks.

V2-26 remains the validator, including calendar and cross-field checks. Keep these
transport types in one place; vocabulary limits come from the statement domain.
"""

from app.domain.statements import vocabulary as v

DATE = {"type": "string", "format": "date", "pattern": r"^\d{4}-\d{2}-\d{2}$"}
UUID = {"type": "string", "pattern":
        "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"}
ACCOUNT_LABEL = {"type": "string", "minLength": 1,
                 "maxLength": v.MAX_ACCOUNT_LABEL_CHARS, "pattern": r"^(?!.*\d{8}).*$"}
CONFIDENCE = {"type": "number", "minimum": 0, "maximum": 1}
