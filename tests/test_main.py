import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiosqlite
import pytest

import main
from core.auth_manager import GCAuthSessionManager


@pytest.mark.parametrize(
    ("proposed_bid", "suggested_max_bid", "expected"),
    [
        (101.0, 101.0, True),
        (101.01, 101.0, False),
        (1.0, 0.0, False),
        (float("nan"), 100.0, False),
        (1.0, float("nan"), False),
    ],
)
def test_bid_cap_never_allows_an_amount_over_the_ai_maximum(
    proposed_bid: float,
    suggested_max_bid: float,
    expected: bool,
) -> None:
    assert main._is_bid_within_ai_cap(proposed_bid, suggested_max_bid) is expected


@pytest.mark.asyncio
async def test_watcher_handles_due_fake_listing_without_form_or_post(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.database import init_db, save_listing

    test_db = tmp_path / "watcher_dry_run.db"
    monkeypatch.setattr("core.database.DB_PATH", test_db)
    await init_db()

    closing_date = (
        datetime.now(ZoneInfo("America/Toronto")) + timedelta(seconds=10)
    ).isoformat(timespec="seconds")
    await save_listing(
        {
            "id": "999999",
            "title": "Dry-run fake server lot",
            "category": "Test",
            "current_bid": 10.0,
            "minimum_bid": 10.0,
            "location": "Brossard, QC",
            "closing_date": closing_date,
            "url": "https://example.invalid/lot/999999",
        }
    )
    async with aiosqlite.connect(test_db) as db:
        await db.execute(
            """
            UPDATE listings
            SET is_analyzed = 1, is_viable = 1, suggested_max_bid = 20.0
            WHERE id = '999999'
            """
        )
        await db.commit()

    class FakeSession:
        def __init__(self) -> None:
            self.post_called = False

        def post(self, *_args, **_kwargs):
            self.post_called = True
            raise AssertionError("Dry-run must not submit an HTTP POST")

    session = FakeSession()
    auth_manager = GCAuthSessionManager()

    async def get_session():
        return session

    async def fetch_page(_url: str) -> str:
        return "<html><body>Invalid lot; no auction form.</body></html>"

    auth_manager.get_session = get_session
    auth_manager.fetch_page = fetch_page
    monkeypatch.setattr(main, "auth_manager", auth_manager)

    notifications: list[dict[str, object]] = []

    class FakeNotifier:
        async def notify_snipe_executed(self, **kwargs):
            notifications.append(kwargs)

    async def allow_bid(_amount: float) -> bool:
        return True

    async def stop_after_one_cycle(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(main, "discord_notifier", FakeNotifier())
    monkeypatch.setattr(main, "authorize_bid", allow_bid)
    monkeypatch.setattr(asyncio, "sleep", stop_after_one_cycle)

    with pytest.raises(asyncio.CancelledError):
        await main.snipe_watcher_worker(poll_interval=0)

    assert session.post_called is False
    assert len(notifications) == 1
    assert notifications[0]["status"] == "FAILED"
    async with aiosqlite.connect(test_db) as db:
        row = await (
            await db.execute(
                "SELECT status FROM active_bids WHERE listing_id = '999999'"
            )
        ).fetchone()
    assert row == ("FAILED",)


@pytest.mark.asyncio
async def test_process_scraped_listings_persists_and_analyzes_only_new_lots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inserted_listings: list[dict[str, object]] = []
    analyzed_listings: list[dict[str, object]] = []
    analyzed_sessions: list[object] = []
    updated_ids: list[str] = []
    session = object()

    class FakeEvaluator:
        async def analyze_listing(self, listing, session=None):
            analyzed_listings.append(listing)
            analyzed_sessions.append(session)
            return {
                "listing_id": listing["id"],
                "estimated_value": 500.0,
                "cfo_net_profit": 100.0,
                "suggested_max_bid": 0.0,
                "is_viable": False,
                "rejection_reason": "below threshold",
            }

    class FakeSessionManager:
        async def get_session(self):
            return session

    async def save_listing(listing):
        inserted_listings.append(listing)
        return listing["id"] == "new-lot"

    async def get_pending_analysis_listings():
        return [inserted_listings[0]]

    async def update_listing_analysis(**kwargs):
        updated_ids.append(kwargs["listing_id"])

    monkeypatch.setattr(main, "cfo_evaluator", FakeEvaluator())
    monkeypatch.setattr(main, "auth_manager", FakeSessionManager())
    monkeypatch.setattr(main, "save_listing", save_listing)
    monkeypatch.setattr(
        main,
        "get_pending_analysis_listings",
        get_pending_analysis_listings,
    )
    monkeypatch.setattr(main, "update_listing_analysis", update_listing_analysis)

    listings = [
        {
            "item_id": "new-lot",
            "title": "Server",
            "current_price": 123.45,
            "minimum_bid": 150.0,
            "closing_date": "2026-11-15 10:30",
            "location": "Ottawa,ON",
            "url": "https://example.test/new-lot",
            "raw_data": "Current Bid (CAD): $123.45 Commodity: 7000 - Computer equipment Location: Ottawa",
        },
        {
            "item_id": "existing-lot",
            "title": "Switch",
            "current_price": 0.0,
            "minimum_bid": 25.0,
            "closing_date": "2026-11-16 10:30",
            "location": "Toronto,ON",
            "url": "https://example.test/existing-lot",
            "raw_data": "Current Bid (CAD): $0.00",
        },
    ]

    inserted_count, analyzed_count = await main.process_scraped_listings(listings)

    assert inserted_count == 1
    assert analyzed_count == 1
    assert len(inserted_listings) == 2
    assert inserted_listings[0]["id"] == "new-lot"
    assert inserted_listings[0]["current_bid"] == 123.45
    assert type(inserted_listings[0]["current_bid"]) is float
    assert inserted_listings[0]["minimum_bid"] == 150.0
    assert inserted_listings[0]["location"] == "Ottawa,ON"
    assert inserted_listings[0]["category"] == "Computer equipment"
    assert len(analyzed_listings) == 1
    assert analyzed_listings[0]["id"] == "new-lot"
    assert analyzed_sessions == [session]
    assert updated_ids == ["new-lot"]


@pytest.mark.asyncio
async def test_process_scraped_listings_analyzes_existing_pending_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analyzed_ids: list[str] = []
    existing_pending = {
        "id": "reset-lot",
        "title": "Existing server",
        "category": "Computer equipment",
        "current_bid": 25.0,
        "minimum_bid": 40.0,
        "location": "Ottawa,ON",
        "closing_date": "2026-11-15 10:30",
        "url": "https://example.test/reset-lot",
        "description": "Server lot",
    }

    class FakeEvaluator:
        async def analyze_listing(self, listing, session=None):
            analyzed_ids.append(listing["id"])
            return {
                "listing_id": listing["id"],
                "estimated_value": 0.0,
                "cfo_net_profit": -43.75,
                "suggested_max_bid": 0.0,
                "is_viable": False,
                "rejection_reason": "fallback valuation",
            }

    class FakeSessionManager:
        async def get_session(self):
            return object()

    async def save_listing(_listing):
        return False

    async def get_pending_analysis_listings():
        return [existing_pending]

    async def update_listing_analysis(**_kwargs):
        return None

    monkeypatch.setattr(main, "cfo_evaluator", FakeEvaluator())
    monkeypatch.setattr(main, "auth_manager", FakeSessionManager())
    monkeypatch.setattr(main, "save_listing", save_listing)
    monkeypatch.setattr(
        main,
        "get_pending_analysis_listings",
        get_pending_analysis_listings,
    )
    monkeypatch.setattr(main, "update_listing_analysis", update_listing_analysis)

    inserted_count, analyzed_count = await main.process_scraped_listings([])

    assert inserted_count == 0
    assert analyzed_count == 1
    assert analyzed_ids == ["reset-lot"]


@pytest.mark.asyncio
async def test_run_scraper_cycle_syncs_fresh_ids_before_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, object]] = []
    scraped_listings = [
        {"item_id": "lot-a"},
        {"item_id": "lot-b"},
        {"item_id": "lot-a"},
    ]

    class FakeScraper:
        async def run_scrape_cycle(self):
            events.append(("scrape", None))
            return scraped_listings

    async def sync_active_listings(item_ids):
        events.append(("sync", set(item_ids)))
        return 2

    async def process_scraped_listings(listings):
        events.append(("process", listings))
        return 1, 1

    monkeypatch.setattr(main, "gcsurplus_scraper", FakeScraper())
    monkeypatch.setattr(main, "sync_active_listings", sync_active_listings)
    monkeypatch.setattr(main, "process_scraped_listings", process_scraped_listings)

    result = await main.run_scraper_cycle_once()

    assert result == (3, 1, 1)
    assert events == [
        ("scrape", None),
        ("sync", {"lot-a", "lot-b"}),
        ("process", scraped_listings),
    ]
