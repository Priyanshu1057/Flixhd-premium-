import logging
import asyncio
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from html import escape
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)
from datetime import datetime, timedelta
from config import TELEGRAM_BOT_TOKEN, ADMIN_CHANNEL_ID, ADMIN_USER_ID, SERVICES, QR_EXPIRY_MINUTES, SUPPORT_USERNAME, LOG_CHANNEL_ID, SELF_PING_URL
from database import (
    save_order, update_order_status, approve_order,
    upsert_user, get_order, get_all_user_ids, get_report_data,
    create_coupon, get_coupon, use_coupon, list_coupons, delete_coupon,
    set_qr_expiry,
    get_user_active_orders, get_pending_screenshot_orders, manually_create_approved_order,
    get_active_subscription_for_service,
)
from qr_generator import generate_upi_qr
from discounts import load_discounts, set_discount, remove_discount, get_all_discounts, get_discounted_price
from scheduler import start_scheduler

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

SELECT_SERVICE, SELECT_BOT, SELECT_PLAN, AWAITING_COUPON, AWAITING_SCREENSHOT = range(5)


# ---------------------------------------------------------------------------
# Health-check HTTP server (required for Render web service)
# ---------------------------------------------------------------------------

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass  # suppress noisy access logs


def start_health_server():
    port = int(os.environ.get("PORT", 8080))
    try:
        server = HTTPServer(("0.0.0.0", port), _HealthHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        logger.info(f"Health server listening on port {port}")
    except OSError as e:
        logger.warning(f"Health server could not start: {e}")


# ---------------------------------------------------------------------------
# Log-channel helper — silently mirrors events to LOG_CHANNEL_ID if set
# ---------------------------------------------------------------------------

async def log_to_channel(bot, text: str):
    if not LOG_CHANNEL_ID:
        return
    try:
        await bot.send_message(chat_id=LOG_CHANNEL_ID, text=text, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"log_to_channel failed: {e}")

PENDING_ORDERS: dict[int, dict] = {}


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_USER_ID


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------

async def send_payment_qr(
    update_or_message,
    context: ContextTypes.DEFAULT_TYPE,
    service_key: str,
    plan_key: str,
    coupon_code: str | None,
    coupon_pct: int | None,
):
    """Save order, generate QR, send to user, register pending order."""
    service = SERVICES[service_key]
    plan = service["plans"][plan_key]
    user = update_or_message.effective_user if hasattr(update_or_message, "effective_user") else update_or_message.from_user

    base_price = plan["price"]
    # Apply store-wide / service discount first
    price_after_discount, discount_pct = get_discounted_price(service_key, base_price)

    # Then apply coupon on top
    if coupon_pct:
        final_price = round(price_after_discount * (1 - coupon_pct / 100))
    else:
        final_price = price_after_discount

    order_data = {
        "user_id": user.id,
        "username": user.username or "",
        "full_name": user.full_name,
        "service_key": service_key,
        "service_name": service["name"],
        "plan_key": plan_key,
        "plan_label": plan["label"],
        "amount": final_price,
        "original_amount": base_price,
        "discount_percent": discount_pct,
        "coupon_code": coupon_code,
        "coupon_percent": coupon_pct,
    }

    try:
        order_id = await save_order(order_data)
    except Exception as e:
        logger.error(f"DB error saving order: {e}")
        msg = getattr(update_or_message, "message", update_or_message)
        await msg.reply_text("⚠️ A database error occurred. Please try again later.")
        return ConversationHandler.END

    # Set QR expiry
    expires_at = datetime.utcnow() + timedelta(minutes=QR_EXPIRY_MINUTES)
    try:
        await set_qr_expiry(order_id, expires_at)
    except Exception as e:
        logger.error(f"DB error setting QR expiry: {e}")

    qr_buf = generate_upi_qr(final_price, service["name"], plan["label"], order_id)

    savings_lines = []
    if discount_pct:
        savings_lines.append(f"🏷 Store discount: {discount_pct}%")
    if coupon_pct:
        savings_lines.append(f"🎟 Coupon `{coupon_code}`: {coupon_pct}%")
    savings_text = ("\n" + "\n".join(savings_lines) + "\n") if savings_lines else ""

    original_line = f"~~₹{base_price}~~ → " if final_price != base_price else ""

    # Format expiry time in IST (UTC+5:30)
    expires_at_ist = expires_at + timedelta(hours=5, minutes=30)
    expiry_str = expires_at_ist.strftime("%I:%M %p IST")

    caption = (
        f"💳 *Payment Details*\n\n"
        f"📦 Service: {service['name']}\n"
        f"📅 Plan: {plan['label']}\n"
        f"{savings_text}"
        f"💰 Amount: {original_line}₹{final_price}\n\n"
        f"•─────•─────────•─────•\n\n"
        f"🏷️ ᴘᴀʏᴍᴇɴᴛ ᴍᴇᴛʜᴏᴅ\n"
        f"💸 ᴜᴘɪ ɪᴅ → `{__import__('config').UPI_ID}`\n\n"
        f"1️⃣ Scan the QR code using any UPI app\n"
        f"2️⃣ Pay ₹{final_price}\n"
        f"3️⃣ Screenshot the payment confirmation\n"
        f"4️⃣ Send the screenshot here\n\n"
        f"‼️ ᴍᴜꜱᴛ sᴇɴᴅ sᴄʀᴇᴇɴsʜᴏᴛ ᴀꜰᴛᴇʀ ᴘᴀʏᴍᴇɴᴛ\n"
        f"‼️ ɢɪᴠᴇ ᴜs ꜱᴏᴍᴇ ᴛɪᴍᴇ ᴛᴏ ᴀᴄᴛɪᴠᴀᴛᴇ ʏᴏᴜʀ ᴘʀᴇᴍɪᴜᴍ\n\n"
        f"•─────•─────────•─────•\n\n"
        f"⏰ *QR expires at {expiry_str}* (valid for {QR_EXPIRY_MINUTES} min)\n"
        f"⏳ Awaiting your payment screenshot..."
    )

    msg = getattr(update_or_message, "message", update_or_message)
    await msg.reply_photo(
        photo=InputFile(qr_buf, filename="payment_qr.png"),
        caption=caption,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel Order", callback_data="cancel_order")]
        ]),
    )

    PENDING_ORDERS[user.id] = {
        "order_id": order_id,
        "amount": final_price,
        "expires_at": expires_at,
    }
    return order_id


# ---------------------------------------------------------------------------
# User conversation
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        is_new = await upsert_user(user.id, user.username or "", user.full_name)
    except Exception as e:
        logger.error(f"DB error saving user: {e}")
        is_new = False

    if is_new:
        uname = f"@{user.username}" if user.username else "no username"
        await log_to_channel(
            context.bot,
            f"🆕 <b>New User Joined</b>\n"
            f"👤 <b>Name:</b> {user.full_name}\n"
            f"🔗 <b>Username:</b> {uname}\n"
            f"🆔 <b>ID:</b> <code>{user.id}</code>",
        )

    keyboard = [
        [InlineKeyboardButton(info["name"], callback_data=f"service:{key}")]
        for key, info in SERVICES.items()
    ]
    keyboard.append([InlineKeyboardButton("ℹ️ ʜᴏᴡ ɪᴛ ᴡᴏʀᴋs", callback_data="help")])

    name = user.first_name or "there"
    await update.message.reply_text(
        f"👋 ʜᴇʏ <b>{name}</b>!\n\n"
        "🌟 ᴡᴇʟᴄᴏᴍᴇ ᴛᴏ <b>FlixHD Premium</b>\n\n"
        "ɢᴇᴛ ᴜɴʟɪᴍɪᴛᴇᴅ ᴀᴄᴄᴇss ᴛᴏ ᴍᴏᴠɪᴇs, sᴇʀɪᴇs & ᴇxᴄʟᴜsɪᴠᴇ ᴄᴏɴᴛᴇɴᴛ ᴀᴛ ᴛʜᴇ ʙᴇsᴛ ᴘʀɪᴄᴇs.\n\n"
        "•─────•─────────•─────•\n\n"
        "👇 ᴄʜᴏᴏsᴇ ᴀ sᴇʀᴠɪᴄᴇ ᴛᴏ ɢᴇᴛ sᴛᴀʀᴛᴇᴅ",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )
    return SELECT_SERVICE


async def select_service(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "help":
        await query.edit_message_text(
            "ℹ️ *ʜᴏᴡ ɪᴛ ᴡᴏʀᴋs:*\n\n"
            "1️⃣ sᴇʟᴇᴄᴛ ʏᴏᴜʀ ʙᴏᴛ ᴘʟᴀɴ\n"
            "2️⃣ ᴄʜᴏᴏsᴇ ᴀ ᴅᴜʀᴀᴛɪᴏɴ\n"
            "3️⃣ ᴇɴᴛᴇʀ ᴄᴏᴜᴘᴏɴ ᴄᴏᴅᴇ ɪꜰ ʏᴏᴜ ʜᴀᴠᴇ ᴏɴᴇ\n"
            "4️⃣ sᴄᴀɴ ᴛʜᴇ ᴜᴘɪ ǫʀ & ᴘᴀʏ\n"
            "5️⃣ sᴇɴᴅ ᴘᴀʏᴍᴇɴᴛ sᴄʀᴇᴇɴsʜᴏᴛ\n"
            "6️⃣ ᴘʀᴇᴍɪᴜᴍ ᴀᴄᴛɪᴠᴀᴛᴇᴅ ✅\n\n"
            "Type /start to begin.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    service_key = query.data.split(":")[1]
    service = SERVICES.get(service_key)
    if not service:
        await query.edit_message_text("❌ Invalid service. Type /start to try again.")
        return ConversationHandler.END

    context.user_data["service_key"] = service_key

    # Check for existing active subscription on this service
    existing_sub = None
    try:
        existing_sub = await get_active_subscription_for_service(update.effective_user.id, service_key)
    except Exception:
        pass

    keyboard = []
    for plan_key, plan in service["plans"].items():
        final_price, discount_pct = get_discounted_price(service_key, plan["price"])
        if discount_pct:
            label = f"◉ {plan['label']} — ₹{plan['price']} ➜ ₹{final_price} ({discount_pct}% off)"
        else:
            label = f"◉ {plan['label']} — ₹{final_price}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"plan:{plan_key}")])
    keyboard.append([InlineKeyboardButton("⬅️ ʙᴀᴄᴋ", callback_data="back")])
    keyboard.append([InlineKeyboardButton("❌ ᴄᴀɴᴄᴇʟ", callback_data="cancel_order")])

    # Build existing-subscription notice (shown on all service screens)
    existing_notice = ""
    if existing_sub:
        till = existing_sub["subscription_end"].strftime("%d %b %Y")
        existing_notice = (
            f"\n\n⚠️ <b>You already have an active subscription</b>\n"
            f"📅 Valid till: <b>{till}</b>\n"
            f"Purchasing again will <b>extend</b> your access from that date."
        )

    if service_key == "adult":
        await query.edit_message_text(
            f"<b>{escape(service['name'])}</b>\n\n"
            "💎 𝖯𝗋𝖾𝗆𝗂𝗎𝗆 𝖲𝗎𝖻𝗌𝖼𝗋𝗂𝗉𝗍𝗂𝗈𝗇 𝖯𝗅𝖺𝗇𝗌\n\n"
            "♻️ 𝖡𝖾𝗇𝖾𝖿𝗂𝗍𝗌:\n"
            "✅ 𝖣𝖺𝗂𝗅𝗒 𝖫𝗂𝗆𝗂𝗍: 50050 𝖥𝗂𝗅𝖾𝗌 (𝖵𝗌 5 𝖥𝗋𝖾𝖾)\n"
            "✅ 𝖭𝗈 𝖳𝗂𝗆𝖾 𝖦𝖺𝗉\n"
            "✅ 𝖠𝖼𝖼𝖾𝗌𝗌 𝗍𝗈 𝖯𝗋𝖾𝗆𝗂𝗎𝗆 𝖢𝗈𝗇𝗍𝖾𝗇𝗍\n"
            "✅ 𝖧𝗂𝗀𝗁 𝖲𝗉𝖾𝖾𝖽\n\n"
            "•─────•─────────•─────•\n\n"
            f"💸 sᴇʟᴇᴄᴛ ʏᴏᴜʀ ᴘʟᴀɴ 👇{existing_notice}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return SELECT_PLAN

    elif service_key == "movie_single":
        # Bot selection first — show inline buttons for each bot
        bots = service.get("bots", [])
        bot_keyboard = [
            [InlineKeyboardButton(f"🤖 {b}", callback_data=f"bot:{b}")]
            for b in bots
        ]
        bot_keyboard.append([InlineKeyboardButton("⬅️ ʙᴀᴄᴋ", callback_data="back")])
        bot_keyboard.append([InlineKeyboardButton("❌ ᴄᴀɴᴄᴇʟ", callback_data="cancel_order")])
        await query.edit_message_text(
            f"<b>{escape(service['name'])}</b>\n\n"
            f"🎯 ᴄʜᴏᴏsᴇ ᴛʜᴇ ʙᴏᴛ ʏᴏᴜ ᴡᴀɴᴛ ᴀᴄᴄᴇss ᴛᴏ 👇{existing_notice}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(bot_keyboard),
        )
        return SELECT_BOT

    elif service_key == "movie_both":
        bots = service.get("bots", [])
        bot_lines = "\n".join(f"🤖 {escape(b)}" for b in bots)
        await query.edit_message_text(
            f"<b>{escape(service['name'])}</b>\n\n"
            "✅ ɢᴇᴛ ᴀᴄᴄᴇss ᴛᴏ <b>ʙᴏᴛʜ</b> ʙᴏᴛs:\n"
            f"{bot_lines}\n\n"
            "•─────•─────────•─────•\n\n"
            f"💸 sᴇʟᴇᴄᴛ ʏᴏᴜʀ ᴘʟᴀɴ 👇{existing_notice}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return SELECT_PLAN

    else:
        await query.edit_message_text(
            f"<b>{escape(service['name'])}</b>\n"
            f"<i>{escape(service['description'])}</i>\n\n"
            "•─────•─────────•─────•\n\n"
            f"sᴇʟᴇᴄᴛ ʏᴏᴜʀ ᴘʟᴀɴ 👇{existing_notice}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return SELECT_PLAN


async def select_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles bot choice for movie_single — stores selection then shows plans."""
    query = update.callback_query
    await query.answer()

    if query.data == "back":
        return await back_to_services(update, context)

    chosen_bot = query.data.split(":", 1)[1]
    context.user_data["selected_bot"] = chosen_bot

    service_key = context.user_data.get("service_key")
    service = SERVICES.get(service_key, {})

    keyboard = []
    for plan_key, plan in service["plans"].items():
        final_price, discount_pct = get_discounted_price(service_key, plan["price"])
        if discount_pct:
            label = f"◉ {plan['label']} — ₹{plan['price']} ➜ ₹{final_price} ({discount_pct}% off)"
        else:
            label = f"◉ {plan['label']} — ₹{final_price}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"plan:{plan_key}")])
    keyboard.append([InlineKeyboardButton("⬅️ ʙᴀᴄᴋ", callback_data="back_to_bot")])
    keyboard.append([InlineKeyboardButton("❌ ᴄᴀɴᴄᴇʟ", callback_data="cancel_order")])

    await query.edit_message_text(
        f"<b>{escape(service['name'])}</b>\n"
        f"🤖 Bot: <b>{escape(chosen_bot)}</b>\n\n"
        "•─────•─────────•─────•\n\n"
        "💸 sᴇʟᴇᴄᴛ ʏᴏᴜʀ ᴘʟᴀɴ 👇",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return SELECT_PLAN


async def back_to_bot_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Goes back to bot selection screen for movie_single."""
    query = update.callback_query
    await query.answer()

    service_key = context.user_data.get("service_key")
    service = SERVICES.get(service_key, {})
    bots = service.get("bots", [])
    bot_keyboard = [
        [InlineKeyboardButton(f"🤖 {b}", callback_data=f"bot:{b}")]
        for b in bots
    ]
    bot_keyboard.append([InlineKeyboardButton("⬅️ ʙᴀᴄᴋ", callback_data="back")])
    bot_keyboard.append([InlineKeyboardButton("❌ ᴄᴀɴᴄᴇʟ", callback_data="cancel_order")])
    await query.edit_message_text(
        f"<b>{escape(service['name'])}</b>\n\n"
        "🎯 ᴄʜᴏᴏsᴇ ᴛʜᴇ ʙᴏᴛ ʏᴏᴜ ᴡᴀɴᴛ ᴀᴄᴄᴇss ᴛᴏ 👇",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(bot_keyboard),
    )
    return SELECT_BOT


async def back_to_services(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton(info["name"], callback_data=f"service:{key}")]
        for key, info in SERVICES.items()
    ]
    keyboard.append([InlineKeyboardButton("ℹ️ ʜᴏᴡ ɪᴛ ᴡᴏʀᴋs", callback_data="help")])
    keyboard.append([InlineKeyboardButton("❌ ᴄᴀɴᴄᴇʟ", callback_data="cancel_order")])

    await query.edit_message_text(
        "🎖️ sᴇʟᴇᴄᴛ ʏᴏᴜʀ ᴘʟᴀɴ 👇",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return SELECT_SERVICE


async def select_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    plan_key = query.data.split(":")[1]
    service_key = context.user_data.get("service_key")
    service = SERVICES.get(service_key)

    if not service or plan_key not in service["plans"]:
        await query.edit_message_text("❌ Invalid selection. Type /start to try again.")
        return ConversationHandler.END

    context.user_data["plan_key"] = plan_key

    await query.edit_message_text(
        "🎟 *Do you have a coupon code?*\n\n"
        "Type your coupon code below, or tap *Skip* to continue without one.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⏭ Skip", callback_data="skip_coupon")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_order")],
        ]),
    )
    return AWAITING_COUPON


async def handle_coupon_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip().upper()
    service_key = context.user_data.get("service_key")
    plan_key = context.user_data.get("plan_key")

    coupon = await get_coupon(code)
    if not coupon:
        await update.message.reply_text(
            "❌ Invalid or expired coupon code. Try again or tap /skip to continue without one.",
        )
        return AWAITING_COUPON

    if coupon.get("max_uses") is not None and coupon["uses"] >= coupon["max_uses"]:
        await update.message.reply_text(
            "❌ This coupon has reached its usage limit. Try another code or /skip.",
        )
        return AWAITING_COUPON

    await use_coupon(code)
    await update.message.reply_text(
        f"✅ Coupon *{code}* applied — *{coupon['percent']}% off!*",
        parse_mode="Markdown",
    )

    await send_payment_qr(update, context, service_key, plan_key, code, coupon["percent"])
    return AWAITING_SCREENSHOT


async def skip_coupon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.delete_message()

    service_key = context.user_data.get("service_key")
    plan_key = context.user_data.get("plan_key")

    await send_payment_qr(query, context, service_key, plan_key, None, None)
    return AWAITING_SCREENSHOT


async def skip_coupon_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    service_key = context.user_data.get("service_key")
    plan_key = context.user_data.get("plan_key")

    await send_payment_qr(update, context, service_key, plan_key, None, None)
    return AWAITING_SCREENSHOT


async def receive_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    pending = PENDING_ORDERS.get(user.id)

    if not pending:
        await update.message.reply_text(
            "⚠️ No active order found. Type /start to begin a new order."
        )
        return ConversationHandler.END

    # Check QR expiry
    expires_at = pending.get("expires_at")
    if expires_at and datetime.utcnow() > expires_at:
        PENDING_ORDERS.pop(user.id, None)
        try:
            await update_order_status(pending["order_id"], "qr_expired")
        except Exception:
            pass
        await update.message.reply_text(
            "⏰ *QR Code Expired*\n\n"
            f"This QR code was only valid for {QR_EXPIRY_MINUTES} minutes and has now expired.\n\n"
            "Please type /start to generate a fresh QR code and complete your payment.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    order_id = pending["order_id"]

    try:
        order = await get_order(order_id)
    except Exception as e:
        logger.error(f"DB error fetching order: {e}")
        order = None

    photo = update.message.photo[-1]
    file_id = photo.file_id

    try:
        await update_order_status(order_id, "screenshot_received", file_id)
    except Exception as e:
        logger.error(f"DB error updating order status: {e}")

    user_info = (
        f"👤 <b>{escape(user.full_name)}</b>"
        + (f" (@{escape(user.username)})" if user.username else "")
        + f"\n🆔 <code>{user.id}</code>"
    )

    service_name = escape(order["service_name"]) if order else "Unknown"
    plan_label = escape(order["plan_label"]) if order else "Unknown"
    amount = order["amount"] if order else pending.get("amount", "?")
    original = order.get("original_amount") if order else None
    discount_pct = order.get("discount_percent") if order else None
    coupon_code = order.get("coupon_code") if order else None
    coupon_pct = order.get("coupon_percent") if order else None
    plan_key = order.get("plan_key", "") if order else ""

    savings_lines = []
    if discount_pct:
        savings_lines.append(f"🏷 Store discount: {discount_pct}%")
    if coupon_pct and coupon_code:
        savings_lines.append(f"🎟 Coupon {escape(coupon_code)}: {coupon_pct}%")
    savings_text = ("\n" + "\n".join(savings_lines)) if savings_lines else ""
    original_text = f" (original ₹{original})" if original and original != amount else ""

    selected_bot = context.user_data.get("selected_bot", "")
    bot_line = f"\n🤖 Bot: <b>{escape(selected_bot)}</b>" if selected_bot else ""

    caption = (
        f"📸 <b>New Payment Screenshot</b>\n\n"
        f"{user_info}\n\n"
        f"📦 {service_name} — {plan_label}{bot_line}\n"
        f"💰 ₹{amount}{original_text}{savings_text}\n"
        f"🗂 <code>{order_id}</code>"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"adm:approve:{order_id}:{user.id}:{plan_key}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"adm:reject:{order_id}:{user.id}"),
            InlineKeyboardButton("⏸ On Hold", callback_data=f"adm:hold:{order_id}:{user.id}"),
        ]
    ])

    try:
        await context.bot.send_photo(
            chat_id=ADMIN_CHANNEL_ID,
            photo=file_id,
            caption=caption,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.error(f"Error forwarding to admin channel: {e}")

    await update.message.reply_text(
        "✅ *Screenshot received!*\n\n"
        "Your payment is under review.\n"
        "You'll be notified once it's verified.\n\n"
        "Thank you! 🎉  Type /start to make another purchase.",
        parse_mode="Markdown",
    )

    await log_to_channel(
        context.bot,
        f"📸 <b>New Order</b>\n"
        f"{user_info}\n"
        f"📦 {service_name} — {plan_label}\n"
        f"💰 ₹{amount}\n"
        f"🗂 <code>{order_id}</code>",
    )

    PENDING_ORDERS.pop(user.id, None)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    PENDING_ORDERS.pop(update.effective_user.id, None)
    await update.message.reply_text("❌ Order cancelled. Type /start to begin again.")
    return ConversationHandler.END


async def cancel_order_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the ❌ Cancel Order inline button at any stage."""
    query = update.callback_query
    await query.answer()
    PENDING_ORDERS.pop(update.effective_user.id, None)
    context.user_data.clear()
    try:
        await query.edit_message_text("❌ Order cancelled. Type /start to begin again.")
    except Exception:
        await query.message.reply_text("❌ Order cancelled. Type /start to begin again.")
    return ConversationHandler.END


async def handle_stale_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catch-all for callback buttons that no longer belong to the current flow."""
    query = update.callback_query
    await query.answer("⚠️ This menu is no longer active. Use /start.", show_alert=True)


async def unexpected_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Please use the buttons to navigate, or type /start to begin."
    )


# ---------------------------------------------------------------------------
# Admin: inline button callbacks (Approve / Reject / On Hold)
# ---------------------------------------------------------------------------

async def admin_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.answer("⛔ Not authorised.", show_alert=True)
        return

    parts = query.data.split(":")
    action = parts[1]
    order_id = parts[2]
    user_id = int(parts[3])

    if action == "approve":
        plan_key = parts[4] if len(parts) > 4 else "1_month"
        try:
            end_date, extended, old_end_date = await approve_order(order_id, plan_key)
            end_str = end_date.strftime("%d %b %Y")
        except Exception as e:
            logger.error(f"Error approving order: {e}")
            await query.answer("DB error approving order.", show_alert=True)
            return

        try:
            order = await get_order(order_id)
            service = order.get("service_name", "your subscription") if order else "your subscription"
            plan = order.get("plan_label", "") if order else ""
            amount = order.get("amount", "?") if order else "?"
            if extended and old_end_date:
                old_str = old_end_date.strftime("%d %b %Y")
                action_text = (
                    f"♻️ *Subscription Extended!*\n\n"
                    f"Your *{service}* ({plan}) has been extended.\n"
                    f"💰 Amount: ₹{amount}\n"
                    f"📅 Was valid till: *{old_str}*\n"
                    f"📅 Now valid till: *{end_str}*\n\n"
                    f"Thank you for renewing! 🙌\n"
                    f"Type /start to make another purchase."
                )
            else:
                action_text = (
                    f"🎉 *Payment Approved!*\n\n"
                    f"Your *{service}* ({plan}) subscription has been activated.\n"
                    f"💰 Amount: ₹{amount}\n"
                    f"📅 Valid until: *{end_str}*\n\n"
                    f"Enjoy your premium access! Thank you 🙌\n"
                    f"Type /start to make another purchase."
                )
            await context.bot.send_message(
                chat_id=user_id,
                text=action_text,
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error(f"Error notifying user {user_id}: {e}")

        status_line = f"✅ <b>APPROVED</b> by @{escape(query.from_user.username or 'admin')} — active until {end_str}"
        await log_to_channel(
            context.bot,
            f"✅ <b>Order Approved</b>\n"
            f"🗂 <code>{order_id}</code>\n"
            f"👤 User ID: <code>{user_id}</code>\n"
            f"📅 Active until: {end_str}\n"
            f"👮 Admin: @{escape(query.from_user.username or 'admin')}",
        )

    elif action == "reject":
        try:
            await update_order_status(order_id, "rejected")
        except Exception as e:
            logger.error(f"Error rejecting order: {e}")

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "❌ *Payment Rejected*\n\n"
                    "We could not verify your payment screenshot.\n"
                    "Please ensure you send a clear screenshot showing the payment confirmation.\n\n"
                    "Type /start to try again or contact support."
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error(f"Error notifying user {user_id}: {e}")

        status_line = f"❌ <b>REJECTED</b> by @{escape(query.from_user.username or 'admin')}"
        await log_to_channel(
            context.bot,
            f"❌ <b>Order Rejected</b>\n"
            f"🗂 <code>{order_id}</code>\n"
            f"👤 User ID: <code>{user_id}</code>\n"
            f"👮 Admin: @{escape(query.from_user.username or 'admin')}",
        )

    elif action == "hold":
        try:
            await update_order_status(order_id, "on_hold")
        except Exception as e:
            logger.error(f"Error setting on hold: {e}")

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "⏸ *Order On Hold*\n\n"
                    "Your payment is currently under review by our team.\n"
                    "We'll notify you as soon as it's processed.\n\n"
                    "Thank you for your patience! 🙏"
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error(f"Error notifying user {user_id}: {e}")

        status_line = f"⏸ <b>ON HOLD</b> by @{escape(query.from_user.username or 'admin')}"
        await log_to_channel(
            context.bot,
            f"⏸ <b>Order On Hold</b>\n"
            f"🗂 <code>{order_id}</code>\n"
            f"👤 User ID: <code>{user_id}</code>\n"
            f"👮 Admin: @{escape(query.from_user.username or 'admin')}",
        )

    else:
        return

    # Edit channel message to show decision and remove buttons
    # On approval, append a ready-to-copy /add_premium command
    try:
        original_caption = query.message.caption or ""
        new_caption = original_caption + f"\n\n{status_line}"
        if action == "approve":
            duration_args = plan_key_to_add_premium_args(plan_key)
            new_caption += (
                f"\n\n📌 ᴜsᴀɢᴇ: <code>/add_premium {user_id} {duration_args}</code>"
            )
        await query.edit_message_caption(caption=new_caption, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error editing channel message: {e}")


# ---------------------------------------------------------------------------
# Admin: /discount command
# ---------------------------------------------------------------------------

DISCOUNT_HELP = (
    "📋 *Discount Command Usage:*\n\n"
    "`/discount set all 20` — 20% off all services\n"
    "`/discount set movie_single 15` — 15% off Movie Bot (Single)\n"
    "`/discount set movie_both 10` — 10% off Both Movie Bots\n\n"
    "`/discount remove all` — remove global discount\n"
    "`/discount remove movie_single` — remove Movie Bot discount\n\n"
    "`/discount list` — show active discounts"
)

VALID_DISCOUNT_KEYS = {"all", "movie_single", "movie_both", "adult"}


async def discount_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Not authorised.")
        return

    args = context.args
    if not args or args[0] not in ("set", "remove", "list"):
        await update.message.reply_text(DISCOUNT_HELP, parse_mode="Markdown")
        return

    action = args[0]

    if action == "list":
        active = await get_all_discounts()
        if not active:
            await update.message.reply_text("ℹ️ No active discounts.")
            return
        lines = []
        for key, pct in active.items():
            label = "All services" if key == "all" else SERVICES[key]["name"]
            lines.append(f"• {label}: *{pct}% off*")
        await update.message.reply_text("🏷 *Active Discounts:*\n\n" + "\n".join(lines), parse_mode="Markdown")
        return

    if action == "set":
        if len(args) < 3:
            await update.message.reply_text("❌ Usage: `/discount set <key> <percent>`", parse_mode="Markdown")
            return
        key = args[1].lower()
        if key not in VALID_DISCOUNT_KEYS:
            await update.message.reply_text(f"❌ Invalid key. Valid: `all`, `streaming`, `music`, `cloud`, `vpn`", parse_mode="Markdown")
            return
        try:
            percent = int(args[2])
            if not 1 <= percent <= 99:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ Percent must be 1–99.")
            return
        await set_discount(key, percent)
        label = "All services" if key == "all" else SERVICES[key]["name"]
        await update.message.reply_text(f"✅ *{percent}% discount* set for *{label}*.", parse_mode="Markdown")
        return

    if action == "remove":
        if len(args) < 2:
            await update.message.reply_text("❌ Usage: `/discount remove <key>`", parse_mode="Markdown")
            return
        key = args[1].lower()
        if key not in VALID_DISCOUNT_KEYS:
            await update.message.reply_text("❌ Invalid key.", parse_mode="Markdown")
            return
        await remove_discount(key)
        label = "All services" if key == "all" else SERVICES[key]["name"]
        await update.message.reply_text(f"✅ Discount removed for *{label}*.", parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Admin: /coupon command
# ---------------------------------------------------------------------------

COUPON_HELP = (
    "🎟 *Coupon Command Usage:*\n\n"
    "`/coupon create <CODE> <percent> [max_uses]`\n"
    "  e.g. `/coupon create SAVE20 20 100`\n\n"
    "`/coupon list` — show all active coupons\n"
    "`/coupon delete <CODE>` — deactivate a coupon"
)


async def coupon_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Not authorised.")
        return

    args = context.args
    if not args or args[0] not in ("create", "list", "delete"):
        await update.message.reply_text(COUPON_HELP, parse_mode="Markdown")
        return

    action = args[0]

    if action == "list":
        coupons = await list_coupons()
        if not coupons:
            await update.message.reply_text("ℹ️ No active coupons.")
            return
        lines = []
        for c in coupons:
            uses = c["uses"]
            max_u = c.get("max_uses")
            usage = f"{uses}/{max_u}" if max_u else f"{uses}/∞"
            lines.append(f"• `{c['code']}` — *{c['percent']}% off* ({usage} uses)")
        await update.message.reply_text("🎟 *Active Coupons:*\n\n" + "\n".join(lines), parse_mode="Markdown")
        return

    if action == "create":
        if len(args) < 3:
            await update.message.reply_text("❌ Usage: `/coupon create <CODE> <percent> [max_uses]`", parse_mode="Markdown")
            return
        code = args[1].upper()
        try:
            percent = int(args[2])
            if not 1 <= percent <= 99:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ Percent must be 1–99.")
            return
        max_uses = None
        if len(args) >= 4:
            try:
                max_uses = int(args[3])
            except ValueError:
                await update.message.reply_text("❌ max_uses must be a number.")
                return
        ok, err = await create_coupon(code, percent, max_uses)
        if not ok:
            await update.message.reply_text(f"❌ {err}")
            return
        limit_str = f" (max {max_uses} uses)" if max_uses else " (unlimited uses)"
        await update.message.reply_text(
            f"✅ Coupon `{code}` created — *{percent}% off*{limit_str}.",
            parse_mode="Markdown",
        )
        return

    if action == "delete":
        if len(args) < 2:
            await update.message.reply_text("❌ Usage: `/coupon delete <CODE>`", parse_mode="Markdown")
            return
        code = args[1].upper()
        deleted = await delete_coupon(code)
        if deleted:
            await update.message.reply_text(f"✅ Coupon `{code}` deactivated.", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"❌ Coupon `{code}` not found.", parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Admin: /broadcast command
# ---------------------------------------------------------------------------

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Not authorised.")
        return

    if not context.args:
        await update.message.reply_text(
            "📢 Usage: `/broadcast <your message>`\n\nYou can use *bold*, _italic_, etc.",
            parse_mode="Markdown",
        )
        return

    message = " ".join(context.args)
    user_ids = await get_all_user_ids()

    if not user_ids:
        await update.message.reply_text("ℹ️ No users to broadcast to.")
        return

    status_msg = await update.message.reply_text(f"📢 Broadcasting to {len(user_ids)} users...")

    sent = 0
    failed = 0
    for uid in user_ids:
        try:
            await context.bot.send_message(chat_id=uid, text=message, parse_mode="Markdown")
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # ~20 msgs/sec to stay within limits

    await status_msg.edit_text(
        f"📢 *Broadcast complete*\n\n✅ Sent: {sent}\n❌ Failed: {failed}",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Admin: /report command
# ---------------------------------------------------------------------------

async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Not authorised.")
        return

    data = await get_report_data()

    ending_lines = []
    for order in data["ending_soon"]:
        end_date = order["subscription_end"].strftime("%d %b %Y")
        username = f"@{order['username']}" if order.get("username") else order.get("full_name", "Unknown")
        service = order.get("service_name", "?")
        plan = order.get("plan_label", "?")
        amount = order.get("amount", "?")
        days_left = (order["subscription_end"].replace(tzinfo=None) - datetime.utcnow()).days + 1
        ending_lines.append(
            f"• {escape(str(username))} — {escape(service)} {escape(plan)}\n"
            f"  ₹{amount} | Ends {end_date} ({days_left}d left)"
        )

    ending_section = (
        "\n\n⏰ *Subscriptions ending in 7 days:*\n" + "\n".join(ending_lines)
        if ending_lines else "\n\n⏰ No subscriptions ending in the next 7 days."
    )

    report = (
        f"📊 *Bot Report*\n\n"
        f"👥 Total users: {data['total_users']}\n\n"
        f"📦 *Orders:*\n"
        f"• Total: {data['total']}\n"
        f"• ✅ Approved: {data['approved']}\n"
        f"• ⏳ Pending review: {data['pending']}\n"
        f"• ⏸ On hold: {data['on_hold']}\n"
        f"• ❌ Rejected: {data['rejected']}\n\n"
        f"💰 *Revenue (approved):* ₹{data['revenue']}"
        f"{ending_section}"
    )

    await update.message.reply_text(report, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Error handler & startup
# ---------------------------------------------------------------------------

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Unhandled error: {context.error}", exc_info=context.error)


# ---------------------------------------------------------------------------
# Duration helpers for /add_premium
# ---------------------------------------------------------------------------

def parse_duration(amount_str: str, unit_str: str) -> timedelta | None:
    """Convert e.g. ('7', 'day') → timedelta(days=7). Returns None on bad input."""
    try:
        amount = float(amount_str)
    except ValueError:
        return None
    unit = unit_str.lower().rstrip("s")  # normalise: days→day, hours→hour, mins→min, months→month, years→year
    if unit in ("day",):
        return timedelta(days=amount)
    if unit in ("hour",):
        return timedelta(hours=amount)
    if unit in ("min", "minute"):
        return timedelta(minutes=amount)
    if unit in ("week",):
        return timedelta(weeks=amount)
    if unit in ("month",):
        return timedelta(days=round(amount * 30))
    if unit in ("year",):
        return timedelta(days=round(amount * 365))
    return None


def plan_key_to_add_premium_args(plan_key: str) -> str:
    """Return the time args portion of /add_premium for a given plan_key.
    e.g. '30_days' → '30 day', '1_month' → '1 month', '1_week' → '7 day'.
    """
    mapping = {
        "1_day":   "1 day",
        "1_week":  "7 day",
        "1_month": "1 month",
        "7_days":  "7 day",
        "15_days": "15 day",
        "30_days": "30 day",
        "45_days": "45 day",
        "60_days": "60 day",
    }
    return mapping.get(plan_key, "30 day")


# ---------------------------------------------------------------------------
# User commands
# ---------------------------------------------------------------------------

async def mysub_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    orders = await get_user_active_orders(user.id)
    if not orders:
        await update.message.reply_text(
            "📭 <b>No active subscriptions found.</b>\n\n"
            "Type /start to purchase a plan.",
            parse_mode="HTML",
        )
        return

    now = datetime.utcnow()
    lines = ["📋 <b>Your Active Subscriptions:</b>\n"]
    for o in orders:
        end = o["subscription_end"]
        remaining = (end - now).days + 1
        lines.append(
            f"📦 <b>{escape(o.get('service_name', '?'))}</b> — {escape(o.get('plan_label', '?'))}\n"
            f"📅 Expires: <b>{end.strftime('%d %b %Y')}</b> ({remaining} day(s) left)\n"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ <b>How it works:</b>\n\n"
        "1️⃣ Type /start and select your bot plan\n"
        "2️⃣ Choose a duration\n"
        "3️⃣ Enter a coupon code if you have one (or skip)\n"
        "4️⃣ Scan the UPI QR code and pay\n"
        "5️⃣ Send the payment screenshot here\n"
        "6️⃣ Wait 5–10 mins — your premium will be activated ✅\n\n"
        "📋 <b>Commands:</b>\n"
        "/start — Buy a subscription\n"
        "/mysub — Check your active subscriptions\n"
        "/support — Contact support\n"
        "/cancel — Cancel current order",
        parse_mode="HTML",
    )


async def support_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🆘 <b>Support</b>\n\n"
        f"For any issues, contact us:\n"
        f"👤 @{escape(SUPPORT_USERNAME)}\n\n"
        "Please include your Telegram ID and order details.",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# Admin commands
# ---------------------------------------------------------------------------


async def dm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a direct message to any user by their Telegram ID.
    Usage: /dm <user_id> <message text>
    """
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Not authorised.")
        return

    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text(
            "📩 <b>Usage:</b> /dm &lt;user_id&gt; &lt;message&gt;\n\n"
            "Example: <code>/dm 123456789 Your subscription is ready!</code>",
            parse_mode="HTML",
        )
        return

    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID — must be a number.")
        return

    text = " ".join(args[1:])
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=f"📩 <b>Message from FlixHD Admin:</b>\n\n{text}",
            parse_mode="HTML",
        )
        await update.message.reply_text(
            f"✅ Message delivered to <code>{target_id}</code>.", parse_mode="HTML"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to send: {e}")

async def add_premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually add premium for flexible duration.
    Usage: /add_premium <user_id> <amount> <unit>
    Example: /add_premium 123456789 1 month
    Accepted units: day, hour, min, week, month, year (singular or plural)
    """
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Not authorised.")
        return

    args = context.args
    if len(args) != 3:
        await update.message.reply_text(
            "❌ <b>Usage:</b> <code>/add_premium &lt;user_id&gt; &lt;amount&gt; &lt;unit&gt;</code>\n\n"
            "📅 <b>Examples:</b>\n"
            "<code>/add_premium 123456789 1 month</code>\n"
            "<code>/add_premium 123456789 7 day</code>\n"
            "<code>/add_premium 123456789 1 year</code>\n"
            "<code>/add_premium 123456789 12 hour</code>\n"
            "<code>/add_premium 123456789 30 min</code>\n\n"
            "🧭 <b>Accepted units:</b> day, hour, min, week, month, year",
            parse_mode="HTML",
        )
        return

    try:
        user_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID — must be a number.")
        return

    amount_str, unit_str = args[1], args[2]
    delta = parse_duration(amount_str, unit_str)
    if delta is None:
        await update.message.reply_text(
            f"❌ Unknown unit <code>{escape(unit_str)}</code>.\n"
            "Accepted: day, hour, min, week, month, year",
            parse_mode="HTML",
        )
        return

    from datetime import timezone
    now = datetime.utcnow()
    end_date = now + delta
    end_str = end_date.strftime("%d %b %Y %H:%M UTC")
    label = f"{amount_str} {unit_str}"

    try:
        order_id, _ = await manually_create_approved_order(
            user_id=user_id,
            service_key="manual",
            service_name="Manual Premium",
            plan_key="manual",
            plan_label=label,
            amount=0,
            end_date=end_date,
        )
    except Exception as e:
        await update.message.reply_text(f"❌ DB error: {e}")
        return

    await update.message.reply_text(
        f"✅ <b>Premium activated!</b>\n\n"
        f"👤 User ID: <code>{user_id}</code>\n"
        f"⏱ Duration: <b>{label}</b>\n"
        f"📅 Valid until: <b>{end_str}</b>\n"
        f"🗂 Order: <code>{order_id}</code>",
        parse_mode="HTML",
    )
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"🎉 <b>Premium Activated!</b>\n\n"
                f"Your premium access has been activated by admin.\n"
                f"⏱ Duration: <b>{label}</b>\n"
                f"📅 Valid until: <b>{end_str}</b>\n\n"
                "Enjoy your access! 🙌"
            ),
            parse_mode="HTML",
        )
    except Exception:
        pass


async def adduser_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually activate a subscription for a user.
    Usage: /adduser <user_id> <service_key> <plan_key>
    Example: /adduser 123456789 movie_single 30_days
    """
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Not authorised.")
        return

    args = context.args
    if len(args) != 3:
        services_list = "\n".join(
            f"  <code>{k}</code>: " + ", ".join(f"<code>{p}</code>" for p in v["plans"])
            for k, v in SERVICES.items()
        )
        await update.message.reply_text(
            "❌ <b>Usage:</b> <code>/adduser &lt;user_id&gt; &lt;service_key&gt; &lt;plan_key&gt;</code>\n\n"
            f"<b>Available services &amp; plans:</b>\n{services_list}",
            parse_mode="HTML",
        )
        return

    try:
        user_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID — must be a number.")
        return

    service_key = args[1]
    plan_key = args[2]
    service = SERVICES.get(service_key)
    if not service:
        await update.message.reply_text(f"❌ Unknown service: <code>{escape(service_key)}</code>", parse_mode="HTML")
        return
    plan = service["plans"].get(plan_key)
    if not plan:
        await update.message.reply_text(f"❌ Unknown plan: <code>{escape(plan_key)}</code>", parse_mode="HTML")
        return

    from config import PLAN_DURATIONS
    days = PLAN_DURATIONS.get(plan_key, 30)
    try:
        order_id, end_date = await manually_create_approved_order(
            user_id=user_id,
            service_key=service_key,
            service_name=service["name"],
            plan_key=plan_key,
            plan_label=plan["label"],
            amount=plan["price"],
            days=days,
        )
    except Exception as e:
        await update.message.reply_text(f"❌ DB error: {e}")
        return

    end_str = end_date.strftime("%d %b %Y")
    await update.message.reply_text(
        f"✅ <b>Subscription activated!</b>\n\n"
        f"👤 User ID: <code>{user_id}</code>\n"
        f"📦 {escape(service['name'])} — {escape(plan['label'])}\n"
        f"📅 Valid until: <b>{end_str}</b>\n"
        f"🗂 Order: <code>{order_id}</code>",
        parse_mode="HTML",
    )
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"🎉 <b>Subscription Activated!</b>\n\n"
                f"Your <b>{escape(service['name'])}</b> ({escape(plan['label'])}) subscription has been activated by admin.\n"
                f"📅 Valid until: <b>{end_str}</b>\n\n"
                "Enjoy your premium access! 🙌"
            ),
            parse_mode="HTML",
        )
    except Exception:
        pass


async def checkuser_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check a user's active subscriptions.
    Usage: /checkuser <user_id>
    """
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Not authorised.")
        return

    args = context.args
    if not args:
        await update.message.reply_text("❌ Usage: <code>/checkuser &lt;user_id&gt;</code>", parse_mode="HTML")
        return
    try:
        user_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return

    orders = await get_user_active_orders(user_id)
    if not orders:
        await update.message.reply_text(
            f"📭 No active subscriptions for user <code>{user_id}</code>.",
            parse_mode="HTML",
        )
        return

    now = datetime.utcnow()
    lines = [f"📋 <b>Active subscriptions for <code>{user_id}</code>:</b>\n"]
    for o in orders:
        end = o["subscription_end"]
        remaining = (end - now).days + 1
        lines.append(
            f"📦 <b>{escape(o.get('service_name', '?'))}</b> — {escape(o.get('plan_label', '?'))}\n"
            f"📅 Expires: <b>{end.strftime('%d %b %Y')}</b> ({remaining} day(s) left)\n"
            f"🗂 <code>{str(o['_id'])}</code>\n"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List orders with screenshot received, awaiting review."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Not authorised.")
        return

    orders = await get_pending_screenshot_orders(limit=20)
    if not orders:
        await update.message.reply_text("✅ No pending orders — all clear!")
        return

    lines = [f"⏳ <b>{len(orders)} Pending Order(s):</b>\n"]
    for o in orders:
        created = o.get("created_at", datetime.utcnow()).strftime("%d %b %H:%M")
        lines.append(
            f"👤 <code>{o.get('user_id', '?')}</code> — "
            f"{escape(o.get('service_name', '?'))} {escape(o.get('plan_label', '?'))}\n"
            f"⏰ {created} | 🗂 <code>{str(o['_id'])}</code>\n"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def _self_ping_loop():
    """Pings /health every 14 min to keep Render/Koyeb free instances awake."""
    import httpx
    url = f"{SELF_PING_URL}/health"
    logger.info(f"Self-ping active → {url} (every 14 min)")
    while True:
        await asyncio.sleep(14 * 60)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(url)
            logger.info(f"Self-ping OK ({r.status_code})")
        except Exception as e:
            logger.warning(f"Self-ping failed: {e}")


async def post_init(application):
    await load_discounts()
    start_scheduler(application.bot)
    if SELF_PING_URL:
        asyncio.create_task(_self_ping_loop())

    # Register command menu visible in Telegram
    from telegram import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat
    user_commands = [
        BotCommand("start",   "🛒 Buy a subscription"),
        BotCommand("mysub",   "📋 Check your active subscriptions"),
        BotCommand("help",    "ℹ️ How it works"),
        BotCommand("support", "🆘 Contact support"),
        BotCommand("cancel",  "❌ Cancel current order"),
    ]
    admin_commands = user_commands + [
        BotCommand("add_premium", "⚡ Add premium by duration (1 month, 7 day…)"),
        BotCommand("adduser",   "➕ Manually activate subscription"),
        BotCommand("checkuser", "🔍 Check a user's subscriptions"),
        BotCommand("pending",   "⏳ List pending orders"),
        BotCommand("discount",  "🏷 Manage store discounts"),
        BotCommand("coupon",    "🎟 Manage coupon codes"),
        BotCommand("broadcast", "📢 Send message to all users"),
        BotCommand("report",    "📊 Stats report"),
    ]
    try:
        await application.bot.set_my_commands(user_commands, scope=BotCommandScopeAllPrivateChats())
        await application.bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=ADMIN_USER_ID))
    except Exception as e:
        logger.warning(f"Could not set bot commands: {e}")

    logger.info("Bot initialised — discounts loaded, scheduler started.")


def main():
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    _cancel_btn = CallbackQueryHandler(cancel_order_callback, pattern="^cancel_order$")
    _stale_btn  = CallbackQueryHandler(handle_stale_callback)

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SELECT_SERVICE: [
                CallbackQueryHandler(select_service, pattern="^service:"),
                CallbackQueryHandler(select_service, pattern="^help$"),
                _cancel_btn,
            ],
            SELECT_BOT: [
                CallbackQueryHandler(select_bot, pattern="^bot:"),
                CallbackQueryHandler(back_to_services, pattern="^back$"),
                _cancel_btn,
            ],
            SELECT_PLAN: [
                CallbackQueryHandler(select_plan, pattern="^plan:"),
                CallbackQueryHandler(back_to_bot_selection, pattern="^back_to_bot$"),
                CallbackQueryHandler(back_to_services, pattern="^back$"),
                _cancel_btn,
            ],
            AWAITING_COUPON: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_coupon_input),
                CallbackQueryHandler(skip_coupon, pattern="^skip_coupon$"),
                CommandHandler("skip", skip_coupon_command),
                _cancel_btn,
            ],
            AWAITING_SCREENSHOT: [
                MessageHandler(filters.PHOTO, receive_screenshot),
                _cancel_btn,
                _stale_btn,
            ],
        },
        fallbacks=[
            CommandHandler("start", start),
            CommandHandler("cancel", cancel),
            _cancel_btn,
            MessageHandler(filters.TEXT & ~filters.COMMAND, unexpected_text),
            _stale_btn,
        ],
        per_user=True,
        per_chat=True,
        per_message=False,
        allow_reentry=True,
    )

    app.add_handler(conv_handler)

    # Admin action callbacks (outside conversation — works in channels too)
    app.add_handler(CallbackQueryHandler(admin_action_callback, pattern="^adm:"))

    # User commands
    app.add_handler(CommandHandler("mysub",   mysub_command))
    app.add_handler(CommandHandler("help",    help_command))
    app.add_handler(CommandHandler("support", support_command))

    # Admin commands — group -1 so they fire before the ConversationHandler
    for _cmd, _hdl in [
        ("discount",    discount_command),
        ("coupon",      coupon_command),
        ("broadcast",   broadcast_command),
        ("report",      report_command),
        ("add_premium", add_premium_command),
        ("adduser",     adduser_command),
        ("checkuser",   checkuser_command),
        ("pending",     pending_command),
        ("dm",          dm_command),
    ]:
        app.add_handler(CommandHandler(_cmd, _hdl), group=-1)

    app.add_error_handler(error_handler)

    start_health_server()
    logger.info("Bot is starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    import asyncio
    asyncio.set_event_loop(asyncio.new_event_loop())
    main()
