import logging
from typing import Dict, Any, Optional
import aiohttp
from core.config import DISCORD_WEBHOOK_URL
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger("gcsurplus_sniper.discord")


class DiscordNotifier:
    """
    Sends rich Discord Webhook embeds with item images, current bids, estimated resale,
    CFO net profit calculation, and status alerts.
    """

    def __init__(self, webhook_url: Optional[str] = None) -> None:
        self.webhook_url = webhook_url or DISCORD_WEBHOOK_URL

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=False,
    )
    async def send_embed(self, embed: Dict[str, Any]) -> bool:
        """Send embed payload to Discord Webhook."""
        if not self.webhook_url:
            logger.warning("DISCORD_WEBHOOK_URL not configured. Skipping Discord notification.")
            return False

        payload = {
            "username": "GC Surplus Arbitrage Sniper",
            "avatar_url": "https://www.gcsurplus.ca/images/logo.gif",
            "embeds": [embed],
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(self.webhook_url, json=payload) as resp:
                if resp.status in (200, 204):
                    logger.info("Discord alert sent successfully.")
                    return True
                else:
                    logger.error(f"Failed to send Discord alert: HTTP {resp.status}")
                    return False

    async def notify_new_deal(self, listing: Dict[str, Any]) -> bool:
        """
        Sends an enriched deal alert when a high-margin opportunity passes CFO validation.
        """
        listing_id = listing.get("id", "N/A")
        title = listing.get("title", "Unknown Item")
        category = listing.get("category", "General")
        current_bid = float(listing.get("current_bid", 0.0))
        estimated_value = float(listing.get("estimated_value", 0.0))
        cfo_net_profit = float(listing.get("cfo_net_profit", 0.0))
        suggested_max_bid = float(listing.get("suggested_max_bid", 0.0))
        url = listing.get("url", "#")
        image_url = listing.get("image_url", "")
        closing_date = listing.get("closing_date", "N/A")

        embed = {
            "title": f"🚀 OPPORTUNITÉ D'ARBITRAGE DÉTECTÉE: {title[:80]}",
            "url": url,
            "color": 3066993,  # Green
            "fields": [
                {
                    "name": "📂 Catégorie",
                    "value": f"`{category}`",
                    "inline": True,
                },
                {
                    "name": "💵 Offre Actuelle",
                    "value": f"**${current_bid:,.2f} CAD**",
                    "inline": True,
                },
                {
                    "name": "📈 Revente Estimée (Marché)",
                    "value": f"**${estimated_value:,.2f} CAD**",
                    "inline": True,
                },
                {
                    "name": "💰 Profit Net CFO Estimé",
                    "value": f"**+${cfo_net_profit:,.2f} CAD**",
                    "inline": True,
                },
                {
                    "name": "🎯 Enchère Maximale Suggérée",
                    "value": f"**${suggested_max_bid:,.2f} CAD**",
                    "inline": True,
                },
                {
                    "name": "⏰ Date de Clôture",
                    "value": f"`{closing_date}`",
                    "inline": True,
                },
            ],
            "footer": {
                "text": f"GC Surplus Sniper ID: {listing_id} | Validation Formule CFO Strict"
            },
        }

        if image_url:
            embed["image"] = {"url": image_url}

        return await self.send_embed(embed)

    async def notify_snipe_executed(
        self,
        listing_id: str,
        title: str,
        bid_amount: float,
        status: str,
        details: Optional[str] = None,
    ) -> bool:
        """
        Sends notification after a snipe attempt (T-15 seconds).
        """
        is_success = status in ("SNIPED", "WON")
        color = 3066993 if is_success else 15158332  # Green or Red

        embed = {
            "title": f"{'✅ ENCHÈRE SNIPE EXÉCUTÉE' if is_success else '⚠️ ÉCHEC SNIPE / REJET DE RISQUE'}",
            "color": color,
            "fields": [
                {"name": "ID Produit", "value": f"`{listing_id}`", "inline": True},
                {"name": "Article", "value": title[:100], "inline": False},
                {"name": "Montant Engagé", "value": f"**${bid_amount:,.2f} CAD**", "inline": True},
                {"name": "Statut Execution", "value": f"`{status}`", "inline": True},
                {"name": "Détails / Message", "value": details or "Aucun détail supplémentaire", "inline": False},
            ],
            "footer": {"text": "Système d'Arbitrage Automatisé gcsurplus_sniper"},
        }
        return await self.send_embed(embed)


# Global instance
discord_notifier = DiscordNotifier()
