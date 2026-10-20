"""Typed date cells in the owner xlsx import (contract section 22, erratum E27).

One golden per rule, on synthetic workbooks built here (never a real export). The unit goldens pin
``xlsx_dates`` on its own; the workbook goldens pin what ``prepare`` does with a typed cell in each
kind of field: a date-only column, a datetime column and the accepted journal's ``accepted_at``.

Reference serials, 1900 system: 2026-10-01 is 46296, 2026-01-15 is 46037, 2026-07-01 is 46204,
2026-03-27 is 46108 and 2026-10-25 is 46320. In the 1904 system each is 1462 lower. Israel moved to
summer time on 2026-03-27 at 02:00 (to 03:00) and back on 2026-10-25 at 02:00 (to 01:00).
"""

import pytest
from app.prototypes import assaf_os_export_provenance as oracle
from app.services.os_ledger import xlsx_dates
from app.services.os_ledger.import_workbook import (
    prepare,
    quarantine_count,
    quarantine_reasons,
    staged_records,
)
from app.services.os_ledger.xlsx import read_xlsx

from tests.fixtures.workbooks.synthetic import (
    JOURNAL,
    STYLE_DATE,
    STYLE_DATETIME,
    Typed,
    synthetic_records,
    synthetic_tables,
    tables_from_records,
    write_book,
)

FIELDS = {entity: [field["key"] for field in oracle.NATIVE_FIELDS[entity]]
          for entity in oracle.NATIVE_FIELDS}
PAYMENT_DATE = FIELDS["payments"].index("date")
CLIENT_CREATED = FIELDS["clients"].index("created_at")
CLIENT_NAME = FIELDS["clients"].index("name")
ACCEPTED_AT = oracle.ACCEPTED_HEADERS.index("accepted_at")


def from_serial(text, kind, **options):
    return xlsx_dates.from_serial(text, kind, **options)


def refused(call, *args, **kwargs):
    with pytest.raises(xlsx_dates.DateValueError) as caught:
        call(*args, **kwargs)
    assert str(caught.value) == "unsafe_date_value"  # a fixed code, never the cell


# ---- unit goldens: one per rule -------------------------------------------------------------


def test_golden_1900_system_date_only():
    assert from_serial("46296", "date") == "2026-10-01"
    assert from_serial("46296.0", "date") == "2026-10-01"
    # The edges: the first day, the last day before Excel's fictional leap day, the day after it,
    # and the last serial Excel has.
    assert from_serial("1", "date") == "1900-01-01"
    assert from_serial("59", "date") == "1900-02-28"
    assert from_serial("61", "date") == "1900-03-01"
    assert from_serial("2958465", "date") == "9999-12-31"


def test_golden_date1904_system():
    assert from_serial("44834", "date", date_1904=True) == "2026-10-01"
    assert from_serial("1", "date", date_1904=True) == "1904-01-02"
    # The same wall-clock moment as the 1900 system's 46204.5, 1462 days earlier.
    assert from_serial("44742.5", "datetime", date_1904=True) == "2026-07-01T09:00:00.000Z"
    # The 1900 system's fictional leap day is an ordinary day here, and 1904 reaches 9999 sooner.
    assert from_serial("60", "date", date_1904=True) == "1904-03-01"
    assert from_serial("2957003", "date", date_1904=True) == "9999-12-31"
    refused(from_serial, "2957004", "date", date_1904=True)


@pytest.mark.parametrize("kind", ["date", "datetime"])
@pytest.mark.parametrize("text", ["60", "60.0", "60.5", "60.9999"])
def test_golden_the_fictional_1900_02_29_is_refused(kind, text):
    refused(from_serial, text, kind)


def test_golden_date_only_takes_the_calendar_date_with_no_zone_conversion():
    # Midnight is the same calendar day, not the previous evening in UTC.
    assert from_serial("46296", "date") == "2026-10-01"
    assert from_serial("46108", "date") == "2026-03-27"
    assert from_serial("46320", "date") == "2026-10-25"


@pytest.mark.parametrize("text", ["46296.5", "46296.25", "46296.000001", "46296.0000000000001"])
def test_golden_a_fraction_on_a_date_only_field_is_refused(text):
    refused(from_serial, text, "date")


def test_golden_datetime_in_winter_is_wall_clock_in_jerusalem():
    # 12:30 at UTC+2.
    assert from_serial("46037.520833333336", "datetime") == "2026-01-15T10:30:00.000Z"
    assert from_serial("46037", "datetime") == "2026-01-14T22:00:00.000Z"  # midnight, UTC+2


def test_golden_datetime_in_summer_uses_the_dst_offset():
    # 12:00 at UTC+3, three hours from UTC and not two.
    assert from_serial("46204.5", "datetime") == "2026-07-01T09:00:00.000Z"
    # Float noise in the last digits does not move the instant off the millisecond, and a real
    # millisecond does (rounded to the nearest).
    assert from_serial("46204.500000000007", "datetime") == "2026-07-01T09:00:00.000Z"
    assert from_serial("46204.5000000116", "datetime") == "2026-07-01T09:00:00.001Z"


def test_golden_the_transition_days_resolve_on_both_sides_of_the_change():
    # Spring forward: 02:00-03:00 does not exist; 01:59 is +2 and 03:00 is +3.
    assert from_serial("46108.0826388889", "datetime") == "2026-03-26T23:59:00.000Z"
    assert from_serial("46108.125", "datetime") == "2026-03-27T00:00:00.000Z"
    # Fall back: 01:00-02:00 happens twice; 00:30 is +3 and 02:30 is +2.
    assert from_serial("46320.0208333333", "datetime") == "2026-10-24T21:30:00.000Z"
    assert from_serial("46320.1041666667", "datetime") == "2026-10-25T00:30:00.000Z"


@pytest.mark.parametrize("text", ["46320.0625", "46320.0416666667", "46320.0791666667"])
def test_golden_an_ambiguous_local_time_is_refused(text):
    # 01:30, 01:00 and 01:55 on 2026-10-25: the hour that happens twice.
    refused(from_serial, text, "datetime")
    assert from_serial("46320.0833333333", "datetime") == "2026-10-25T00:00:00.000Z"  # 02:00 is not


@pytest.mark.parametrize("text", ["46108.1041666667", "46108.0833333333", "46108.1180555556"])
def test_golden_a_nonexistent_local_time_is_refused(text):
    # 02:30, 02:00 and 02:50 on 2026-03-27: the hour that does not happen.
    refused(from_serial, text, "datetime")


def test_golden_a_date_only_field_is_not_subject_to_the_dst_rules():
    assert from_serial("46108", "date") == "2026-03-27"
    assert from_serial("46320", "date") == "2026-10-25"


@pytest.mark.parametrize("kind", ["date", "datetime"])
@pytest.mark.parametrize("text", [
    "0", "0.5", "0.999", "-1", "-46296", "2958466", "2958465.5", "99999999",
    "1E+999999999", "-1E+999999999", "1E-999999999", "1" + "0" * 100,
    # Not the reader's plain decimal text: padding, separators, other scripts' digits, words.
    " 46296", "46296 ", "4_6296", "٤٦٢٩٦", "NaN", "Infinity", "", "46296,5",
])
def test_golden_out_of_range_serials_are_refused(kind, text):
    refused(from_serial, text, kind)
    refused(from_serial, text, kind, date_1904=True)


def test_golden_the_range_edges_are_inclusive():
    assert from_serial("1", "date") == "1900-01-01"
    assert from_serial("2958465", "date") == "9999-12-31"
    assert from_serial("2958465", "datetime") == "9999-12-30T22:00:00.000Z"  # midnight at UTC+2
    refused(from_serial, "2958465.0000001", "datetime")


def test_golden_a_t_d_iso_cell_is_a_date():
    assert xlsx_dates.from_iso("2026-10-01", "date") == "2026-10-01"
    assert xlsx_dates.from_iso("2026-10-01T00:00:00", "date") == "2026-10-01"
    assert xlsx_dates.from_iso("2026-10-01T00:00:00.000", "date") == "2026-10-01"
    # Datetime: wall-clock Jerusalem time, converted with the DST offset of that day.
    assert xlsx_dates.from_iso("2026-07-01T12:00:00", "datetime") == "2026-07-01T09:00:00.000Z"
    assert xlsx_dates.from_iso("2026-01-15T12:30:00", "datetime") == "2026-01-15T10:30:00.000Z"
    assert xlsx_dates.from_iso("2026-07-01T12:00:00.250", "datetime") == "2026-07-01T09:00:00.250Z"
    assert xlsx_dates.from_iso("2026-07-01", "datetime") == "2026-06-30T21:00:00.000Z"


@pytest.mark.parametrize("kind,text", [
    ("date", "2026-10-01T09:30:00"),  # a time of day on a date-only field
    ("date", "2026-10-01T00:00:00.5"),
    ("date", "2026-10-01T00:00:00Z"),  # a zone: wall-clock or instant is unspecified
    ("datetime", "2026-10-01T00:00:00Z"),
    ("datetime", "2026-10-01T00:00:00+03:00"),
    ("datetime", "2026-10-25T01:30:00"),  # ambiguous
    ("datetime", "2026-03-27T02:30:00"),  # nonexistent
    ("date", "2026-02-30"), ("date", "2026-13-01"), ("date", "1899-12-31"), ("date", ""),
    ("date", "01/10/2026"), ("date", "2026-10-01 "), ("datetime", "2026-10-01T24:00:00"),
    ("datetime", "2026-10-01T10:00:60"), ("date", "2026-1-1"),
    ("date", "٢٠٢٦-١٠-٠١"),  # other scripts' digits
])
def test_golden_unsafe_iso_cells_are_refused(kind, text):
    refused(xlsx_dates.from_iso, text, kind)


def test_a_non_date_field_kind_is_a_programming_error_not_a_refusal():
    with pytest.raises(ValueError, match="unsupported date field kind"):
        xlsx_dates.from_serial("46296", "text")


# ---- workbook goldens: what prepare does with a typed cell -----------------------------------


def prepared(tmp_path, tables, *, date_1904=False, name="typed.xlsx"):
    path = write_book(tmp_path / name, tables, date_1904=date_1904)
    return prepare(read_xlsx(path), workspace_id="synthetic-workspace", authority_epoch=1)


def payment_with(cell):
    tables = synthetic_tables()
    tables["payments"][1][PAYMENT_DATE] = cell  # PAY_ILS, whose string date is 2026-10-01
    return tables


def pay_ils(batch):
    return next(row for row in batch.rows if row.record_id == "PAY_ILS")


def test_a_typed_date_maps_to_what_the_string_cell_said(tmp_path):
    clean = prepared(tmp_path, synthetic_tables(), name="strings.xlsx")
    assert quarantine_count(clean) == 0 and pay_ils(clean).canonical_record["date"] == "2026-10-01"
    for cell, options in [
        (Typed("46296", style=STYLE_DATE), {}),
        (Typed("46296", style=STYLE_DATETIME), {}),  # a datetime format on a date-only field
        (Typed("44834", style=STYLE_DATE), {"date_1904": True}),
        (Typed("2026-10-01", t="d"), {}),
    ]:
        batch = prepared(tmp_path, payment_with(cell), name="typed.xlsx", **options)
        assert quarantine_count(batch) == 0 and pay_ils(batch).status == "staged"
        # The journal's fingerprint of the string cell still matches the typed one.
        assert staged_records(batch) == staged_records(clean)


@pytest.mark.parametrize("cell,options,reason", [
    (Typed("60", style=STYLE_DATE), {}, "unsafe_date_value"),  # 1900-02-29
    (Typed("46296.5", style=STYLE_DATE), {}, "unsafe_date_value"),
    (Typed("0", style=STYLE_DATE), {}, "unsafe_date_value"),
    (Typed("2958466", style=STYLE_DATE), {}, "unsafe_date_value"),
    (Typed("2026-10-01T09:30:00", t="d"), {}, "unsafe_date_value"),
    (Typed("2026-10-01T00:00:00Z", t="d"), {}, "unsafe_date_value"),
    (Typed("44834.5", style=STYLE_DATE), {"date_1904": True}, "unsafe_date_value"),
])
def test_an_unsafe_date_cell_quarantines_the_row_with_a_code(tmp_path, cell, options, reason):
    batch = prepared(tmp_path, payment_with(cell), **options)
    assert pay_ils(batch).status == "quarantined" and reason in pay_ils(batch).quarantine
    assert quarantine_reasons(batch)[reason] == 1


def test_the_1904_flag_changes_what_a_serial_means(tmp_path):
    # The 1900 reading of 44834 is 2022-09-30: valid, but not the journal's date, so it is held.
    wrong = prepared(tmp_path, payment_with(Typed("44834", style=STYLE_DATE)), name="wrong.xlsx")
    assert pay_ils(wrong).status == "quarantined"
    assert "accepted_input_changed" in pay_ils(wrong).quarantine
    assert "unsafe_date_value" not in pay_ils(wrong).quarantine


def test_a_date_in_a_field_that_takes_no_date_has_no_binding(tmp_path):
    tables = synthetic_tables()
    tables["clients"][1][CLIENT_NAME] = Typed("46296", style=STYLE_DATE)
    batch = prepared(tmp_path, tables)
    assert "date_serial_binding_unspecified" in batch.rows[0].quarantine
    tables["clients"][1][CLIENT_NAME] = Typed("2026-10-01", t="d")
    batch = prepared(tmp_path, tables)
    assert "date_serial_binding_unspecified" in batch.rows[0].quarantine


def test_strings_in_date_and_datetime_fields_keep_the_iso_string_path(tmp_path):
    batch = prepared(tmp_path, synthetic_tables())
    assert quarantine_count(batch) == 0
    # A datetime string is carried exactly as written, not re-expressed: the journal's accepted_at
    # is a string here and stays one.
    acceptance = batch.rows[0].provenance["native_acceptance"]
    assert acceptance["accepted_at"] == "2026-10-02T00:00:00+00:00"
    # A number in a date field with no date format is not a date either: it stays the plain number
    # and the record is invalid, exactly as before.
    tables = synthetic_tables()
    tables["payments"][1][PAYMENT_DATE] = 46296
    batch = prepared(tmp_path, tables, name="number.xlsx")
    assert "record_invalid" in pay_ils(batch).quarantine
    assert "unsafe_date_value" not in pay_ils(batch).quarantine


def journal_with(cell):
    tables = synthetic_tables()
    tables[JOURNAL][1][ACCEPTED_AT] = cell  # PER_ONE revision 1, the first record
    return tables


@pytest.mark.parametrize("cell,expected", [
    (Typed("46037.520833333336", style=STYLE_DATETIME), "2026-01-15T10:30:00.000Z"),  # winter
    (Typed("46204.5", style=STYLE_DATETIME), "2026-07-01T09:00:00.000Z"),  # summer, DST
    (Typed("46297.125", style=STYLE_DATETIME), "2026-10-02T00:00:00.000Z"),
    (Typed("2026-07-01T12:00:00", t="d"), "2026-07-01T09:00:00.000Z"),
])
def test_a_typed_accepted_at_is_the_utc_instant_of_the_jerusalem_wall_clock(
    tmp_path, cell, expected
):
    batch = prepared(tmp_path, journal_with(cell))
    assert batch.issues == () and quarantine_count(batch) == 0
    assert batch.rows[0].provenance["native_acceptance"]["accepted_at"] == expected


def test_a_typed_accepted_at_in_a_1904_workbook(tmp_path):
    batch = prepared(tmp_path, journal_with(Typed("44742.5", style=STYLE_DATETIME)),
                     date_1904=True)
    assert batch.issues == ()
    assert batch.rows[0].provenance["native_acceptance"]["accepted_at"] == (
        "2026-07-01T09:00:00.000Z")


@pytest.mark.parametrize("cell", [
    Typed("46320.0625", style=STYLE_DATETIME),  # ambiguous: 01:30 on the fall-back night
    Typed("46108.1041666667", style=STYLE_DATETIME),  # nonexistent: 02:30 in the spring gap
    Typed("60.5", style=STYLE_DATETIME),  # Excel's fictional 1900-02-29
    Typed("0.5", style=STYLE_DATETIME),  # a time of day alone
    Typed("2958466", style=STYLE_DATETIME),
    Typed("2026-10-25T01:30:00", t="d"),
])
def test_an_unsafe_accepted_at_leaves_the_record_unaccepted(tmp_path, cell):
    batch = prepared(tmp_path, journal_with(cell))
    assert "invalid_journal_acceptance" in batch.issues
    assert "unaccepted_record" in batch.rows[0].quarantine


def clients_created(text, cell):
    records = synthetic_records()
    records["clients"][0]["created_at"] = text  # the journal's fingerprint covers the string
    tables = tables_from_records(records)
    tables["clients"][1][CLIENT_CREATED] = cell
    return tables


def test_a_typed_created_at_is_read_as_the_datetime_the_fingerprint_covers(tmp_path):
    # created_at is a datetime in the physical descriptor, so a typed cell takes the instant form
    # that Apps Script's Date.toISOString() fingerprints.
    batch = prepared(tmp_path, clients_created(
        "2026-07-01T09:00:00.000Z", Typed("46204.5", style=STYLE_DATETIME)))
    assert batch.issues == () and batch.rows[0].status == "staged"
    # Control: another moment in the same cell no longer matches the accepted fingerprint.
    batch = prepared(tmp_path, clients_created(
        "2026-07-01T09:00:00.000Z", Typed("46204.6", style=STYLE_DATETIME)), name="other.xlsx")
    assert "accepted_raw_changed" in batch.rows[0].quarantine
    # And an unsafe one is refused on the cell.
    batch = prepared(tmp_path, clients_created(
        "2026-10-24T22:30:00.000Z", Typed("46320.0625", style=STYLE_DATETIME)), name="gap.xlsx")
    assert "unsafe_date_value" in batch.rows[0].quarantine


def test_the_fields_read_as_dates_are_the_descriptors_date_and_datetime_columns():
    typed = {(entity, field["key"]): field["type"]
             for entity, fields in oracle.NATIVE_FIELDS.items() for field in fields
             if field["type"] in {"date", "datetime"}}
    assert {key for key, kind in typed.items() if kind == "date"} == {
        ("works", "start_date"), ("payments", "date"), ("time", "date"),
        ("expenses", "date"), ("tasks", "due_date"),
    }
    assert {key for key, kind in typed.items() if kind == "datetime"} == {
        (entity, key) for entity in ("clients", "leads", "works", "tasks")
        for key in ("created_at", "updated_at")
    } | {("payments", "created_at"), ("time", "created_at"), ("expenses", "created_at")}
