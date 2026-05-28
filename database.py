import motor.motor_asyncio
from config import MONGODB_URI
from datetime import datetime, timedelta

client = motor.motor_asyncio.AsyncIOMotorClient(
    MONGODB_URI,
    tls=True,
    tlsAllowInvalidCertificates=True,
)
db = client["premium_bot"]

orders_col = db["orders"]
users_col = db["users"]
discounts_col = db["discounts"]
coupons_col = db["coupons"]


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

async def save_order(order: dict) -> str:
    order["created_at"] = datetime.utcnow()
    order["status"] = "pending"
    result = await orders_col.insert_one(order)
    return str(result.inserted_id)


async def set_qr_expiry(order_id: str, expires_at: datetime):
    from bson import ObjectId
    await orders_col.update_one(
        {"_id": ObjectId(order_id)},
        {"$set": {"qr_expires_at": expires_at}},
    )


async def expire_stale_qr_orders() -> list[dict]:
    """Mark all pending orders whose QR has expired and return them so the bot can notify users."""
    now = datetime.utcnow()
    cursor = orders_col.find({
        "status": {"$in": ["pending"]},
        "qr_expires_at": {"$lt": now},
    })
    stale = await cursor.to_list(length=None)
    if stale:
        ids = [doc["_id"] for doc in stale]
        await orders_col.update_many(
            {"_id": {"$in": ids}},
            {"$set": {"status": "qr_expired", "updated_at": now}},
        )
    return stale


async def update_order_status(order_id: str, status: str, screenshot_file_id: str = None):
    from bson import ObjectId
    update = {"status": status, "updated_at": datetime.utcnow()}
    if screenshot_file_id:
        update["screenshot_file_id"] = screenshot_file_id
    await orders_col.update_one({"_id": ObjectId(order_id)}, {"$set": update})


async def approve_order(order_id: str, plan_key: str):
    """Mark order approved and set subscription start/end dates.
    If the user already has an active subscription for the same service,
    the new days are added on top of the existing end date (extension).
    Returns (end_date, extended: bool, old_end_date | None).
    """
    from bson import ObjectId
    from config import PLAN_DURATIONS
    days = PLAN_DURATIONS.get(plan_key, 30)
    now = datetime.utcnow()

    # Fetch the order to get user_id + service_key for the extension check
    order = await orders_col.find_one({"_id": ObjectId(order_id)})
    user_id = order.get("user_id") if order else None
    service_key = order.get("service_key") if order else None

    # Check for an existing active subscription on the same service
    old_end_date = None
    extended = False
    base_date = now  # start counting from today by default
    if user_id and service_key:
        existing = await get_active_subscription_for_service(user_id, service_key)
        if existing and existing["subscription_end"] > now:
            old_end_date = existing["subscription_end"]
            base_date = old_end_date  # extend from current expiry
            extended = True

    end_date = base_date + timedelta(days=days)

    await orders_col.update_one(
        {"_id": ObjectId(order_id)},
        {"$set": {
            "status": "approved",
            "updated_at": now,
            "subscription_start": now,
            "subscription_end": end_date,
            "extended": extended,
            "extended_from": old_end_date,
            "reminder_7_sent": False,
            "reminder_3_sent": False,
            "reminder_1_sent": False,
        }},
    )
    return end_date, extended, old_end_date


async def get_order(order_id: str) -> dict | None:
    from bson import ObjectId
    return await orders_col.find_one({"_id": ObjectId(order_id)})


async def get_expiring_orders(days_ahead: int) -> list[dict]:
    """Get approved orders whose subscription ends exactly `days_ahead` days from now."""
    now = datetime.utcnow()
    window_start = now + timedelta(days=days_ahead - 1)
    window_end = now + timedelta(days=days_ahead)
    reminder_field = f"reminder_{days_ahead}_sent"
    cursor = orders_col.find({
        "status": "approved",
        "subscription_end": {"$gte": window_start, "$lt": window_end},
        reminder_field: False,
    })
    return await cursor.to_list(length=None)


async def mark_reminder_sent(order_id: str, days: int):
    from bson import ObjectId
    await orders_col.update_one(
        {"_id": ObjectId(order_id)},
        {"$set": {f"reminder_{days}_sent": True}},
    )


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

async def upsert_user(user_id: int, username: str, full_name: str) -> bool:
    """Returns True if this is a brand-new user, False if already existed."""
    result = await users_col.update_one(
        {"user_id": user_id},
        {
            "$set": {
                "username": username,
                "full_name": full_name,
                "last_seen": datetime.utcnow(),
            },
            "$setOnInsert": {"joined_at": datetime.utcnow()},
        },
        upsert=True,
    )
    return result.upserted_id is not None


async def get_all_user_ids() -> list[int]:
    cursor = users_col.find({}, {"user_id": 1})
    docs = await cursor.to_list(length=None)
    return [d["user_id"] for d in docs]


# ---------------------------------------------------------------------------
# Coupons
# ---------------------------------------------------------------------------

async def create_coupon(code: str, percent: int, max_uses: int | None):
    existing = await coupons_col.find_one({"code": code.upper()})
    if existing:
        return False, "Coupon code already exists."
    await coupons_col.insert_one({
        "code": code.upper(),
        "percent": percent,
        "max_uses": max_uses,
        "uses": 0,
        "active": True,
        "created_at": datetime.utcnow(),
    })
    return True, None


async def get_coupon(code: str) -> dict | None:
    return await coupons_col.find_one({"code": code.upper(), "active": True})


async def use_coupon(code: str):
    await coupons_col.update_one(
        {"code": code.upper()},
        {"$inc": {"uses": 1}},
    )


async def list_coupons() -> list[dict]:
    cursor = coupons_col.find({"active": True})
    return await cursor.to_list(length=None)


async def delete_coupon(code: str) -> bool:
    result = await coupons_col.update_one(
        {"code": code.upper()},
        {"$set": {"active": False}},
    )
    return result.modified_count > 0


# ---------------------------------------------------------------------------
# User subscriptions
# ---------------------------------------------------------------------------

async def get_user_active_orders(user_id: int) -> list[dict]:
    """Return all approved (active) orders for a user, newest first."""
    now = datetime.utcnow()
    cursor = orders_col.find({
        "user_id": user_id,
        "status": "approved",
        "subscription_end": {"$gte": now},
    }).sort("subscription_end", -1)
    return await cursor.to_list(length=None)


async def get_active_subscription_for_service(user_id: int, service_key: str) -> dict | None:
    """Return the latest active subscription for a user for a specific service, or None."""
    now = datetime.utcnow()
    return await orders_col.find_one(
        {
            "user_id": user_id,
            "service_key": service_key,
            "status": "approved",
            "subscription_end": {"$gte": now},
        },
        sort=[("subscription_end", -1)],
    )


async def get_pending_screenshot_orders(limit: int = 20) -> list[dict]:
    """Return orders with screenshot received, awaiting admin review."""
    cursor = orders_col.find(
        {"status": "screenshot_received"}
    ).sort("created_at", 1).limit(limit)
    return await cursor.to_list(length=None)


async def manually_create_approved_order(
    user_id: int,
    service_key: str,
    service_name: str,
    plan_key: str,
    plan_label: str,
    amount: int,
    days: int = 0,
    end_date: datetime | None = None,
) -> str:
    """Admin: create and immediately approve an order for a user.
    Pass either `days` (integer) or an explicit `end_date` datetime.
    """
    from bson import ObjectId
    now = datetime.utcnow()
    if end_date is None:
        end_date = now + timedelta(days=days)
    doc = {
        "user_id": user_id,
        "service_key": service_key,
        "service_name": service_name,
        "plan_key": plan_key,
        "plan_label": plan_label,
        "amount": amount,
        "status": "approved",
        "created_at": now,
        "updated_at": now,
        "subscription_start": now,
        "subscription_end": end_date,
        "reminder_7_sent": False,
        "reminder_3_sent": False,
        "reminder_1_sent": False,
        "manual": True,
    }
    result = await orders_col.insert_one(doc)
    return str(result.inserted_id), end_date


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

async def get_report_data() -> dict:
    total = await orders_col.count_documents({})
    approved = await orders_col.count_documents({"status": "approved"})
    pending = await orders_col.count_documents({"status": "pending"})
    screenshot_received = await orders_col.count_documents({"status": "screenshot_received"})
    rejected = await orders_col.count_documents({"status": "rejected"})
    on_hold = await orders_col.count_documents({"status": "on_hold"})
    total_users = await users_col.count_documents({})

    # Revenue from approved orders
    pipeline = [
        {"$match": {"status": "approved"}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
    ]
    rev_cursor = orders_col.aggregate(pipeline)
    rev_docs = await rev_cursor.to_list(length=1)
    revenue = rev_docs[0]["total"] if rev_docs else 0

    # Subscriptions ending in next 7 days
    now = datetime.utcnow()
    window_end = now + timedelta(days=7)
    cursor = orders_col.find({
        "status": "approved",
        "subscription_end": {"$gte": now, "$lt": window_end},
    }).sort("subscription_end", 1)
    ending_soon = await cursor.to_list(length=20)

    return {
        "total": total,
        "approved": approved,
        "pending": pending + screenshot_received,
        "rejected": rejected,
        "on_hold": on_hold,
        "revenue": revenue,
        "total_users": total_users,
        "ending_soon": ending_soon,
    }
