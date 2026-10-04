import math
import ssl
import certifi


import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Dict, Any, Optional, Union
from urllib.parse import urljoin
import aiohttp
from bs4 import BeautifulSoup, Tag
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception,
)
from core.config import GCSURPLUS_USER, GCSURPLUS_PASS, GCSURPLUS_LOGIN_URL, GCSURPLUS_BASE_URL

logger = logging.getLogger("gcsurplus_sniper.auth")


class TransientHttpError(Exception):
    """Exception raised for HTTP 429 or 5xx server errors requiring retry."""

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(f"HTTP {status}: {message}")


def is_transient_error(exception: BaseException) -> bool:
    """Predicate function for tenacity retry: retry on 429 or 5xx or connection issues."""
    if isinstance(exception, TransientHttpError):
        return exception.status == 429 or exception.status >= 500
    if isinstance(exception, (aiohttp.ClientError, asyncio.TimeoutError)):
        return True
    return False


import asyncio


def _is_hidden_element(element: Tag) -> bool:
    current: Optional[Tag] = element
    while current is not None:
        classes = current.get("class", [])
        style = str(current.get("style", "")).replace(" ", "").casefold()
        if (
            current.has_attr("hidden")
            or str(current.get("aria-hidden", "")).casefold() == "true"
            or "hidden" in classes
            or "d-none" in classes
            or "display:none" in style
            or "visibility:hidden" in style
        ):
            return True
        if "modal" in classes and not ({"show", "in"} & set(classes)):
            return True
        parent = current.parent
        current = parent if isinstance(parent, Tag) else None
    return False


def _has_active_bid_error(soup: BeautifulSoup) -> bool:
    error_pattern = re.compile(
        r"\b(?:error|invalid|rejected|required|exceeds?|minimum bid|"
        r"maximum bid|not accepted)\b",
        re.IGNORECASE,
    )
    for element in soup.select(
        "#modalBidError, #modalHighBidMsg, [role='alert'], .alert-danger"
    ):
        if _is_hidden_element(element):
            continue
        if error_pattern.search(element.get_text(" ", strip=True)):
            return True
    return False


class GCAuthSessionManager:
    """
    Manages persistent aiohttp.ClientSession for GC Surplus authentication.
    Handles dynamic CSRF token extraction and exponential retries on rate limits (429) & server errors (5xx).
    """

    def __init__(self) -> None:
        self.session: Optional[aiohttp.ClientSession] = None
        self._is_authenticated: bool = False
        self._headers: Dict[str, str] = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
        }

    async def get_session(self) -> aiohttp.ClientSession:
        """Returns or initializes the single persistent ClientSession."""
        if self.session is None or self.session.closed:
            jar = aiohttp.CookieJar(unsafe=True)
            timeout = aiohttp.ClientTimeout(total=30, connect=10)
            connector = aiohttp.TCPConnector(ssl=False)
            self.session = aiohttp.ClientSession(
                headers=self._headers,
                cookie_jar=jar,
                timeout=timeout,
                connector=connector,
            )
            self._is_authenticated = False
        return self.session

    async def close(self) -> None:
        """Closes the underlying aiohttp session cleanly."""
        if self.session and not self.session.closed:
            await self.session.close()
            self._is_authenticated = False
            logger.info("Auth Session closed.")

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception(is_transient_error),
        reraise=True,
    )
    async def fetch_page(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        method: str = "GET",
        data: Optional[Union[aiohttp.FormData, Dict[str, str]]] = None,
    ) -> str:
        """
        Fetch HTML with automatic retry on transient errors (429 / 5xx).
        """
        session = await self.get_session()
        async with session.request(method, url, params=params, data=data, ssl=False) as resp:
            if resp.status == 429 or resp.status >= 500:
                raise TransientHttpError(resp.status, f"Error fetching {url}")
            resp.raise_for_status()
            return await resp.text()

    def extract_csrf_tokens(self, html: str) -> Dict[str, str]:
        """
        Dynamically extract hidden CSRF/token fields from form elements.
        Searches common input names like 'csrf_token', '_csrf', 'token', 'snc', etc.
        """
        soup = BeautifulSoup(html, "html.parser")
        tokens: Dict[str, str] = {}

        for input_tag in soup.find_all("input", type="hidden"):
            name = input_tag.get("name")
            value = input_tag.get("value", "")
            if name:
                tokens[name] = value

        return tokens

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception(is_transient_error),
        reraise=True,
    )
    async def authenticate(self) -> bool:
        """
        Authenticates with GCSurplus by fetching the login form, extracting dynamic tokens,
        and posting credentials.
        """
        if not GCSURPLUS_USER or not GCSURPLUS_PASS:
            logger.warning("GC Surplus credentials not set in environment. Running in unauthenticated read-only mode.")
            return False

        logger.info("Initiating GC Surplus authentication flow...")
        session = await self.get_session()

        # Step 1: GET login page to obtain initial cookies and dynamic hidden form tokens
        login_page_html = await self.fetch_page(GCSURPLUS_LOGIN_URL)
        form_tokens = self.extract_csrf_tokens(login_page_html)

        # Step 2: Prepare credentials payload merged with dynamic form tokens
        payload = {
            **form_tokens,
            "username": GCSURPLUS_USER,
            "password": GCSURPLUS_PASS,
            "user_id": GCSURPLUS_USER,
            "user_pass": GCSURPLUS_PASS,
            "action": "login",
        }

        # Step 3: POST login request
        async with session.post(GCSURPLUS_LOGIN_URL, data=payload, ssl=False) as resp:
            if resp.status == 429 or resp.status >= 500:
                raise TransientHttpError(resp.status, "Login attempt server error")

            resp_text = await resp.text()
            # Verify authentication success (e.g., presence of logout link or session cookie)
            if "logout" in resp_text.lower() or "deconnexion" in resp_text.lower() or resp.status == 200:
                self._is_authenticated = True
                logger.info("GC Surplus authentication successful.")
                return True
            else:
                logger.error("Authentication failed: Invalid credentials or rejected form payload.")
                self._is_authenticated = False
                return False

    async def place_snipe_bid(
        self, listing_url: str, bid_amount: float
    ) -> Optional[bool]:
        """
        Submit one bid attempt. Return None when the server response is ambiguous.
        """
        if not math.isfinite(bid_amount) or bid_amount <= 0:
            logger.warning("Refusing invalid bid amount: %r", bid_amount)
            return False

        session = await self.get_session()
        page_html = await self.fetch_page(listing_url)
        soup = BeautifulSoup(page_html, "html.parser")
        form = soup.find("form", id="main")
        if not isinstance(form, Tag):
            logger.error("No main form found on listing page %s", listing_url)
            return False

        payload: Dict[str, str] = {}
        for input_tag in form.find_all("input"):
            input_type = str(input_tag.get("type", "")).casefold()
            name = input_tag.get("name")
            if input_type == "hidden" and isinstance(name, str) and name:
                value = input_tag.get("value", "")
                payload[name] = value if isinstance(value, str) else str(value)
        payload["frm_txtMaxBidAmt"] = f"{bid_amount:.2f}"

        action = form.get("action")
        post_url = urljoin(
            listing_url,
            action if isinstance(action, str) and action else listing_url,
        )
        async with session.post(post_url, data=payload, ssl=False) as resp:
            if resp.status == 429 or resp.status >= 500:
                raise TransientHttpError(resp.status, "Bid submission server error")

            resp_text = await resp.text()
            if resp.status >= 400:
                logger.error(
                    "Bid submission returned HTTP %s for %s",
                    resp.status,
                    listing_url,
                )
                return False

        response_soup = BeautifulSoup(resp_text, "html.parser")
        if _has_active_bid_error(response_soup):
            logger.error("GC Surplus returned an active bid error for %s", listing_url)
            return False

        response_form = response_soup.find("form", id="main")
        bid_input = (
            response_form.find("input", attrs={"name": "frm_txtMaxBidAmt"})
            if isinstance(response_form, Tag)
            else None
        )
        echoed_bid = bid_input.get("value") if isinstance(bid_input, Tag) else None
        try:
            confirmed = Decimal(str(echoed_bid)) == Decimal(f"{bid_amount:.2f}")
        except (InvalidOperation, TypeError, ValueError):
            confirmed = False

        if confirmed:
            logger.info(
                "Bid amount echoed without an active error for listing %s",
                listing_url,
            )
            return True

        logger.warning(
            "Bid result is ambiguous for %s; leaving the attempt pending.",
            listing_url,
        )
        return None


# Global singleton instance for auth session management
auth_manager = GCAuthSessionManager()
