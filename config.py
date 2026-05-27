import os

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
MONGODB_URI = os.environ["MONGODB_URI"]
ADMIN_CHANNEL_ID = int(os.environ["ADMIN_CHANNEL_ID"])
UPI_ID = os.environ["UPI_ID"]
ADMIN_USER_ID = int(os.environ["ADMIN_USER_ID"])

# Telegram MTProto credentials (from my.telegram.org) — required for Pyrogram features
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")

# Optional separate log channel — all events (orders, approvals, rejections) are mirrored here
_log_channel_raw = os.environ.get("LOG_CHANNEL_ID", "")
LOG_CHANNEL_ID = int(_log_channel_raw) if _log_channel_raw else None

# Self-ping URL — set this to your Render/Koyeb public URL to keep the free instance alive
# e.g. https://your-bot.onrender.com  or  https://your-bot.koyeb.app
SELF_PING_URL = os.environ.get("SELF_PING_URL", "").rstrip("/")

QR_EXPIRY_MINUTES = 5

PLAN_DURATIONS: dict[str, int] = {
    "1_day": 1,
    "1_week": 7,
    "1_month": 30,
    "7_days": 7,
    "15_days": 15,
    "30_days": 30,
    "45_days": 45,
    "60_days": 60,
}

MOVIE_BOTS = ["@Movie_seriesflixbot", "@FilmyflixHDbot"]

SERVICES = {
    "movie_single": {
        "name": "🎬 Movie Bot (Single)",
        "description": "Premium access to 1 Movie Bot of your choice",
        "bots": MOVIE_BOTS,
        "plans": {
            "7_days":  {"label": "07 Days", "price": 20},
            "15_days": {"label": "15 Days", "price": 30},
            "30_days": {"label": "30 Days", "price": 45},
            "45_days": {"label": "45 Days", "price": 90},
            "60_days": {"label": "60 Days", "price": 110},
        },
    },
    "movie_both": {
        "name": "🎬🎬 Both Movie Bots",
        "description": "Premium access to both Movie Bots",
        "bots": MOVIE_BOTS,
        "plans": {
            "7_days":  {"label": "07 Days", "price": 25},
            "15_days": {"label": "15 Days", "price": 40},
            "30_days": {"label": "30 Days", "price": 60},
            "45_days": {"label": "45 Days", "price": 100},
            "60_days": {"label": "60 Days", "price": 130},
        },
    },
}

SERVICES["adult"] = {
    "name": "🔞 18+ Video Bot",
    "description": "Premium adult content bot access",
    "plans": {
        "1_day":  {"label": "1 Day",  "price": 10},
        "1_week": {"label": "1 Week", "price": 50},
        "1_month": {"label": "1 Month", "price": 150},
    },
}

SUPPORT_USERNAME = "YourSupportUsername"  # Change to your support @username
