import asyncio
import logging
import math
import re
import sys

from core.config import MAX_CAPITAL_EXPOSURE
from core.database import (
    init_db,
    save_listing,
    update_listing_analysis,
    get_pending_analysis_listings,
    sync_active_listings,
    authorize_bid,
    get_unbound_snipes_due,
    record_active_bid,
    update_bid_status,
    log_system_event,
)
from core.auth_manager import auth_manager
from scrapers.gcsurplus import gcsurplus_scraper
from bot.discord_app import discord_notifier
from ai.analyzer import cfo_evaluator

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("gcsurplus_sniper.main")
COMMODITY_PATTERN = re.compile(r"Commodity:\s*\d+\s*-\s*(.*?)(?:\s+Location:|$)")
MINIMUM_BID_INCREMENT = 1.0


def _is_bid_within_ai_cap(proposed_bid: float, suggested_max_bid: float) -> bool:
    if (
        not math.isfinite(proposed_bid)
        or not math.isfinite(suggested_max_bid)
        or suggested_max_bid <= 0
        or proposed_bid > suggested_max_bid
    ):
        logger.warning(
            "Plafond IA dépassé : enchère proposée %.2f, plafond IA %.2f. "
            "Enchère annulée.",
            proposed_bid,
            suggested_max_bid,
        )
        return False
    return True


async def process_scraped_listings(listings: list[dict[str, object]]) -> tuple[int, int]:
    """Persist lots and analyze newly inserted or previously pending records."""
    inserted_count = 0

    for listing in listings:
        current_price = float(listing["current_price"])
        raw_data = str(listing["raw_data"])
        category_match = COMMODITY_PATTERN.search(raw_data)
        database_listing = {
            "id": str(listing["item_id"]),
            "title": str(listing["title"]),
            "category": (
                category_match.group(1).strip() if category_match else "GC Surplus"
            ),
            "current_bid": current_price,
            "minimum_bid": float(listing["minimum_bid"]),
            "location": str(listing["location"]),
            "closing_date": str(listing["closing_date"]),
            "url": str(listing["url"]),
            "description": raw_data,
        }

        inserted = await save_listing(database_listing)
        if not inserted:
            continue

        inserted_count += 1

    pending_listings = await get_pending_analysis_listings()
    if not pending_listings:
        return inserted_count, 0

    session = await auth_manager.get_session()
    analyzed_count = 0
    for database_listing in pending_listings:
        logger.info(
            "Lancement de l'analyse IA pour le lot en attente %s "
            "(current_bid=%s, type=%s).",
            database_listing["id"],
            database_listing["current_bid"],
            type(database_listing["current_bid"]).__name__,
        )
        analysis = await cfo_evaluator.analyze_listing(
            database_listing,
            session=session,
        )
        await update_listing_analysis(
            listing_id=str(analysis["listing_id"]),
            estimated_value=float(analysis["estimated_value"]),
            cfo_net_profit=float(analysis["cfo_net_profit"]),
            suggested_max_bid=float(analysis["suggested_max_bid"]),
            is_viable=bool(analysis["is_viable"]),
            rejection_reason=(
                str(analysis["rejection_reason"])
                if analysis["rejection_reason"] is not None
                else None
            ),
        )
        analyzed_count += 1
        logger.info(
            "Analyse terminée pour %s : viable=%s, profit net=%s.",
            database_listing["id"],
            analysis["is_viable"],
            analysis["cfo_net_profit"],
        )

    return inserted_count, analyzed_count


async def periodic_scraper_worker(interval_seconds: int = 300) -> None:
    """
    Background worker that runs the GC Surplus scraper periodically (e.g. every 5 minutes).
    Sends Discord notifications for newly discovered viable deals.
    """
    logger.info("Starting periodic scraper worker...")
    while True:
        try:
            logger.info("Triggering periodic scraping cycle...")
            scraped_count, inserted_count, analyzed_count = (
                await run_scraper_cycle_once()
            )

            await log_system_event(
                "INFO",
                "ScraperWorker",
                f"Completed cycle. {scraped_count} scraped, "
                f"{inserted_count} inserted, {analyzed_count} analyzed.",
            )

        except Exception as e:
            logger.error(f"Error in periodic scraper worker: {e}", exc_info=True)
            await log_system_event("ERROR", "ScraperWorker", f"Worker error: {e}")

        await asyncio.sleep(interval_seconds)


async def run_scraper_cycle_once() -> tuple[int, int, int]:
    """Scrape once, synchronize active IDs, then persist and analyze listings."""
    discovered_listings = await gcsurplus_scraper.run_scrape_cycle()
    active_item_ids = list(
        {str(listing["item_id"]) for listing in discovered_listings}
    )
    if active_item_ids:
        await sync_active_listings(active_item_ids)
    else:
        logger.warning(
            "Skipping active-listing synchronization for an empty scraper result."
        )

    inserted_count, analyzed_count = await process_scraped_listings(
        discovered_listings
    )
    logger.info(
        "Cycle terminé : %s lots récupérés, %s nouveaux enregistrés, "
        "%s analyses IA terminées.",
        len(discovered_listings),
        inserted_count,
        analyzed_count,
    )
    return len(discovered_listings), inserted_count, analyzed_count


async def snipe_watcher_worker(poll_interval: int = 5) -> None:
    """
    Background worker running at high frequency (poll_interval=5s).
    Monitors upcoming closing listings at T-15 seconds.
    Executes bid authorization check (Risk Control) and places snipe bid if exposure <= MAX_CAPITAL_EXPOSURE.
    """
    logger.info("Starting snipe watcher worker (T-15s precision)...")
    while True:
        try:
            # Query items closing within 15 seconds that are viable and not yet bid on
            due_snipes = await get_unbound_snipes_due(seconds_window=15)

            for item in due_snipes:
                listing_id = item["id"]
                title = item["title"]
                url = item["url"]
                suggested_bid = float(item.get("suggested_max_bid", 0.0))
                current_bid = float(item.get("current_bid", 0.0))
                target_bid_amount = current_bid + MINIMUM_BID_INCREMENT
                if not _is_bid_within_ai_cap(target_bid_amount, suggested_bid):
                    continue

                logger.info(
                    f"[T-15s Snipe Triggered] Listing: {listing_id} | "
                    f"Target Bid: ${target_bid_amount:.2f} CAD"
                )

                # STRICT FINANCIAL GOVERNANCE: Check Capital Exposure
                is_authorized = await authorize_bid(target_bid_amount)
                if not is_authorized:
                    error_msg = f"Exposure limit reached (Max=${MAX_CAPITAL_EXPOSURE:.2f}). Bid rejected."
                    logger.warning(f"Snipe blocked for {listing_id}: {error_msg}")
                    bid_id = await record_active_bid(
                        listing_id=listing_id,
                        bid_amount=target_bid_amount,
                        status="REJECTED_EXPOSURE",
                        error_message=error_msg,
                    )
                    await discord_notifier.notify_snipe_executed(
                        listing_id=listing_id,
                        title=title,
                        bid_amount=target_bid_amount,
                        status="REJECTED_EXPOSURE",
                        details=error_msg,
                    )
                    continue

                # Record pending bid attempt
                bid_id = await record_active_bid(
                    listing_id=listing_id,
                    bid_amount=target_bid_amount,
                    status="PENDING",
                )

                # Execute Snipe via Auth Session Manager
                success = await auth_manager.place_snipe_bid(
                    listing_url=url,
                    bid_amount=target_bid_amount,
                )

                if success is None:
                    message = (
                        "Résultat du POST ambigu : aucune confirmation de montant "
                        "ni erreur active. Tentative conservée en attente."
                    )
                    await update_bid_status(
                        bid_id,
                        status="PENDING",
                        error_message=message,
                    )
                    logger.warning(
                        "Bid result is ambiguous for listing %s; "
                        "automatic retry is suppressed.",
                        listing_id,
                    )
                    await discord_notifier.notify_snipe_executed(
                        listing_id=listing_id,
                        title=title,
                        bid_amount=target_bid_amount,
                        status="PENDING",
                        details=message,
                    )
                elif success:
                    await update_bid_status(bid_id, status="SNIPED")
                    logger.info(f"SNIPE SUCCESSFUL for listing {listing_id} @ ${target_bid_amount:.2f}")
                    await discord_notifier.notify_snipe_executed(
                        listing_id=listing_id,
                        title=title,
                        bid_amount=target_bid_amount,
                        status="SNIPED",
                        details="Enchère placée avec succès à T-15s.",
                    )
                else:
                    await update_bid_status(bid_id, status="FAILED", error_message="Bid POST failed or rejected")
                    logger.error(f"SNIPE FAILED for listing {listing_id}")
                    await discord_notifier.notify_snipe_executed(
                        listing_id=listing_id,
                        title=title,
                        bid_amount=target_bid_amount,
                        status="FAILED",
                        details="Le serveur GC Surplus a rejeté l'offre.",
                    )

        except Exception as e:
            logger.error(f"Error in snipe watcher worker: {e}", exc_info=True)
            await log_system_event("ERROR", "SnipeWatcher", f"Watcher error: {e}")

        await asyncio.sleep(poll_interval)


async def main() -> None:
    """
    Main Micro-Service entrypoint:
    1. Initialize database schema & WAL mode.
    2. Authenticate session with GC Surplus.
    3. Launch concurrent asyncio workers (scraper & snipe watcher).
    """
    try:
        logger.info("Initializing gcsurplus_sniper microservice...")
        await init_db()

        # Authenticate session
        auth_success = await auth_manager.authenticate()
        if not auth_success:
            logger.warning("Running without active GC Surplus session. Bidding will be simulated.")

        logger.info("Starting concurrent asyncio workers...")
        await asyncio.gather(
            periodic_scraper_worker(interval_seconds=300),
            # Désactivé explicitement : le worker pourrait soumettre des enchères réelles.
            # snipe_watcher_worker(poll_interval=5),
        )
    finally:
        await auth_manager.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("gcsurplus_sniper service stopped by user.")
    except Exception as exc:
        logger.critical(f"Unhandled exception in service: {exc}", exc_info=True)
        sys.exit(1)
