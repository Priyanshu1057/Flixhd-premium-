"""
Background scheduler:
  - Sends subscription expiry reminders (every 12 hours)
  - Expires stale QR codes and notifies users (every 5 minutes)
  - Self-pings the service URL every 14 minutes to prevent free-tier sleep
"""
import asyncio
import logging
import urllib.request
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from database import get_expiring_orders, mark_reminder_sent, expire_stale_qr_orders
from config import SELF_PING_URL

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


def start_scheduler(bot):
    async def send_reminders():
        for days in (7, 3, 1):
            try:
                orders = await get_expiring_orders(days)
                for order in orders:
                    user_id = order.get("user_id")
                    if not user_id:
                        continue
                    service = order.get("service_name", "your subscription")
                    plan = order.get("plan_label", "")
                    end_date = order["subscription_end"].strftime("%d %b %Y")
                    order_id = str(order["_id"])

                    if days == 1:
                        urgency = "🚨 *Last day!*"
                    elif days == 3:
                        urgency = "⚠️ *3 days left!*"
                    else:
                        urgency = "📅 *7 days left*"

                    msg = (
                        f"{urgency}\n\n"
                        f"Your *{service}* ({plan}) subscription expires on *{end_date}*.\n\n"
                        f"Tap /start to renew and keep enjoying uninterrupted access! 🎉"
                    )
                    try:
                        await bot.send_message(chat_id=user_id, text=msg, parse_mode="Markdown")
                        await mark_reminder_sent(order_id, days)
                        logger.info(f"Sent {days}d reminder to user {user_id} for order {order_id}")
                    except Exception as e:
                        logger.error(f"Failed to send reminder to {user_id}: {e}")
            except Exception as e:
                logger.error(f"Reminder job error (days={days}): {e}")

    async def expire_qr_codes():
        try:
            stale_orders = await expire_stale_qr_orders()
            for order in stale_orders:
                user_id = order.get("user_id")
                if not user_id:
                    continue
                service = order.get("service_name", "your subscription")
                plan = order.get("plan_label", "")
                try:
                    await bot.send_message(
                        chat_id=user_id,
                        text=(
                            f"⏰ *QR Code Expired*\n\n"
                            f"Your payment QR for *{service}* ({plan}) has expired.\n\n"
                            f"Please type /start to generate a new one and complete your purchase."
                        ),
                        parse_mode="Markdown",
                    )
                    logger.info(f"Notified user {user_id} of expired QR for order {order['_id']}")
                except Exception as e:
                    logger.error(f"Failed to notify user {user_id} of QR expiry: {e}")
        except Exception as e:
            logger.error(f"QR expiry job error: {e}")

    async def self_ping():
        url = SELF_PING_URL
        if not url:
            return
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: urllib.request.urlopen(url, timeout=10),
            )
            logger.info(f"Self-ping OK → {url}")
        except Exception as e:
            logger.warning(f"Self-ping failed: {e}")

    scheduler.add_job(send_reminders, "interval", hours=12, id="subscription_reminders")
    scheduler.add_job(expire_qr_codes, "interval", minutes=5, id="qr_expiry")
    if SELF_PING_URL:
        scheduler.add_job(self_ping, "interval", minutes=14, id="self_ping")
        logger.info(f"Self-ping scheduled every 14 min → {SELF_PING_URL}")
    scheduler.start()
    logger.info("Scheduler started — reminders every 12h, QR expiry check every 5min.")
