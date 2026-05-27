"""
In-memory discount store.
Discounts are percentage values (0-100) keyed by service_key or "all".
They reset when the bot restarts; for persistence they are also saved to MongoDB.
"""
import logging
from database import db

logger = logging.getLogger(__name__)

discounts_col = db["discounts"]

_active_discounts: dict[str, int] = {}


async def load_discounts():
    """Load active discounts from DB into memory on startup."""
    _active_discounts.clear()
    async for doc in discounts_col.find({"active": True}):
        _active_discounts[doc["key"]] = doc["percent"]
    logger.info(f"Loaded discounts: {_active_discounts}")


async def set_discount(key: str, percent: int):
    """Set a discount for 'all' or a specific service key."""
    _active_discounts[key] = percent
    await discounts_col.update_one(
        {"key": key},
        {"$set": {"key": key, "percent": percent, "active": True}},
        upsert=True,
    )


async def remove_discount(key: str):
    """Remove a discount."""
    _active_discounts.pop(key, None)
    await discounts_col.update_one({"key": key}, {"$set": {"active": False}})


async def get_all_discounts() -> dict[str, int]:
    return dict(_active_discounts)


def get_discounted_price(service_key: str, original_price: int) -> tuple[int, int | None]:
    """
    Returns (final_price, discount_percent_applied).
    Service-specific discount takes priority over 'all'.
    """
    percent = _active_discounts.get(service_key) or _active_discounts.get("all")
    if percent:
        discounted = round(original_price * (1 - percent / 100))
        return discounted, percent
    return original_price, None
