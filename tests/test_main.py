import pytest

import main


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
