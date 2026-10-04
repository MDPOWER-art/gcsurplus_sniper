from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import AsyncGenerator

import pytest

from core.auth_manager import GCAuthSessionManager


@asynccontextmanager
async def response_context(html: str) -> AsyncGenerator["FakeResponse", None]:
    yield FakeResponse(html)


class FakeResponse:
    def __init__(self, html: str, status: int = 200) -> None:
        self.html = html
        self.status = status

    async def text(self) -> str:
        return self.html


class FakeSession:
    def __init__(self, response_html: str) -> None:
        self.response_html = response_html
        self.posts: list[tuple[str, dict[str, str]]] = []

    def post(
        self,
        url: str,
        *,
        data: dict[str, str],
        ssl: bool,
    ) -> AbstractAsyncContextManager[FakeResponse]:
        self.posts.append((url, data))
        return response_context(self.response_html)


@pytest.mark.asyncio
async def test_bid_posts_all_main_form_hidden_fields_and_requires_echo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = GCAuthSessionManager()
    session = FakeSession(
        """
        <form id="main">
          <input type="hidden" name="h_bidLotNo" value="770619">
          <input type="text" name="frm_txtMaxBidAmt" value="125.00">
        </form>
        """
    )
    page_html = """
    <form id="not-main">
      <input type="hidden" name="ignored" value="no">
    </form>
    <form id="main" action="mn-eng.cfm">
      <input type="hidden" name="h_bidLotNo" value="770619">
      <input type="hidden" name="h_urltoken" value="test-token">
      <input type="hidden" name="h_empty" value="">
      <input type="text" name="frm_txtMaxBidAmt">
    </form>
    """

    async def get_session() -> FakeSession:
        return session

    async def fetch_page(_url: str) -> str:
        return page_html

    monkeypatch.setattr(manager, "get_session", get_session)
    monkeypatch.setattr(manager, "fetch_page", fetch_page)

    result = await manager.place_snipe_bid(
        "https://example.test/detail?lcn=770619",
        125.0,
    )

    assert result is True
    assert session.posts == [
        (
            "https://example.test/mn-eng.cfm",
            {
                "h_bidLotNo": "770619",
                "h_urltoken": "test-token",
                "h_empty": "",
                "frm_txtMaxBidAmt": "125.00",
            },
        )
    ]


@pytest.mark.asyncio
async def test_bid_response_with_active_error_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = GCAuthSessionManager()
    session = FakeSession(
        """
        <div id="modalBidError">
          <section class="alert alert-danger" role="alert">
            Bid Error: bid exceeds the maximum allowed.
          </section>
        </div>
        """
    )

    async def get_session() -> FakeSession:
        return session

    async def fetch_page(_url: str) -> str:
        return '<form id="main"><input type="hidden" name="h_bidLotNo" value="1"></form>'

    monkeypatch.setattr(manager, "get_session", get_session)
    monkeypatch.setattr(manager, "fetch_page", fetch_page)

    result = await manager.place_snipe_bid("https://example.test/lot", 125.0)

    assert result is False


@pytest.mark.asyncio
async def test_ambiguous_bid_response_remains_unconfirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = GCAuthSessionManager()
    session = FakeSession("<html><body>Page loaded.</body></html>")

    async def get_session() -> FakeSession:
        return session

    async def fetch_page(_url: str) -> str:
        return '<form id="main"><input type="hidden" name="h_bidLotNo" value="1"></form>'

    monkeypatch.setattr(manager, "get_session", get_session)
    monkeypatch.setattr(manager, "fetch_page", fetch_page)

    result = await manager.place_snipe_bid("https://example.test/lot", 125.0)

    assert result is None
