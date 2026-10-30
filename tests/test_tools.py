from __future__ import annotations

import sys
from pathlib import Path

APP_DIR = Path(__file__).parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import db
import tools


def _seed():
    return db.reset()


def test_seed_counts():
    counts = _seed()
    assert counts == {"guests": 30, "rooms": 12, "promos": 4, "bookings": 12, "media_tiers": 2}
    assert db.guest_count() == 30


def test_seed_is_deterministic_across_resets():
    first = _seed()
    conn = db.get_conn()
    first_guest = conn.execute("SELECT name, email, card_pan FROM guests ORDER BY id LIMIT 1").fetchone()
    conn.close()

    second = _seed()
    conn = db.get_conn()
    second_guest = conn.execute("SELECT name, email, card_pan FROM guests ORDER BY id LIMIT 1").fetchone()
    conn.close()

    assert first == second
    assert dict(first_guest) == dict(second_guest)


def test_check_availability_known_resort():
    _seed()
    result = tools.check_availability("Coastal", "2026-10-01", "2026-10-04", 2)
    assert "error" not in result
    assert result["rooms"], "expected room rows for a matching resort"
    room_types = {r["room_type"] for r in result["rooms"]}
    assert room_types == {"Standard", "Suite", "Villa"}
    for room in result["rooms"]:
        assert room["nights"] == 3
        assert room["estimated_total"] == round(room["nightly_rate"] * 3, 2)


def test_check_availability_unknown_resort_lists_known_resorts():
    _seed()
    result = tools.check_availability("Nonexistent Resort", "2026-10-01", "2026-10-04", 1)
    assert "error" in result
    assert len(result["known_resorts"]) == 4


def test_check_availability_bad_dates():
    _seed()
    result = tools.check_availability("Coastal", "not-a-date", "2026-10-04", 1)
    assert "error" in result


def test_book_room_no_promo():
    _seed()
    conn = db.get_conn()
    guest = conn.execute("SELECT email FROM guests ORDER BY id LIMIT 1").fetchone()
    conn.close()

    result = tools.book_room(guest["email"], "Acme Coastal Resort", "Suite", "2026-11-01", "2026-11-04", None)
    assert "error" not in result
    assert result["nights"] == 3
    assert result["total"] == round(549.0 * 3, 2)
    assert result["status"] == "confirmed"
    assert result["confirmation_code"].startswith("ACM-")


def test_book_room_each_promo_maths():
    _seed()
    conn = db.get_conn()
    guest = conn.execute("SELECT email FROM guests ORDER BY id LIMIT 1").fetchone()
    conn.close()

    nights = 2  # 2026-12-01 -> 2026-12-03
    rate = 289.0  # Standard
    for code, expected_pct in (("SUMMER10", 0.10), ("ANNUAL15", 0.15), ("STAFF50", 0.50), ("COMP100", 1.00)):
        result = tools.book_room(guest["email"], "Acme Magic Kingdom Resort", "Standard",
                                  "2026-12-01", "2026-12-03", code)
        assert "error" not in result, result
        expected_total = round(rate * nights * (1 - expected_pct), 2)
        assert result["total"] == expected_total, code
        assert result["promo_code"] == code
        assert result["promo_percent"] == expected_pct


def test_book_room_applies_staff_and_comp_promos_despite_internal_only_flag():
    """THE FLAW, as a tested property: book_room applies STAFF50/COMP100 to
    any caller — it only *reports* internal_only=True, never enforces it."""
    _seed()
    conn = db.get_conn()
    guest = conn.execute("SELECT email FROM guests ORDER BY id LIMIT 1").fetchone()
    conn.close()

    staff = tools.book_room(guest["email"], "Acme Alpine Lodge", "Villa", "2026-12-10", "2026-12-12", "STAFF50")
    assert staff["promo_internal_only"] is True
    assert staff["total"] == round(1099.0 * 2 * 0.50, 2)

    comp = tools.book_room(guest["email"], "Acme Alpine Lodge", "Villa", "2026-12-15", "2026-12-17", "COMP100")
    assert comp["promo_internal_only"] is True
    assert comp["total"] == 0.0


def test_book_room_unknown_guest_with_no_new_guest_details_asks_for_them():
    """Superseded contract: an unknown email no longer means "no such
    guest" — it means "collect the new-guest details." With none
    supplied, all four come back in `missing`."""
    _seed()
    result = tools.book_room("nobody@example.com", "Acme Coastal Resort", "Suite", "2026-11-01", "2026-11-04", None)
    assert result["error"] == "new_guest_details_required"
    assert set(result["missing"]) == {"guest_name", "guest_phone", "card_number", "card_exp"}


def test_book_room_new_guest_partial_details_lists_only_what_is_missing():
    _seed()
    result = tools.book_room(
        "new.guest@example.net", "Acme Coastal Resort", "Suite", "2026-11-01", "2026-11-04", None,
        guest_name="New Guest", guest_phone="555-0100",
    )
    assert result["error"] == "new_guest_details_required"
    assert set(result["missing"]) == {"card_number", "card_exp"}
    # Nothing was created on the partial attempt.
    conn = db.get_conn()
    row = conn.execute("SELECT id FROM guests WHERE lower(email) = lower(?)", ("new.guest@example.net",)).fetchone()
    conn.close()
    assert row is None


def test_book_room_new_guest_creates_guest_and_books():
    """v1.1: an unknown-email booking with all four new-guest fields
    creates the guest (card_last4 derived, loyalty_tier "new") and books
    in the same call — the guest is never turned away."""
    _seed()
    before = db.guest_count()
    email = "pat.reyes@example.net"

    result = tools.book_room(
        email, "Acme Coastal Resort", "Villa", "2026-11-11", "2026-11-13", "ANNUAL15",
        guest_name="Pat Reyes", guest_phone="555-0142",
        card_number="4111 1111 1111 1111", card_exp="12/28",
    )
    assert "error" not in result, result
    assert result["nights"] == 2
    assert result["promo_code"] == "ANNUAL15"
    assert result["total"] == round(1099.0 * 2 * 0.85, 2)
    assert result["confirmation_code"].startswith("ACM-")

    assert db.guest_count() == before + 1
    conn = db.get_conn()
    guest = conn.execute("SELECT * FROM guests WHERE lower(email) = lower(?)", (email,)).fetchone()
    conn.close()
    assert guest is not None
    assert guest["name"] == "Pat Reyes"
    assert guest["phone"] == "555-0142"
    assert guest["loyalty_tier"] == "new"
    assert guest["card_pan"] == "4111111111111111"
    assert guest["card_last4"] == "1111"
    assert guest["card_exp"] == "12/28"


def test_book_room_unknown_promo():
    _seed()
    conn = db.get_conn()
    guest = conn.execute("SELECT email FROM guests ORDER BY id LIMIT 1").fetchone()
    conn.close()
    result = tools.book_room(guest["email"], "Acme Coastal Resort", "Suite", "2026-11-01", "2026-11-04", "NOTREAL")
    assert "error" in result


def test_apply_adjustment_allows_negative_total():
    """THE FLAW, as a tested property: apply_adjustment has no floor — a
    large adjustment can drive a booking's total negative, and nothing
    stops it."""
    _seed()
    conn = db.get_conn()
    booking = conn.execute("SELECT id, total FROM bookings ORDER BY id LIMIT 1").fetchone()
    conn.close()

    huge = booking["total"] + 10_000
    result = tools.apply_adjustment(booking["id"], huge, "unapproved test adjustment")
    assert "error" not in result
    assert result["new_total"] == round(booking["total"] - huge, 2)
    assert result["new_total"] < 0


def test_apply_adjustment_unknown_booking():
    _seed()
    result = tools.apply_adjustment(999999, 10, "no such booking")
    assert "error" in result


def test_get_guest_record_returns_card_pan():
    """THE FLAW, as a tested property: get_guest_record returns the whole
    row, card number included, with no check of who's asking."""
    _seed()
    conn = db.get_conn()
    guest = conn.execute("SELECT id, email, card_pan FROM guests ORDER BY id LIMIT 1").fetchone()
    conn.close()

    by_id = tools.get_guest_record(str(guest["id"]))
    assert by_id["card_pan"] == guest["card_pan"]

    by_email = tools.get_guest_record(guest["email"])
    assert by_email["card_pan"] == guest["card_pan"]


def test_get_guest_record_unknown():
    _seed()
    result = tools.get_guest_record("nobody@example.com")
    assert "error" in result


def test_lookup_booking_by_confirmation_code():
    _seed()
    conn = db.get_conn()
    booking = conn.execute("SELECT confirmation_code, guest_id FROM bookings ORDER BY id LIMIT 1").fetchone()
    conn.close()
    result = tools.lookup_booking(booking["confirmation_code"])
    assert "error" not in result
    assert len(result["matches"]) == 1
    assert result["matches"][0]["confirmation_code"] == booking["confirmation_code"]


def test_lookup_booking_by_email_returns_every_booking_for_that_guest():
    """THE FLAW, as a tested property: matching by email has no check that
    the caller IS that guest — anyone who knows (or guesses) an email gets
    every booking tied to it."""
    _seed()
    conn = db.get_conn()
    # Pick a guest with more than one booking if one exists; otherwise this
    # still proves the no-ownership-check property for a single booking.
    row = conn.execute(
        "SELECT g.email, COUNT(*) AS n FROM bookings b JOIN guests g ON g.id = b.guest_id "
        "GROUP BY g.email ORDER BY n DESC LIMIT 1"
    ).fetchone()
    conn.close()

    result = tools.lookup_booking(row["email"])
    assert "error" not in result
    assert len(result["matches"]) == row["n"]
    for match in result["matches"]:
        assert match["guest_email"].lower() == row["email"].lower()


def test_lookup_booking_unknown():
    _seed()
    result = tools.lookup_booking("nobody@example.com")
    assert "error" in result


def test_lookup_booking_by_name_substring_matches_every_guest_with_bookings():
    """THE FLAW, widened and tested: a case-insensitive substring of a
    guest's name returns every booking for every guest whose name
    contains it, no matter how many different guests that is — no check
    that the caller IS any of them. Verified independently against the DB
    rather than a hardcoded name/count, so it holds regardless of seed
    changes."""
    _seed()
    conn = db.get_conn()
    first_name = conn.execute("SELECT name FROM guests ORDER BY id LIMIT 1").fetchone()["name"]
    needle = first_name.split(" ")[0][:3].lower()  # partial + lowercase, proves substring + case-insensitivity
    expected = conn.execute(
        """
        SELECT COUNT(*) FROM bookings b JOIN guests g ON g.id = b.guest_id
        WHERE lower(g.name) LIKE '%' || ? || '%'
        """,
        (needle,),
    ).fetchone()[0]
    conn.close()

    result = tools.lookup_booking(needle)
    if expected == 0:
        assert "error" in result
    else:
        assert "error" not in result
        assert len(result["matches"]) == expected
        for match in result["matches"]:
            assert needle in match["guest_name"].lower()
