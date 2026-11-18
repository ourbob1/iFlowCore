"""Statement ingestion: CSV/PDF parsing, schema-driven mappings, DB import."""

from __future__ import annotations

import json

import pytest

from core.financial.ingestion import (
    BUILTIN_PROFILES,
    FieldSource,
    IngestionError,
    MappingProfile,
    import_records,
    list_profiles,
    load_profile,
    parse_csv,
    parse_pdf,
    read_csv_headers,
    save_profile,
    suggest_profile,
)

from .conftest import new_id

UBER_CSV = """\
Uber Driver Statement
Period: 2026-09-01 to 2026-09-07

Trip UUID,Date/Time,Service Type,Fare,Surge,Wait Time,Tip,Quest,Tolls,Service Fee
aaa-1,2026-09-01 08:15:00,UberX,12.40,2.10,0.35,3.00,,,-3.60
aaa-2,2026-09-01 23:50:00,UberX,"1,020.00",,,,,6.75,-255.00
aaa-3,2026-09-02 10:00:00,Quest Bonus,,,,,25.00,,
aaa-4,2026-09-02 11:00:00,UberX,0.00,,,,,,
"""

LYFT_CSV = "Date;Ride ID;Ride Earnings;Tips;Bonuses;Lyft Fee\n09/03/2026 07:30 PM;L-9;18,50;2,00;;4,10\n"

TASKRABBIT_CSV = """\
Date,Invoice ID,Type,Description,Amount
2026-09-04,INV-1,Task Payment,Furniture assembly,85.00
2026-09-04,INV-1T,Tip,Furniture assembly,15.00
2026-09-05,PO-77,Payout,Weekly payout,-100.00
2026-09-06,INV-2,Refund,Partial refund,-10.00
"""


def uber():
    return BUILTIN_PROFILES["uber-driver-csv"]


# --- CSV parsing ----------------------------------------------------------------


def test_uber_csv_splits_components_and_finds_header_after_preamble():
    result = parse_csv(UBER_CSV, uber(), timezone="America/New_York")
    assert result.ok, result.errors
    by_ref = {r.external_ref: r for r in result.records}

    earning = by_ref["aaa-1#earning"]
    assert earning.gross_amount_minor == 1240 + 210 + 35      # summed columns, exact cents
    assert earning.platform_fee_minor == 360                  # sign stripped, attached to earning
    assert by_ref["aaa-1#tip"].gross_amount_minor == 300
    assert by_ref["aaa-1#tip"].platform_fee_minor == 0
    assert earning.occurred_at == "2026-09-01T12:15:00.000Z"  # EDT → UTC

    assert by_ref["aaa-2#earning"].gross_amount_minor == 102000   # thousands separator
    assert by_ref["aaa-2#reimbursement"].gross_amount_minor == 675
    # 23:50 local on Sep 1 is Sep 2 in UTC — timezone handling matters for tax years.
    assert by_ref["aaa-2#earning"].occurred_at.startswith("2026-09-02T03:50")

    assert by_ref["aaa-3#incentive"].gross_amount_minor == 2500
    assert "aaa-4#earning" not in by_ref                     # $0.00 rows produce nothing
    assert result.rows_seen == 4
    assert result.totals_by_kind()[("USD", "tip")] == 300


def test_timezone_is_required():
    with pytest.raises(IngestionError, match="timezone"):
        parse_csv(UBER_CSV, uber())


def test_european_semicolon_csv_with_decimal_comma():
    profile = BUILTIN_PROFILES["lyft-driver-csv"].with_updates(decimal_separator=",", default_currency="EUR")
    result = parse_csv(LYFT_CSV, profile, timezone="Europe/Paris")
    assert result.ok, result.errors
    by_kind = {r.kind: r for r in result.records}
    assert by_kind["earning"].gross_amount_minor == 1850
    assert by_kind["earning"].platform_fee_minor == 410
    assert by_kind["earning"].currency == "EUR"
    assert by_kind["tip"].gross_amount_minor == 200
    assert by_kind["earning"].occurred_at == "2026-09-03T17:30:00.000Z"


def test_row_typed_statement_maps_kinds_and_skips_payouts():
    result = parse_csv(TASKRABBIT_CSV, BUILTIN_PROFILES["taskrabbit-csv"], timezone="America/Chicago")
    assert result.ok, result.errors
    kinds = [(r.kind, r.gross_amount_minor) for r in result.records]
    assert kinds == [("earning", 8500), ("tip", 1500), ("refund", -1000)]
    assert result.rows_skipped == 1


def test_unknown_row_type_is_an_error_not_income():
    csv_text = TASKRABBIT_CSV + "2026-09-07,X-1,Mystery Credit,??,50.00\n"
    result = parse_csv(csv_text, BUILTIN_PROFILES["taskrabbit-csv"], timezone="UTC")
    assert len(result.errors) == 1
    assert "Mystery Credit" in result.errors[0].message
    assert result.errors[0].line == 6
    assert len(result.records) == 3          # good rows still parsed


def test_bad_rows_reported_with_line_numbers():
    csv_text = "Trip UUID,Date/Time,Fare\nt1,2026-09-01,10.00\nt2,not-a-date,5.00\nt3,2026-09-01,12.345\n"
    result = parse_csv(csv_text, uber(), timezone="UTC")
    assert [e.line for e in result.errors] == [3, 4]
    assert "Unrecognized date" in result.errors[0].message
    assert "decimal places" in result.errors[1].message


def test_sub_cent_amounts_can_be_rounded_by_opt_in():
    csv_text = "Trip UUID,Date/Time,Fare\nt3,2026-09-01,12.345\n"
    result = parse_csv(csv_text, uber().with_updates(round_sub_minor=True), timezone="UTC")
    assert result.records[0].gross_amount_minor == 1235


def test_missing_optional_columns_warn():
    csv_text = "Trip UUID,Date/Time,Fare\nt1,2026-09-01,10.00\n"
    result = parse_csv(csv_text, uber(), timezone="UTC")
    assert result.ok
    assert any("'amount:tip'" in w for w in result.warnings)


def test_no_header_match_raises():
    with pytest.raises(IngestionError, match="No header row"):
        parse_csv("foo,bar\n1,2\n", uber(), timezone="UTC")


def test_bom_and_cp1252_bytes(tmp_path):
    path = tmp_path / "s.csv"
    path.write_bytes("﻿Trip UUID,Date/Time,Fare,Service Type\nt1,2026-09-01,10.00,Café\n".encode("utf-8"))
    assert parse_csv(path, uber(), timezone="UTC").records[0].description == "Café"
    path.write_bytes("Trip UUID,Date/Time,Fare,Service Type\nt1,2026-09-01,10.00,Café\n".encode("cp1252"))
    assert parse_csv(path, uber(), timezone="UTC").records[0].description == "Café"


def test_fallback_refs_are_stable_and_distinguish_identical_rows():
    profile = MappingProfile(name="noref", platform="X", date="Date", amounts={"tip": "Tip"}, timezone="UTC")
    text = "Date,Tip\n2026-09-01,5.00\n2026-09-01,5.00\n2026-09-02,5.00\n"
    first = parse_csv(text, profile)
    second = parse_csv(text, profile)
    refs = [r.external_ref for r in first.records]
    assert len(set(refs)) == 3                               # identical rows kept distinct
    assert refs == [r.external_ref for r in second.records]  # deterministic across imports
    assert all(r.startswith("h:") for r in refs)


# --- mapping profiles -------------------------------------------------------------


def test_profile_json_roundtrip():
    for profile in BUILTIN_PROFILES.values():
        restored = MappingProfile.from_json(profile.to_json())
        assert restored == profile


def test_profile_validation():
    with pytest.raises(IngestionError, match="kind"):
        MappingProfile(name="x", platform="X", date="D", amounts={"salary": "A"})
    with pytest.raises(IngestionError, match="no amount"):
        MappingProfile(name="x", platform="X", date="D", amounts={})
    with pytest.raises(IngestionError, match="timezone"):
        MappingProfile(name="x", platform="X", date="D", amounts={"earning": "A"}, timezone="Mars/Olympus")
    with pytest.raises(IngestionError, match="line_pattern"):
        MappingProfile(name="x", platform="X", date="D", amounts={"earning": "A"}, source_format="pdf")
    with pytest.raises(IngestionError, match="exactly one"):
        MappingProfile(name="x", platform="X", date="D", amounts={"earning": "A", "tip": "B"},
                       kind_column="T", kind_values={"a": "tip"})


def test_field_source_coercion():
    assert FieldSource.coerce("A") == FieldSource(("A",))
    assert FieldSource.coerce(["A", "B"]).combine == "first"
    assert FieldSource.coerce({"columns": ["A", "B"], "combine": "sum"}).combine == "sum"
    with pytest.raises(IngestionError):
        FieldSource.coerce(42)


def test_newer_profile_schema_rejected():
    data = BUILTIN_PROFILES["uber-driver-csv"].to_dict() | {"schema_version": 99}
    with pytest.raises(IngestionError, match="newer"):
        MappingProfile.from_dict(data)


def test_suggest_profile_from_headers():
    headers = read_csv_headers("Order ID,Delivery Date,Total Pay,Customer Tip,Commission\n")[0]
    draft = suggest_profile(headers, name="doordash-draft", platform="DoorDash", timezone="UTC")
    assert draft.date.columns == ("Delivery Date",)
    assert draft.amounts["tip"].columns == ("Customer Tip",)
    assert draft.amounts["earning"].columns == ("Total Pay",)
    assert draft.fee.columns == ("Commission",)
    assert draft.external_ref.columns == ("Order ID",)
    result = parse_csv("Order ID,Delivery Date,Total Pay,Customer Tip,Commission\nD1,2026-09-01,8.00,2.50,1.00\n", draft)
    assert result.ok and len(result.records) == 2


# --- PDF --------------------------------------------------------------------------

PDF_TEXT = """\
Statement for September 2026            Page 1
Date        Ref       Description                Amount     Tip
09/01/2026  INV-1042  Furniture assembly         $85.00     $12.00
09/02/2026  INV-1043  TV mounting                $120.00
Total                                            $205.00
"""


def test_pdf_line_regex_mapping():
    profile = BUILTIN_PROFILES["generic-pdf-statement"]
    result = parse_pdf("ignored.pdf", profile, timezone="UTC", text_extractor=lambda _: PDF_TEXT)
    assert result.ok, result.errors
    got = sorted((r.external_ref, r.kind, r.gross_amount_minor) for r in result.records)
    assert got == [("INV-1042#earning", "earning", 8500), ("INV-1042#tip", "tip", 1200),
                   ("INV-1043#earning", "earning", 12000)]


def test_real_pdf_file_roundtrip(tmp_path):
    fpdf = pytest.importorskip("fpdf")
    pdf = fpdf.FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=10)
    for line in PDF_TEXT.splitlines():
        pdf.cell(0, 6, line, new_x="LMARGIN", new_y="NEXT")
    path = tmp_path / "statement.pdf"
    pdf.output(str(path))
    result = parse_pdf(path, BUILTIN_PROFILES["generic-pdf-statement"], timezone="UTC")
    assert result.ok, result.errors
    assert sum(r.gross_amount_minor for r in result.records) == 8500 + 1200 + 12000


# --- database ---------------------------------------------------------------------


def test_import_is_idempotent(db, platform_id):
    records = parse_csv(UBER_CSV, uber(), timezone="America/New_York").records
    first = import_records(db, platform_id, records, base_currency="USD")
    assert first.inserted == len(records) and first.duplicates == 0
    again = import_records(db, platform_id, records, base_currency="USD")
    assert again.inserted == 0 and again.duplicates == len(records)
    with db.connection() as conn:
        row = conn.execute(
            "SELECT sum(gross_amount_minor) g, sum(platform_fee_minor) f, sum(net_amount_minor) n FROM transactions"
        ).fetchone()
    assert (row["g"], row["f"]) == (1485 + 300 + 102000 + 675 + 2500, 360 + 25500)
    assert row["n"] == row["g"] - row["f"]


def test_import_pins_fx_base_amount(db, platform_id):
    profile = BUILTIN_PROFILES["lyft-driver-csv"].with_updates(decimal_separator=",", default_currency="EUR")
    records = parse_csv(LYFT_CSV, profile, timezone="Europe/Paris").records
    result = import_records(db, platform_id, records, base_currency="USD", fx_rates={"EUR": "1.10"})
    assert result.unconverted == 0
    with db.connection() as conn:
        rows = {r["kind"]: r["base_amount_minor"] for r in conn.execute("SELECT kind, base_amount_minor FROM transactions")}
    assert rows == {"earning": 2035, "tip": 220}

    missing = parse_csv(LYFT_CSV.replace("L-9", "L-10"), profile, timezone="Europe/Paris").records
    assert import_records(db, platform_id, missing, base_currency="USD").unconverted == 2


def test_profiles_persist_and_override_builtins(db):
    custom = uber().with_updates(amounts={"earning": "Net Fare"})
    pid = save_profile(db, custom)
    assert load_profile(db, "uber-driver-csv").amounts["earning"].columns == ("Net Fare",)
    assert save_profile(db, custom.with_updates(timezone="UTC")) == pid       # update, not duplicate
    assert load_profile(db, "uber-driver-csv").timezone == "UTC"
    with db.connection() as conn:
        row = conn.execute("SELECT mapping_version, mapping_json FROM import_mappings").fetchone()
    assert row["mapping_version"] == 2
    assert json.loads(row["mapping_json"])["schema_version"] == 1
    assert "taskrabbit-csv" in list_profiles(db)
    with pytest.raises(IngestionError):
        load_profile(db, "nope")


def test_v1_database_upgrades_to_latest(tmp_path):
    """A Phase 1 database (schema v1) picks up the Phase 2 migrations."""
    import sqlite3

    from database.connection import Database
    from database.migrations import discover_migrations, migrate

    path = tmp_path / "old.db"
    with Database(path, driver="sqlite") as old:
        with old.connection() as conn:
            migrate(conn, sqlite3.complete_statement, discover_migrations()[:1])
            conn.execute("INSERT INTO platforms (id, name) VALUES (?, 'Uber')", (new_id(),))
    with Database(path, driver="sqlite") as upgraded:
        assert upgraded.initialize() == len(discover_migrations())
        with upgraded.connection() as conn:
            assert conn.execute("SELECT count(*) FROM platforms").fetchone()[0] == 1
            assert conn.execute("SELECT count(*) FROM import_mappings").fetchone()[0] == 0
