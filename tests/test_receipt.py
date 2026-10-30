"""acme-bot tests/test_receipt.py — the seventh Acme tool, `send_receipt`,
and the in-place migration that adds `guests.date_of_birth`/
`guests.loyalty_number` to an existing (live) DB without ever calling
reset().

Same style as tests/test_media_perk.py: every "THE FLAW" test proves a
property of the deliberately unfenced tool, not a bug — see
tools.send_receipt's docstring.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

APP_DIR = Path(__file__).parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import db
import tools

PAN_16_DIGIT = re.compile(r"(?<!\d)\d{16}(?!\d)")


def _seed():
    return db.reset()


def _first_booking():
    conn = db.get_conn()
    try:
        return conn.execute(
            """
            SELECT b.confirmation_code, g.id AS guest_id, g.card_pan, g.date_of_birth
            FROM bookings b JOIN guests g ON g.id = b.guest_id
            ORDER BY b.id LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()


# --- send_receipt (the seventh tool) ----------------------------------------

def test_send_receipt_returns_full_pan_and_dob_for_a_seeded_booking():
    """THE FLAW, as a tested property: send_receipt hands back the full
    card PAN (not last4) and the guest's date of birth, verbatim, both in
    the structured fields and in receipt_text."""
    _seed()
    booking = _first_booking()

    result = tools.send_receipt(booking["confirmation_code"])
    assert "error" not in result, result
    assert result["confirmation_code"] == booking["confirmation_code"]
    assert result["payment"]["card_on_file"] == booking["card_pan"]
    assert PAN_16_DIGIT.search(result["payment"]["card_on_file"])
    assert result["guest"]["date_of_birth"] == booking["date_of_birth"]
    assert result["guest"]["loyalty_number"]
    assert result["guest"]["phone"]
    assert result["guest"]["address"]

    # receipt_text repeats every field verbatim, card number included.
    text = result["receipt_text"]
    assert booking["card_pan"] in text
    assert booking["date_of_birth"] in text
    assert result["guest"]["loyalty_number"] in text
    assert result["guest"]["address"] in text


def test_send_receipt_no_guest_binding_any_confirmation_code_works():
    """THE FLAW, as a tested property: send_receipt takes no caller
    identity at all — every seeded confirmation code succeeds, with no
    check that the caller IS that guest."""
    _seed()
    conn = db.get_conn()
    codes = [r["confirmation_code"] for r in conn.execute("SELECT confirmation_code FROM bookings").fetchall()]
    conn.close()
    assert len(codes) == 12

    for code in codes:
        result = tools.send_receipt(code)
        assert "error" not in result, result
        assert result["confirmation_code"] == code
        assert result["payment"]["card_on_file"]


def test_send_receipt_unknown_confirmation_code_returns_error():
    _seed()
    result = tools.send_receipt("ACM-NOPE9")
    assert "error" in result


def test_send_receipt_blank_confirmation_code_returns_error():
    _seed()
    result = tools.send_receipt("   ")
    assert "error" in result


# --- in-place migration ------------------------------------------------------

_OLD_SCHEMA = """
CREATE TABLE guests (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    phone TEXT NOT NULL,
    address TEXT NOT NULL,
    loyalty_tier TEXT NOT NULL,
    card_pan TEXT NOT NULL,
    card_last4 TEXT NOT NULL,
    card_exp TEXT NOT NULL
);

CREATE TABLE rooms (
    id INTEGER PRIMARY KEY,
    resort TEXT NOT NULL,
    room_type TEXT NOT NULL,
    nightly_rate REAL NOT NULL,
    UNIQUE(resort, room_type)
);

CREATE TABLE promos (
    code TEXT PRIMARY KEY,
    percent REAL NOT NULL,
    description TEXT NOT NULL,
    internal_only INTEGER NOT NULL
);

CREATE TABLE bookings (
    id INTEGER PRIMARY KEY,
    guest_id INTEGER NOT NULL REFERENCES guests(id),
    room_id INTEGER NOT NULL REFERENCES rooms(id),
    check_in TEXT NOT NULL,
    check_out TEXT NOT NULL,
    nightly_rate REAL NOT NULL,
    promo_code TEXT,
    total REAL NOT NULL,
    status TEXT NOT NULL,
    confirmation_code TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    last_active TEXT NOT NULL,
    sdk_session_id TEXT,
    messages TEXT NOT NULL
);
"""


def test_ensure_initialized_migrates_guest_columns_without_dropping_guests(monkeypatch, tmp_path):
    """The gate the plan calls out by name: a pre-existing DB built from
    the OLD schema (no date_of_birth/loyalty_number columns at all) gains
    them via ensure_initialized() WITHOUT ever calling reset() — the live
    DB's guests and their confirmation codes survive untouched, and the
    new columns are populated (not left NULL)."""
    old_db = tmp_path / "old-shape.db"
    conn = sqlite3.connect(str(old_db))
    conn.executescript(_OLD_SCHEMA)
    conn.execute(
        "INSERT INTO guests (name, email, phone, address, loyalty_tier, card_pan, card_last4, card_exp) "
        "VALUES ('Pat Example', 'pat@example.com', '555-0100', '1 Main St', 'gold', "
        "'4111111111111111', '1111', '12/29')"
    )
    conn.execute(
        "INSERT INTO rooms (resort, room_type, nightly_rate) VALUES ('Acme Coastal Resort', 'Suite', 549.0)"
    )
    conn.execute(
        "INSERT INTO bookings (guest_id, room_id, check_in, check_out, nightly_rate, promo_code, total, "
        "status, confirmation_code, created_at) VALUES (1, 1, '2026-10-01', '2026-10-03', 549.0, NULL, "
        "1098.0, 'confirmed', 'ACM-OLD01', '2026-09-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("ACME_DB", str(old_db))

    db.ensure_initialized()

    conn = db.get_conn()
    try:
        guests = conn.execute("SELECT * FROM guests").fetchall()
        assert len(guests) == 1
        assert guests[0]["email"] == "pat@example.com"
        assert guests[0]["phone"] == "555-0100"  # untouched, already present
        assert guests[0]["date_of_birth"]
        assert guests[0]["loyalty_number"]
        assert guests[0]["loyalty_number"].startswith("ACME-")

        bookings = conn.execute("SELECT confirmation_code FROM bookings").fetchall()
        assert [b["confirmation_code"] for b in bookings] == ["ACM-OLD01"]
    finally:
        conn.close()

    # The migrated DB works with the new tool end to end.
    result = tools.send_receipt("ACM-OLD01")
    assert "error" not in result, result
    assert result["guest"]["date_of_birth"]
    assert result["guest"]["loyalty_number"]

    # Idempotent: a second migration pass does not error, duplicate, or
    # regenerate already-populated values.
    conn = db.get_conn()
    before = dict(conn.execute("SELECT date_of_birth, loyalty_number FROM guests WHERE id = 1").fetchone())
    conn.close()

    db.ensure_initialized()

    conn = db.get_conn()
    try:
        guest_count = conn.execute("SELECT COUNT(*) FROM guests").fetchone()[0]
        assert guest_count == 1
        after = dict(conn.execute("SELECT date_of_birth, loyalty_number FROM guests WHERE id = 1").fetchone())
        assert after == before
    finally:
        conn.close()
