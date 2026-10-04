from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import AsyncGenerator

import pytest

from core.auth_manager import auth_manager
from scrapers.gcsurplus import GCSurplusScraper, _parse_price_and_closing_date


@asynccontextmanager
async def response_context(html: str) -> AsyncGenerator["FakeResponse", None]:
    yield FakeResponse(html)


@pytest.mark.parametrize(
    ("site_date", "expected"),
    [
        ("08-October-2026 @ 12:23", "2026-10-08T12:23:00-04:00"),
        ("05-October-2026 @ 9:00 a.m.", "2026-10-05T09:00:00-04:00"),
        ("05-Oct-2026 @ 9:00 PM", "2026-10-05T21:00:00-04:00"),
    ],
)
def test_closing_date_is_normalized_to_sqlite_datetime(
    site_date: str,
    expected: str,
) -> None:
    current_price, closing_date = _parse_price_and_closing_date(
        f"Current Bid (CAD): $12.00\nClosing: {site_date}"
    )

    assert current_price == 12.0
    assert closing_date == expected


def test_unrecognized_closing_date_is_not_returned_as_valid() -> None:
    current_price, closing_date = _parse_price_and_closing_date(
        "Current Bid (CAD): $12.00\nClosing: not-a-date"
    )

    assert current_price == 12.0
    assert closing_date is None


class FakeResponse:
    def __init__(self, html: str) -> None:
        self.html = html

    def raise_for_status(self) -> None:
        return None

    async def text(self) -> str:
        return self.html


class FakeSession:
    def __init__(self, html: str) -> None:
        self.html = html
        self.requests: list[tuple[str, dict[str, str] | None]] = []

    def get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
    ) -> AbstractAsyncContextManager[FakeResponse]:
        self.requests.append((url, params))
        return response_context(self.html)


@pytest.mark.asyncio
async def test_scrape_cycle_uses_shared_session_and_returns_required_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = """
    <table id="srchResultData">
      <tbody>
        <tr>
          <td><div><a href="mn-eng.cfm?snc=wfsav&sc=enc-bid&lcn=123&lct=L">
            Dell PowerEdge server
          </a></div></td>
          <td>Open Bidding</td>
          <td>Current Bid (CAD): $0.00</td>
          <td>Minimum bid: $150.00</td>
          <td>Closing: 05-October-2026 @ 9:00 a.m.</td>
          <dl>
            <dt>Minimum bid:</dt><dd>$150.00</dd>
            <dt>Location:</dt><dd>North York,ON</dd>
          </dl>
        </tr>
        <tr>
          <td><a href="mn-eng.cfm?snc=wfsav&sc=enc-bid&lcn=456&lct=L">
            Office chair
          </a></td>
          <td>Current Bid (CAD): $10.00</td>
          <td>Closing: 06-October-2026 @ 9:00 a.m.</td>
        </tr>
        <tr>
          <td><a href="mn-eng.cfm?snc=wfsav&sc=enc-bid&lcn=789&lct=L">
            Lenovo server
          </a></td>
          <td>Current Bid (CAD): $200.00</td>
          <td>Closing: 07-October-2026 @ 9:00 a.m.</td>
          <dl>
            <dt>Location:</dt><dd>Ottawa,ON</dd>
          </dl>
        </tr>
      </tbody>
    </table>
    """
    session = FakeSession(html)

    async def get_session() -> FakeSession:
        return session

    monkeypatch.setattr(auth_manager, "get_session", get_session)
    listings = await GCSurplusScraper().run_scrape_cycle()

    assert len(session.requests) == 5
    assert {params["hpcs"] for _, params in session.requests if params} == {
        "7000",
        "5900",
        "5800",
        "6500",
        "6700",
    }
    assert all(params["saleType"] == "OB" for _, params in session.requests if params)
    assert len(listings) == 2
    assert set(listings[0]) == {
        "item_id",
        "title",
        "current_price",
        "minimum_bid",
        "closing_date",
        "location",
        "url",
        "raw_data",
    }
    assert listings[0]["item_id"] == "123"
    assert listings[0]["title"] == "Dell PowerEdge server"
    assert listings[0]["current_price"] == 0.0
    assert listings[0]["minimum_bid"] == 150.0
    assert listings[0]["closing_date"] == "2026-10-05T09:00:00-04:00"
    assert listings[0]["location"] == "North York,ON"
    assert listings[0]["url"] == (
        "https://www.gcsurplus.ca/mn-eng.cfm?snc=wfsav&sc=enc-bid&lcn=123&lct=L"
    )
    assert "Current Bid (CAD): $0.00" in listings[0]["raw_data"]
    assert listings[1]["item_id"] == "789"
    assert listings[1]["current_price"] == 200.0
    assert listings[1]["minimum_bid"] == 200.0
    assert listings[1]["location"] == "Ottawa,ON"


@pytest.mark.asyncio
async def test_scrape_cycle_surfaces_an_access_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession("<html><body>Web Page Blocked!</body></html>")

    async def get_session() -> FakeSession:
        return session

    monkeypatch.setattr(auth_manager, "get_session", get_session)

    with pytest.raises(RuntimeError, match="page de blocage"):
        await GCSurplusScraper().run_scrape_cycle()
