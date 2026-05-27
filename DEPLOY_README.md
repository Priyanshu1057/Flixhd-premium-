# FlixHD Premium Telegram Bot — Deployment Guide

## Environment Variables

| Variable        | Required | Description                                          | Example                              |
|-----------------|----------|------------------------------------------------------|--------------------------------------|
| TELEGRAM_BOT_TOKEN | ✅ Yes | Bot token from @BotFather                           | 1234567890:AAF...                    |
| MONGODB_URI     | ✅ Yes   | MongoDB Atlas connection string                      | mongodb+srv://user:pass@cluster/     |
| ADMIN_CHANNEL_ID| ✅ Yes   | Channel ID for admin alerts (negative number)        | -1001234567890                       |
| ADMIN_USER_ID   | ✅ Yes   | Your Telegram user ID                                | 123456789                            |
| UPI_ID          | ✅ Yes   | UPI ID shown on payment QR screen                   | Flixhd@ptaxis                        |
| LOG_CHANNEL_ID  | ⬜ Optional | Separate log channel ID                           | -1009876543210                       |
| SELF_PING_URL   | ⬜ Optional | Public URL — keeps Render/Koyeb free instance awake | https://your-bot.onrender.com       |
| API_ID          | ⬜ Optional | Telegram MTProto API ID (my.telegram.org)          | 12345678                             |
| API_HASH        | ⬜ Optional | Telegram MTProto API Hash                          | abcdef1234567890                     |

## Config to Customise (bot/config.py)
- SUPPORT_USERNAME  — your support @username
- MOVIE_BOTS        — list of your movie bot usernames
- SERVICES          — service names and plan pricing
- QR_EXPIRY_MINUTES — how long QR codes stay valid (default 15)
- PLAN_DURATIONS    — days per plan key

## Install & Run
pip install -r bot/requirements.txt
cd bot && python bot.py

## Deployment Options
### Render (Free)
  Build: pip install -r bot/requirements.txt
  Start: cd bot && python bot.py
  Set SELF_PING_URL to your Render URL to prevent sleep.

### Koyeb (Free)
  Start: cd bot && python bot.py
  Add all env vars in the Koyeb dashboard.

### VPS
  pip install -r bot/requirements.txt
  screen -S premiumbot
  cd bot && python bot.py

### Railway
  Start: cd bot && python bot.py
  Add env vars via Railway dashboard.

## MongoDB Setup
  1. Create free cluster at mongodb.com/atlas
  2. Database name: premium_bot
  3. Collections auto-created: orders, users, discounts, coupons
  4. Allow all IPs in Network Access (or your server IP)
  5. Copy connection string to MONGODB_URI

## Git .gitignore
  .env
  __pycache__/
  *.pyc

## Admin Commands
  /add_premium  user_id 1 month
  /adduser      user_id movie_single 30_days
  /checkuser    user_id
  /pending      (list pending orders)
  /discount     set all 20
  /coupon       add CODE 15 100
  /broadcast    Your message here
  /report       (full stats)
