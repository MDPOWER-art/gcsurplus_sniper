import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set
from zoneinfo import ZoneInfo

import aiosqlite
from core.config import DB_PATH, MAX_CAPITAL_EXPOSURE, SCHEMA_PATH

logger = logging.getLogger("gcsurplus_sniper.db")
TORONTO_TIMEZONE = ZoneInfo("America/Toronto")


async def init_db() -> None:
    """Initialize the SQLite database with WAL mode and load schema."""
    if not SCHEMA_PATH.exists():
        raise FileNotFoundError(f"Schema file not found at {SCHEMA_PATH}")

    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema_sql = f.read()

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode = WAL;")
        await db.execute("PRAGMA synchronous = NORMAL;")
        await db.executescript(schema_sql)
        async with db.execute("PRAGMA table_info(listings)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        if "minimum_bid" not in columns:
            await db.execute(
                "ALTER TABLE listings ADD COLUMN minimum_bid REAL NOT NULL DEFAULT 0.0"
            )
        if "location" not in columns:
            await db.execute(
                "ALTER TABLE listings ADD COLUMN location TEXT NOT NULL DEFAULT ''"
            )
        if "is_active" not in columns:
            await db.execute(
                "ALTER TABLE listings ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1"
            )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_listings_is_active ON listings(is_active)"
        )
        await db.commit()
    logger.info("Database initialized successfully with WAL mode.")


async def get_current_exposure() -> float:
    """
    Calculate current capital exposure across pending or active won/sniped bids.
    Only PENDING and WON/SNIPED bids contribute to active risk.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT COALESCE(SUM(bid_amount), 0.0)
            FROM active_bids
            WHERE status IN ('PENDING', 'SNIPED', 'WON')
            """
        ) as cursor:
            row = await cursor.fetchone()
            return float(row[0]) if row else 0.0


async def authorize_bid(amount: float) -> bool:
    """
    Strict Risk Control: Checks if placing a bid of `amount` would exceed MAX_CAPITAL_EXPOSURE.
    Returns True if allowed, False if blocked.
    """
    current_exposure = await get_current_exposure()
    projected_exposure = current_exposure + amount
    if projected_exposure > MAX_CAPITAL_EXPOSURE:
        logger.warning(
            f"Risk Control Blocked Bid: Requested=${amount:.2f}, "
            f"Current Exposure=${current_exposure:.2f}, Max Allowed=${MAX_CAPITAL_EXPOSURE:.2f}"
        )
        return False
    return True


async def listing_exists(listing_id: str) -> bool:
    """Check if a listing ID has already been recorded in the database."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM listings WHERE id = ?", (listing_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row is not None


async def get_existing_listing_ids() -> Set[str]:
    """Return a set of all listing IDs currently present in SQLite to optimize scraping check."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id FROM listings") as cursor:
            rows = await cursor.fetchall()
            return {row[0] for row in rows}


async def save_listing(listing: Dict[str, Any]) -> bool:
    """Insert a scraped auction listing; return whether it was newly inserted."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT OR IGNORE INTO listings (
                id, title, category, current_bid, minimum_bid, location,
                closing_date, url, image_url, description
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                listing["id"],
                listing["title"],
                listing["category"],
                listing.get("current_bid", 0.0),
                listing.get("minimum_bid", 0.0),
                listing.get("location", ""),
                listing["closing_date"],
                listing["url"],
                listing.get("image_url", ""),
                listing.get("description", ""),
            ),
        )
        inserted = cursor.rowcount == 1
        if not inserted:
            await db.execute(
                """
                UPDATE listings
                SET current_bid = ?,
                    minimum_bid = ?,
                    location = ?,
                    closing_date = ?,
                    url = ?,
                    description = ?,
                    is_active = 1,
                    estimated_value = CASE WHEN
                        current_bid IS NOT ? OR minimum_bid IS NOT ? OR
                        location IS NOT ? OR closing_date IS NOT ?
                        THEN 0.0 ELSE estimated_value END,
                    cfo_net_profit = CASE WHEN
                        current_bid IS NOT ? OR minimum_bid IS NOT ? OR
                        location IS NOT ? OR closing_date IS NOT ?
                        THEN 0.0 ELSE cfo_net_profit END,
                    suggested_max_bid = CASE WHEN
                        current_bid IS NOT ? OR minimum_bid IS NOT ? OR
                        location IS NOT ? OR closing_date IS NOT ?
                        THEN 0.0 ELSE suggested_max_bid END,
                    is_viable = CASE WHEN
                        current_bid IS NOT ? OR minimum_bid IS NOT ? OR
                        location IS NOT ? OR closing_date IS NOT ?
                        THEN 0 ELSE is_viable END,
                    rejection_reason = CASE WHEN
                        current_bid IS NOT ? OR minimum_bid IS NOT ? OR
                        location IS NOT ? OR closing_date IS NOT ?
                        THEN NULL ELSE rejection_reason END,
                    is_analyzed = CASE WHEN
                        current_bid IS NOT ? OR minimum_bid IS NOT ? OR
                        location IS NOT ? OR closing_date IS NOT ?
                        THEN 0 ELSE is_analyzed END
                WHERE id = ?
                """,
                (
                    listing.get("current_bid", 0.0),
                    listing.get("minimum_bid", 0.0),
                    listing.get("location", ""),
                    listing["closing_date"],
                    listing["url"],
                    listing.get("description", ""),
                    *(
                        value
                        for _ in range(6)
                        for value in (
                            listing.get("current_bid", 0.0),
                            listing.get("minimum_bid", 0.0),
                            listing.get("location", ""),
                            listing["closing_date"],
                        )
                    ),
                    listing["id"],
                ),
            )
        await db.commit()
        return inserted


async def sync_active_listings(listing_ids: List[str]) -> int:
    """Synchronize active flags from a complete, non-empty scraper result."""
    active_ids = {str(listing_id).strip() for listing_id in listing_ids}
    active_ids.discard("")
    if not active_ids:
        logger.warning(
            "Skipping active-listing synchronization because the scraper returned no item IDs."
        )
        return 0

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "CREATE TEMP TABLE active_listing_ids (id TEXT PRIMARY KEY)"
        )
        await db.executemany(
            "INSERT INTO active_listing_ids (id) VALUES (?)",
            ((listing_id,) for listing_id in active_ids),
        )
        async with db.execute(
            """
            UPDATE listings
            SET is_active = 0
            WHERE is_active != 0
              AND NOT EXISTS (
                  SELECT 1
                  FROM active_listing_ids
                  WHERE active_listing_ids.id = listings.id
              )
            """
        ) as cursor:
            deactivated_count = cursor.rowcount
        async with db.execute(
            """
            UPDATE listings
            SET is_active = 1
            WHERE is_active != 1
              AND EXISTS (
                  SELECT 1
                  FROM active_listing_ids
                  WHERE active_listing_ids.id = listings.id
              )
            """
        ) as cursor:
            reactivated_count = cursor.rowcount
        await db.commit()

    changed_count = deactivated_count + reactivated_count
    logger.info(
        "Synchronized active listings: %s item IDs received, %s flags changed.",
        len(active_ids),
        changed_count,
    )
    return changed_count


async def get_unbound_snipes_due(seconds_window: int = 15) -> List[Dict[str, Any]]:
    """
    Find viable analyzed listings whose closing_date is approaching within seconds_window
    and have no successful or pending active_bids attached.
    """
    now = datetime.now(TORONTO_TIMEZONE)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT l.* 
            FROM listings l
            LEFT JOIN active_bids ab ON l.id = ab.listing_id AND ab.status IN ('PENDING', 'SNIPED', 'WON')
            WHERE l.is_viable = 1
              AND l.is_analyzed = 1
              AND l.is_active = 1
              AND ab.id IS NULL
            """
        ) as cursor:
            rows = await cursor.fetchall()

    due_listings = []
    for row in rows:
        listing = dict(row)
        try:
            closing_date = datetime.fromisoformat(listing["closing_date"])
        except (TypeError, ValueError):
            logger.warning(
                "Skipping lot %s with invalid closing_date %r.",
                listing["id"],
                listing["closing_date"],
            )
            continue

        if closing_date.tzinfo is None:
            closing_date = closing_date.replace(tzinfo=TORONTO_TIMEZONE)
        else:
            closing_date = closing_date.astimezone(TORONTO_TIMEZONE)

        remaining = closing_date - now
        if timedelta(seconds=-60) < remaining <= timedelta(seconds=seconds_window):
            due_listings.append(listing)

    return due_listings


async def update_listing_analysis(
    listing_id: str,
    estimated_value: float,
    cfo_net_profit: float,
    suggested_max_bid: float,
    is_viable: bool,
    rejection_reason: Optional[str] = None,
) -> None:
    """Update AI analysis result for a listing."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE listings
            SET estimated_value = ?,
                cfo_net_profit = ?,
                suggested_max_bid = ?,
                is_viable = ?,
                rejection_reason = ?,
                is_analyzed = 1
            WHERE id = ?
            """,
            (
                estimated_value,
                cfo_net_profit,
                suggested_max_bid,
                1 if is_viable else 0,
                rejection_reason or "",
                listing_id,
            ),
        )
        await db.commit()


async def get_pending_analysis_listings() -> List[Dict[str, Any]]:
    """Get all listings that haven't been analyzed by AI yet."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM listings WHERE is_analyzed = 0 AND is_active = 1"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def record_active_bid(
    listing_id: str,
    bid_amount: float,
    status: str = "PENDING",
    error_message: Optional[str] = None,
) -> int:
    """Record an attempted or placed bid into active_bids table."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO active_bids (listing_id, bid_amount, status, error_message)
            VALUES (?, ?, ?, ?)
            """,
            (listing_id, bid_amount, status, error_message),
        )
        bid_id = cursor.lastrowid
        await db.commit()
        return bid_id if bid_id is not None else 0


async def update_bid_status(
    bid_id: int, status: str, error_message: Optional[str] = None
) -> None:
    """Update status of a bid record."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE active_bids SET status = ?, error_message = ? WHERE id = ?",
            (status, error_message, bid_id),
        )
        await db.commit()


async def log_system_event(level: str, component: str, message: str) -> None:
    """Log internal system event into SQLite database."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO system_logs (level, component, message) VALUES (?, ?, ?)",
            (level, component, message),
        )
        await db.commit()
