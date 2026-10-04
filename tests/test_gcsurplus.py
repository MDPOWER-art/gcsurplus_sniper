import pytest
import pytest_asyncio
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

from ai.analyzer import CFOEvaluator
from core.config import TARGET_REGISTRY, MAX_CAPITAL_EXPOSURE, MIN_NET_PROFIT_THRESHOLD


def test_target_registry_categories():
    assert "Infrastructure_TI" in TARGET_REGISTRY
    assert "Laboratoire_Scientifique" in TARGET_REGISTRY
    assert "Medical_Veterinaire" in TARGET_REGISTRY
    assert "Arpentage_Ingenierie" in TARGET_REGISTRY


def test_cfo_formula():
    evaluator = CFOEvaluator()
    # Net = (Estimated_Value * 0.85) - (Bid * 1.15) - 15.00
    # Estimated = 2000, Bid = 500
    # Net = (2000 * 0.85) - (500 * 1.15) - 15 = 1700 - 575 - 15 = 1110
    net = evaluator.calculate_cfo_net_profit(2000.0, 500.0)
    assert round(net, 2) == 1110.0


def test_cfo_rejection_below_threshold():
    evaluator = CFOEvaluator()
    # Estimated = 600, Bid = 200
    # Net = (600 * 0.85) - (200 * 1.15) - 15 = 510 - 230 - 15 = 265 (< 500)
    net = evaluator.calculate_cfo_net_profit(600.0, 200.0)
    assert net < MIN_NET_PROFIT_THRESHOLD


@pytest.mark.asyncio
async def test_database_and_exposure(tmp_path, monkeypatch):
    test_db = tmp_path / "test_gcsurplus.db"
    monkeypatch.setattr("core.database.DB_PATH", test_db)

    from core.database import init_db, authorize_bid, record_active_bid, get_current_exposure

    await init_db()

    # Initial exposure should be 0.0
    exposure = await get_current_exposure()
    assert exposure == 0.0

    # Test authorization for bid under MAX_CAPITAL_EXPOSURE
    allowed = await authorize_bid(1000.0)
    assert allowed is True

    # Record a pending bid of 2500
    await record_active_bid(
        listing_id="test-101",
        bid_amount=2500.0,
        status="PENDING"
    )

    current_exp = await get_current_exposure()
    assert current_exp == 2500.0

    # New bid of 1000 would result in 3500 exposure > 3000 -> Should be blocked
    allowed_over = await authorize_bid(1000.0)
    assert allowed_over is False


@pytest.mark.asyncio
async def test_save_listing_reports_new_insert_and_keeps_price_numeric(
    tmp_path,
    monkeypatch,
):
    test_db = tmp_path / "test_save_listing.db"
    monkeypatch.setattr("core.database.DB_PATH", test_db)

    from core.database import init_db, save_listing

    await init_db()
    listing = {
        "id": "lot-123",
        "title": "Test server",
        "category": "Computer equipment",
        "current_bid": 123.45,
        "minimum_bid": 150.0,
        "location": "Ottawa,ON",
        "closing_date": "2026-11-15 10:30",
        "url": "https://example.test/lot-123",
    }

    assert await save_listing(listing) is True
    assert await save_listing(listing) is False

    import aiosqlite

    async with aiosqlite.connect(test_db) as db:
        await db.execute(
            "UPDATE listings SET is_analyzed = 1, estimated_value = 1000 WHERE id = ?",
            ("lot-123",),
        )
        await db.commit()

    updated_listing = {
        **listing,
        "minimum_bid": 175.0,
        "location": "Vancouver,BC",
    }
    assert await save_listing(updated_listing) is False

    async with aiosqlite.connect(test_db) as db:
        async with db.execute(
            """SELECT current_bid, typeof(current_bid), minimum_bid,
                      typeof(minimum_bid), location, is_analyzed, estimated_value
               FROM listings WHERE id = ?""",
            ("lot-123",),
        ) as cursor:
            row = await cursor.fetchone()

    assert row == (123.45, "real", 175.0, "real", "Vancouver,BC", 0, 0.0)


@pytest.mark.asyncio
async def test_cfo_uses_minimum_bid_and_rejects_non_supported_provinces():
    evaluator = CFOEvaluator()
    evaluated_prompts = []

    class FakeModels:
        async def generate_content(self, **kwargs):
            evaluated_prompts.extend(kwargs["contents"])
            return SimpleNamespace(
                parsed=SimpleNamespace(
                    estimated_resale_value=3000.0,
                    visual_defects_found=False,
                    defect_details="",
                ),
                text=None,
            )

    evaluator.client = SimpleNamespace(
        aio=SimpleNamespace(models=FakeModels())
    )
    listing = {
        "id": "qc-lot",
        "title": "Workstation",
        "category": "Computer equipment",
        "description": "Desktop workstation",
        "current_bid": 0.0,
        "minimum_bid": 1320.0,
        "location": "Montréal,QC",
    }

    result = await evaluator.analyze_listing(listing)

    assert result["cfo_net_profit"] == pytest.approx(
        evaluator.calculate_cfo_net_profit(3000.0, 1320.0)
    )
    assert result["is_viable"] is True
    assert "Cost basis" in evaluated_prompts[0]
    assert "$1320.00" in evaluated_prompts[0]

    evaluated_prompts.clear()
    listing["location"] = "Penticton,BC"
    rejected = await evaluator.analyze_listing(listing)

    assert rejected["is_viable"] is False
    assert rejected["estimated_value"] == 0.0
    assert "outside supported area" in rejected["rejection_reason"]
    assert evaluated_prompts == []


@pytest.mark.asyncio
async def test_init_db_migrates_existing_listings_table(tmp_path, monkeypatch):
    import sqlite3

    from core.config import SCHEMA_PATH
    from core.database import init_db

    test_db = tmp_path / "legacy.db"
    monkeypatch.setattr("core.database.DB_PATH", test_db)
    legacy_schema = SCHEMA_PATH.read_text(encoding="utf-8")
    legacy_schema = legacy_schema.replace(
        "    minimum_bid REAL NOT NULL DEFAULT 0.0,\n"
        "    location TEXT NOT NULL DEFAULT '',\n",
        "",
    ).replace("    is_active INTEGER NOT NULL DEFAULT 1,\n", "")
    with sqlite3.connect(test_db) as db:
        db.executescript(legacy_schema)
        db.execute(
            """INSERT INTO listings
               (id, title, category, current_bid, closing_date, url)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("legacy-1", "Legacy item", "Computer equipment", 10.0, "2026-11-15", "https://example.test"),
        )

    await init_db()

    with sqlite3.connect(test_db) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(listings)")}
        row = db.execute(
            "SELECT minimum_bid, location, is_active FROM listings WHERE id = ?",
            ("legacy-1",),
        ).fetchone()

    assert {"minimum_bid", "location", "is_active"} <= columns
    assert row == (0.0, "", 1)


@pytest.mark.asyncio
async def test_sync_active_listings_and_filter_analysis_and_watcher(
    tmp_path,
    monkeypatch,
):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from core.database import (
        get_pending_analysis_listings,
        get_unbound_snipes_due,
        init_db,
        save_listing,
        sync_active_listings,
    )

    test_db = tmp_path / "test_active_listings.db"
    monkeypatch.setattr("core.database.DB_PATH", test_db)
    await init_db()

    toronto = ZoneInfo("America/Toronto")
    closing_datetime = datetime.now(toronto) + timedelta(seconds=10)
    closing_date = closing_datetime.isoformat(timespec="seconds")
    for listing_id in ("lot-1", "lot-2"):
        await save_listing(
            {
                "id": listing_id,
                "title": f"Server {listing_id}",
                "category": "Computer equipment",
                "current_bid": 10.0,
                "closing_date": closing_date,
                "url": f"https://example.test/{listing_id}",
            }
        )

    assert await sync_active_listings(["lot-1"]) == 1

    pending = await get_pending_analysis_listings()
    assert {listing["id"] for listing in pending} == {"lot-1"}

    import aiosqlite

    async with aiosqlite.connect(test_db) as db:
        await db.execute(
            "UPDATE listings SET is_analyzed = 1, is_viable = 1"
        )
        await db.commit()

    due = await get_unbound_snipes_due(seconds_window=15)
    assert {listing["id"] for listing in due} == {"lot-1"}
    due_datetime = datetime.fromisoformat(due[0]["closing_date"])
    assert due_datetime.tzinfo is not None
    assert due_datetime.utcoffset() == closing_datetime.utcoffset()

    assert await sync_active_listings([]) == 0
    async with aiosqlite.connect(test_db) as db:
        flags = await db.execute_fetchall(
            "SELECT id, is_active FROM listings ORDER BY id"
        )
    assert flags == [("lot-1", 1), ("lot-2", 0)]
