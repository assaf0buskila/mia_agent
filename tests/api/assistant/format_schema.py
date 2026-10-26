"""Derive the wire schema from the Markdown field table, not the transport implementation.

The document has an example and a field table rather than a JSON Schema fence. Parse its
required/optional fields, limits and closed vocabularies. Semantic checks (dates relative to
one another, card-like strings, etc.) remain the statement service's job.
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def object_schema(properties, required):
    return {"type": "object", "properties": properties, "required": required,
            "additionalProperties": False}


def schema_from_format():
    doc = (ROOT / "docs/STATEMENT_BATCH_FORMAT.md").read_text(encoding="utf-8")
    example = json.loads(re.search(r"```json\n(.*?)\n```", doc, re.S)[1])
    table = doc.split("| Field | Rule |", 1)[1].split("\n\n", 1)[0]
    rows = [r.split("|")[1:3] for r in table.splitlines() if r.startswith("| ")]
    fields = {left.strip(): right.strip() for left, right in rows}
    # Any added, removed, or reclassified field fails until both transports adopt it.
    assert len(fields) == 8
    required_row = next(left for left in fields if "`txn_date`" in left)
    required_line = re.findall(r"`([a-z_]+)`", re.sub(r"\([^)]*\)", "", required_row))
    optional_rows = [left for left, rule in fields.items() if rule.startswith("optional")]
    optional = [f for left in optional_rows
                for f in re.findall(r"`([a-z_]+)`", re.sub(r"\([^)]*\)", "", left))]
    assert set(required_line) == {"txn_date", "merchant_display", "amount_minor", "kind",
                                  "currency", "confidence", "suggested_tag"}
    assert set(optional) == {"original_amount_minor", "original_currency", "installment",
                             "suggested_category", "suggested_path", "is_subscription"}
    max_lines = int(re.search(r"At most \*\*(\d+) lines", doc)[1])

    def bound(name):
        left, right = next((field, rule) for field, rule in fields.items()
                           if f"`{name}`" in field)
        return int(re.search(r"at most (\d+)", left if "line " in left else right)[1])

    def choices(name):
        # Closed vocabularies in the field table are the consecutive backticked values
        # inside that field's parentheses (the suggested_path row uses the same form).
        left = next(field for field in fields if f"`{name}`" in field)
        values = re.search(r"`" + name + r"` \(([^)]+)\)", left)[1]
        return [None if v == "null" else v for v in re.findall(r"`([^`]+)`", values)]

    date = {"type": "string", "format": "date", "pattern": r"^\d{4}-\d{2}-\d{2}$"}
    amount = {"type": "integer", "minimum": 1}
    properties = {
        "txn_date": date, "merchant_display": {"type": "string", "minLength": 1,
                                                "maxLength": bound("merchant_display")},
        "amount_minor": amount, "kind": {"type": "string", "enum": choices("kind")},
        "currency": {"type": "string", "const": choices("currency")[0]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "suggested_tag": {"enum": choices("suggested_tag")},
        "original_amount_minor": amount,
        "original_currency": {"type": "string", "pattern": "^[A-Z]{3}$", "not": {"const": "ILS"}},
        "installment": object_schema({"n": {"type": "integer", "minimum": 1},
                                       "of": {"type": "integer", "minimum": 1}}, ["n", "of"]),
        "suggested_category": {"type": "string", "maxLength": bound("suggested_category")},
        "suggested_path": {"type": "string", "enum": choices("suggested_path")},
        "is_subscription": {"type": "boolean"},
    }
    assert set(properties) == set(required_line + optional)
    line = object_schema(properties, required_line)
    line["dependentRequired"] = {"original_amount_minor": ["original_currency"],
                                 "original_currency": ["original_amount_minor"]}
    return object_schema({
        "schema": {"type": "string", "const": example["schema"]},
        "client_batch_id": {"type": "string", "pattern":
                            "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"},
        "account_label": {"type": "string", "minLength": 1,
                          "maxLength": bound("account_label"), "pattern": r"^(?!.*\d{8}).*$"},
        "charge_date": date,
        "lines": {"type": "array", "minItems": 1, "maxItems": max_lines, "items": line},
    }, list(example))
