# Xerodel — Telegram + Website Account Store

A single-process FastAPI app with a website, admin panel, and Telegram bot that share one PostgreSQL database. Orders are fully synced between the website and the bot.

## Features
- **Website**: product shop, checkout, order tracking, Telegram Login Widget auth
- **Telegram Bot**: browse, buy, orders, payment proof submission, admin menu
- **Admin Panel** (website + bot `/admin`): products, inventory, orders, payments, settings, audit log
- **Payment Proof Flow**: user submits via bot DM or website upload → bot posts to private channel with inline ✅ Accept / ❌ Reject buttons
- **Delivery**: bot DM credentials to buyer + visible on buyer's order page
- **Public Channel**: order posts created at placement, edited on every status change
- **Expiry**: pending orders auto-cancel after configurable minutes
- **Per-unit Inventory**: each account/cookie is one row; assigned on sale, removed from stock

## Quick Start

1. **Run PostgreSQL 14+ and create a database**, then configure `.env` (`DATABASE_URL`). The easiest local setup is Docker Compose, which starts both the app and a Postgres container:
   ```bash
   cp .env.example .env   # or create .env with the values below
   docker compose up -d --build
   ```
   Required:
   - `DATABASE_URL` — e.g. `postgresql://xerodel:xerodel@db:5432/xerodel` (inside Compose) or your Dokploy/Postgres connection string
   - `BOT_TOKEN` — from @BotFather
   - `BOT_USERNAME` — bot username (without @) for Telegram Login Widget
   - `PUBLIC_CHANNEL_ID` — channel ID where orders are posted (bot must be admin)
   - `PRIVATE_CHANNEL_ID` — channel ID for payment proofs (bot must be admin)
   - `ADMIN_IDS` — comma-separated Telegram user IDs allowed to use `/admin` and website admin panel
   - `WEBHOOK_SECRET` — random string (e.g. `openssl rand -hex 32`)
   - `SESSION_SECRET` — random string (e.g. `openssl rand -hex 32`)
   - `BASE_URL` — public HTTPS URL (from Cloudflare Tunnel)
   - `BKASH_NUMBER`, `NAGAD_NUMBER` — payment numbers
   - `SEED_DEMO=true` — inserts 3 demo products on first run

2. **Install & run:**
   ```bash
   chmod +x run.sh tunnel.sh
   ./run.sh
   ```
   The app runs at `http://localhost:8000`.

3. **Expose with Cloudflare Tunnel:**
   ```bash
   ./tunnel.sh
   # copy the https://xxx.trycloudflare.com URL
   # update BASE_URL in .env and restart
   ```
   For a stable domain, create a named tunnel:
   ```bash
   cloudflared tunnel login
   cloudflared tunnel create sell-premium
   cloudflared tunnel route dns sell-premium yourdomain.com
   cloudflared tunnel run sell-premium
   ```

4. **Bot Setup:**
   - Add bot as **admin** in both channels (post/edit rights)
   - In BotFather: `/setdomain yourdomain.com` (for Telegram Login Widget)
   - Webhook auto-registered at startup (`/tg/webhook/<WEBHOOK_SECRET>`)
   - Fallback: set `USE_POLLING=true` if tunnel is down

## Admin Commands (Bot)
- `/admin` — main menu (pending payments, recent orders, stock, products, inventory, stats, settings)
- `/order <code>` — inspect any order (buyer, items, proof, accept/reject/cancel actions)
- `/admin_stats` — stats summary
- `/admin_settings` — view settings
- `/set_bkash <number>` — update bKash number
- `/set_nagad <number>` — update Nagad number
- `/set_channels <public_id> <private_id>` — update channel IDs
- `/set_admins <id1,id2>` — update admin IDs
- `/set_expiry <minutes>` — order expiry time
- `/set_instructions <text>` — payment instructions

Add Product asks for platform, title, price and an optional description (`/skip`).

## User Commands (Bot)
- `/start` — welcome + menu (admins also see an Admin button)
- `/help` — ordering guide and command list
- `/browse` — paginated product catalog
- `/buy [product_id]` — buy flow (quantity → confirm, with Cancel buttons)
- `/orders` — clickable order list with live status
- `/proof [order_code]` — submit payment proof (bKash/Nagad, TrxID caption)
- `/cancel` — stop the current step

After a proof is submitted the bot shows check-status and website buttons; each order page also has refresh, proof and website actions.

## Website Routes
- `/` — landing
- `/shop` — all products
- `/product/<id>` — product detail + buy
- `/checkout?product_id=&qty=` — checkout
- `/orders` — my orders
- `/order/<code>` — order detail + proof upload + credentials (polling)
- `/login` — Telegram Login Widget
- `/logout` — logout
- `/admin` — admin panel (admin only)

## Admin Panel
- Dashboard: stats, pending payments, low stock, recent orders, audit log
- Products: add, edit, toggle, delete (with stock counts)
- Inventory: per-product bulk add (username | password | cookie), delete unsold
- Orders: filter, detail view with buyer, items, proof, credentials
- Payments: pending queue with photo + accept/reject + note; history
- Settings: all config editable
- Audit log: all admin actions

## Database Schema
PostgreSQL tables are created automatically on startup (`app/database.py`): `users`, `products`, `inventory`, `orders`, `order_items`, `order_inventory`, `payment_proofs`, `settings`, `admin_actions`.

### Migrating existing SQLite data
If you already ran the SQLite version, copy the old data once:
```bash
DATABASE_URL=postgresql://user:pass@host:5432/db \
  python scripts/migrate_sqlite_to_postgres.py --sqlite data/app.db --replace
```
Then copy `data/proofs/` to the server's `PROOF_DIR` (same path, e.g. `/app/data/proofs`). The `--replace` flag wipes existing target rows; omit it for an empty database.

## Deployment (Docker / Dokploy)
`docker-compose.yml` starts PostgreSQL plus the app and waits for the database healthcheck.
1. Set `DATABASE_URL`, bot tokens, channel IDs and `BASE_URL` in the environment (Dokploy → Environment).
2. Mount a persistent volume at `/app/data` for payment proof images.
3. Deploy the Compose stack; the schema is created on first start.

If you use an external managed Postgres, point `DATABASE_URL` at it and remove the `db` service.

## Validation Checklist
1. Place order via bot → public channel post appears with masked buyer
2. Submit proof via website upload → private channel post with photo + Accept/Reject buttons
3. Accept from **website admin panel** → private post edited, buyer DM received with credentials, public post updated, inventory removed from available stock, website order page shows credentials
4. Repeat with **channel button** accept → website admin panel shows accepted (sync check)
5. Reject flow → order cancelled, buyer notified, both channel posts edited
6. Expiry: create order, wait/adjust expiry → auto-cancel, public post edited
7. Stock: buy last unit → second buyer sees "Out of stock"
8. Change product price in website admin → bot `/buy` shows new price immediately
9. Non-admin Telegram login on website → blocked from `/admin`

## Project Structure
```
app/
  main.py                 # FastAPI app, lifespan, webhook, expiry sweep
  config.py               # pydantic-settings from .env
  database.py             # PostgreSQL schema, connection wrapper, init, settings, audit
  services/
    orders.py             # create order, status transitions, expiry sweep
    inventory.py          # stock count, assign items (BEGIN IMMEDIATE), release
    payments.py           # submit proof, accept/reject (single entry point)
    delivery.py           # assign inventory + DM credentials + mark delivered
    channels.py           # post/edit public + private channel messages
    products.py           # product CRUD (used by admin panel + bot)
  telegram/
    bot.py                # Application setup, webhook feeding
    handlers_user.py      # browse/buy/orders/proof conversation
    handlers_admin.py     # /admin menu + actions
    handlers_channel.py   # private-channel inline button callbacks
  web/
    auth.py               # Telegram Login Widget hash verification, session cookie
    routes.py             # website pages
    admin_routes.py       # admin panel pages/API
    templates/            # Jinja2: site pages + admin pages
    static/               # css/js
requirements.txt, .env.example, run.sh, tunnel.sh, README.md
```

## Notes
- Single process: uvicorn runs FastAPI + python-telegram-bot Application in same loop
- PostgreSQL handles concurrency; every request opens a short-lived connection and writes are committed per operation
- Race-safe inventory assignment via a transaction with `SELECT ... FOR UPDATE SKIP LOCKED` + re-check at verification
- Timestamps are stored as ISO-8601 UTC text (`YYYY-MM-DDTHH:MM:SS`) for compatibility with existing data
- Credentials only visible to buyer (DM + auth-checked order page) — never in public channel
- If bot DM fails (user blocked), credentials still available on website; admin alerted in private channel
- Tunnel fallback: set `USE_POLLING=true` for long-polling mode when webhook unreachable