import logging
import re
from datetime import datetime
from typing import List, Optional, TypedDict
from urllib.parse import parse_qs, urljoin, urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Tag

from core.auth_manager import auth_manager
from core.config import GCSURPLUS_BASE_URL, GCSURPLUS_SEARCH_URL

logger = logging.getLogger("gcsurplus_sniper.scraper")
TORONTO_TIMEZONE = ZoneInfo("America/Toronto")
TARGET_CATEGORY_CODES = ("7000", "5900", "5800", "6500", "6700")
RESULTS_TABLE_SELECTOR = "table#srchResultData"

KEYWORDS = (
    "server",
    "switch",
    "router",
    "cisco",
    "dell",
    "hp",
    "storage",
    "san",
    "rack",
    "oscilloscope",
    "microscope",
    "spectrometer",
    "centrifuge",
    "laser",
    "ultrasound",
)

PRICE_PATTERN = re.compile(
    r"(?:current\s+(?:bid|price)|current\s+offer|prix\s+(?:actuel|courant)|"
    r"offre\s+actuelle)\s*(?:\([^)]*\))?\s*:?\s*(?:CAD\s*)?\$?\s*"
    r"([\d,]+(?:\.\d{1,2})?)",
    re.IGNORECASE,
)
MONEY_PATTERN = re.compile(r"[\d,]+(?:\.\d{1,2})?")
NO_BIDS_PATTERN = re.compile(
    r"(?:current\s+(?:bid|price)|current\s+offer|prix\s+(?:actuel|courant)|"
    r"offre\s+actuelle)\s*(?:\([^)]*\))?\s*:?\s*(?:no bids|aucune offre)",
    re.IGNORECASE,
)
CLOSING_DATE_PATTERN = re.compile(
    r"(?:closing\s+date|closing|closes|auction\s+ends|end\s+date|date\s+de\s+cl[oô]ture|"
    r"date\s+de\s+fermeture)\s*:?\s*([^\n]+)",
    re.IGNORECASE,
)
CLOSING_DATE_FORMATS = (
    "%d-%B-%Y @ %I:%M %p",
    "%d-%B-%Y @ %H:%M",
    "%d-%b-%Y @ %I:%M %p",
    "%d-%b-%Y @ %H:%M",
)


class ScrapedListing(TypedDict):
    item_id: str
    title: str
    current_price: float
    minimum_bid: float
    closing_date: str
    location: str
    url: str
    raw_data: str


def _get_item_id(anchor: Tag) -> Optional[str]:
    href = anchor.get("href", "")
    if not isinstance(href, str):
        return None

    query = parse_qs(urlsplit(href).query)
    for key in ("lcn", "item_id", "itemid", "lot_id", "lotid"):
        value = next(
            (values[0].strip() for name, values in query.items()
             if name.casefold() == key and values[0].strip()),
            None,
        )
        if value:
            return value

    node: Optional[Tag] = anchor
    for _ in range(3):
        for attribute in ("data-item-id", "data-lot-id", "data-id"):
            value = node.get(attribute)
            if isinstance(value, str) and value.strip():
                return value.strip()
        parent = node.parent
        node = parent if isinstance(parent, Tag) else None
        if node is None:
            break

    return None


def _parse_price_and_closing_date(text: str) -> tuple[Optional[float], Optional[str]]:
    price_matches = list(PRICE_PATTERN.finditer(text))
    if len(price_matches) == 1:
        current_price = float(price_matches[0].group(1).replace(",", ""))
    elif NO_BIDS_PATTERN.search(text):
        current_price = 0.0
    else:
        current_price = None

    closing_matches = list(CLOSING_DATE_PATTERN.finditer(text))
    closing_date = None
    if len(closing_matches) == 1:
        raw_closing_date = " ".join(
            closing_matches[0].group(1).replace("\xa0", " ").split()
        )
        raw_closing_date = re.sub(
            r"\b([ap])\s*\.\s*m\s*\.?",
            r"\1m",
            raw_closing_date,
            flags=re.IGNORECASE,
        )
        for date_format in CLOSING_DATE_FORMATS:
            try:
                closing_date = datetime.strptime(
                    raw_closing_date, date_format
                ).replace(tzinfo=TORONTO_TIMEZONE).isoformat(timespec="seconds")
                break
            except ValueError:
                continue
    return current_price, closing_date


def _get_detail_value(row: Tag, label_pattern: re.Pattern[str]) -> Optional[str]:
    for label in row.find_all("dt"):
        label_text = label.get_text(" ", strip=True)
        if label_pattern.search(label_text):
            value = label.find_next_sibling("dd")
            if value is not None:
                text = value.get_text(" ", strip=True)
                if text:
                    return text
    return None


def _parse_money(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    match = MONEY_PATTERN.search(value)
    return float(match.group(0).replace(",", "")) if match else None


class GCSurplusScraper:
    async def run_scrape_cycle(self) -> List[ScrapedListing]:
        """Fetch and locally filter lot listings using the authenticated shared session."""
        session = await auth_manager.get_session()
        discovered_listings: List[ScrapedListing] = []
        seen_item_ids = set()
        total_html_chars = 0

        for category_code in TARGET_CATEGORY_CODES:
            page_url = GCSURPLUS_SEARCH_URL
            params = {
                "snc": "wfsav",
                "sc": "ach-shop",
                "jstp": "sly",
                "hpsr": "",
                "hpcs": category_code,
                "vndsld": "0",
                "saleType": "OB",
                "srchType": "",
                "rpp": "25",
                "str": "1",
            }
            seen_pages = set()

            while page_url not in seen_pages:
                seen_pages.add(page_url)
                logger.info(
                    "Chargement des lots de la catégorie %s : %s",
                    category_code,
                    page_url,
                )
                async with session.get(page_url, params=params) as response:
                    response.raise_for_status()
                    html = await response.text()

                soup = BeautifulSoup(html, "html.parser")
                body_text = soup.get_text("\n", strip=True)
                if "web page blocked!" in body_text.casefold():
                    raise RuntimeError(
                        "GC Surplus a renvoyé une page de blocage au lieu des lots."
                    )
                total_html_chars += len(body_text)

                results_table = soup.select_one(RESULTS_TABLE_SELECTOR)
                if results_table is None:
                    raise RuntimeError(
                        f"La page GC Surplus ne contient pas le tableau "
                        f"de résultats attendu pour la catégorie {category_code}."
                    )

                for row in results_table.find_all("tr"):
                    for anchor in row.find_all("a", href=True):
                        href = anchor.get("href")
                        if not isinstance(href, str) or "sc=enc-bid" not in href.casefold():
                            continue
                        item_id = _get_item_id(anchor)
                        title = anchor.get_text(" ", strip=True)
                        if not item_id or not title or item_id in seen_item_ids:
                            continue
                        seen_item_ids.add(item_id)

                        listing_text = row.get_text("\n", strip=True)
                        raw_data = " ".join(listing_text.split())
                        searchable_text = f"{title} {raw_data}".casefold()
                        if not any(keyword in searchable_text for keyword in KEYWORDS):
                            continue

                        current_price, closing_date = _parse_price_and_closing_date(
                            listing_text
                        )
                        minimum_bid = _parse_money(
                            _get_detail_value(
                                row,
                                re.compile(r"\b(?:minimum|starting)\b", re.IGNORECASE),
                            )
                        )
                        if minimum_bid is None and current_price > 0:
                            minimum_bid = current_price
                        location = _get_detail_value(
                            row,
                            re.compile(r"\blocation\b", re.IGNORECASE),
                        )
                        if (
                            current_price is None
                            or minimum_bid is None
                            or not closing_date
                            or not location
                        ):
                            logger.warning(
                                "Lot %s ignoré : offre minimale, localisation ou "
                                "autre donnée requise introuvable.",
                                item_id,
                            )
                            continue

                        discovered_listings.append(
                            {
                                "item_id": item_id,
                                "title": title,
                                "current_price": current_price,
                                "minimum_bid": minimum_bid,
                                "closing_date": closing_date,
                                "location": location,
                                "url": urljoin(GCSURPLUS_BASE_URL, href),
                                "raw_data": raw_data,
                            }
                        )

                next_page = next(
                    (
                        anchor.get("href")
                        for anchor in soup.find_all("a", href=True)
                        if anchor.get_text(" ", strip=True).casefold() == "next"
                        and parse_qs(urlsplit(anchor["href"]).query).get(
                            "hpcs", [None]
                        )[0] == category_code
                    ),
                    None,
                )
                if not isinstance(next_page, str):
                    break
                page_url = urljoin(GCSURPLUS_SEARCH_URL, next_page)
                params = {}

        logger.info(
            "Analyse terminée : %s caractères de texte sur les pages de catégories.",
            total_html_chars,
        )

        logger.info(
            "Cycle de scraping terminé. %s lots pertinents trouvés.",
            len(discovered_listings),
        )
        return discovered_listings


# Instance exportée attendue par main.py
gcsurplus_scraper = GCSurplusScraper()
