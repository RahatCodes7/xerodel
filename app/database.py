import os
import sqlite3
import time

from app.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    full_name TEXT,
    username TEXT,
    photo_url TEXT,
    is_admin INTEGER DEFAULT 0,
    created_at TEXT,
    last_seen_at TEXT
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL CHECK(platform IN ('telegram','crunchyroll','cookie')),
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    price_bdt REAL NOT NULL,
    is_active INTEGER DEFAULT 1,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS inventory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    account_username TEXT,
    account_password TEXT,
    cookie_data TEXT,
    status TEXT NOT NULL DEFAULT 'available' CHECK(status IN ('available','assigned','sold')),
    assigned_order_id INTEGER,
    created_at TEXT,
    sold_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_inventory_product_status ON inventory(product_id, status);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_code TEXT UNIQUE NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(telegram_id),
    status TEXT NOT NULL DEFAULT 'pending_payment',
    total_bdt REAL NOT NULL,
    payment_method TEXT,
    created_at TEXT,
    expires_at TEXT,
    public_msg_id INTEGER,
    paid_at TEXT,
    delivered_at TEXT,
    cancel_reason TEXT,
    delivery_status TEXT,
    delivery_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL,
    unit_price REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS order_inventory (
    order_id INTEGER NOT NULL REFERENCES orders(id),
    inventory_id INTEGER NOT NULL REFERENCES inventory(id),
    PRIMARY KEY (order_id, inventory_id)
);

CREATE TABLE IF NOT EXISTS payment_proofs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER UNIQUE NOT NULL REFERENCES orders(id),
    method TEXT,
    transaction_id TEXT,
    sender_number TEXT,
    photo_file_id TEXT,
    photo_path TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','accepted','rejected')),
    private_msg_id INTEGER,
    reviewed_by INTEGER,
    reviewed_at TEXT,
    note TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS admin_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER,
    action TEXT,
    target_type TEXT,
    target_id TEXT,
    detail TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_admin_actions_created ON admin_actions(created_at DESC);
"""

DEMO_PRODUCTS = [
    ("telegram", "Telegram Premium (1 month)", "Telegram Premium subscription for 1 month. Username + password delivered after payment verification.", 350.0),
    ("crunchyroll", "Crunchyroll Premium (1 month)", "Crunchyroll Premium account, 1 month validity. Credentials delivered after payment verification.", 500.0),
    ("cookie", "Cookie Pack (session cookies)", "Session cookies with credentials for the selected platform. Delivered after payment verification.", 250.0),
]


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def get_connection():
    conn = sqlite3.connect(settings.DATABASE_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    os.makedirs(os.path.dirname(settings.DATABASE_PATH) or ".", exist_ok=True)
    os.makedirs(settings.PROOF_DIR, exist_ok=True)
    conn = get_connection()
    conn.executescript(SCHEMA)
    conn.commit()

    default_settings = {
        "site_name": settings.SITE_NAME,
        "bkash_number": settings.BKASH_NUMBER,
        "nagad_number": settings.NAGAD_NUMBER,
        "payment_instructions": settings.PAYMENT_INSTRUCTIONS,
        "admin_ids": settings.ADMIN_IDS,
        "public_channel_id": settings.PUBLIC_CHANNEL_ID,
        "private_channel_id": settings.PRIVATE_CHANNEL_ID,
        "expiry_minutes": str(settings.ORDER_EXPIRY_MINUTES),
    }
    for key, value in default_settings.items():
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()

    count = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    if count == 0 and settings.SEED_DEMO:
        for platform, title, description, price in DEMO_PRODUCTS:
            conn.execute(
                "INSERT INTO products (platform, title, description, price_bdt, is_active, created_at) VALUES (?,?,?,?,1,?)",
                (platform, title, description, price, now_iso()),
            )
        conn.commit()

    conn.close()


def get_setting(key, default=None):
    conn = get_connection()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    if row is None or row["value"] is None:
        return default
    return row["value"]


def set_setting(key, value):
    conn = get_connection()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    conn.commit()
    conn.close()


def get_admin_ids():
    raw = get_setting("admin_ids", settings.ADMIN_IDS) or settings.ADMIN_IDS or ""
    ids = []
    for part in raw.replace(",", " ").split():
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    return ids


def get_channel_ids():
    return (
        get_setting("public_channel_id", settings.PUBLIC_CHANNEL_ID) or "",
        get_setting("private_channel_id", settings.PRIVATE_CHANNEL_ID) or "",
    )


def get_expiry_minutes():
    try:
        return int(get_setting("expiry_minutes", settings.ORDER_EXPIRY_MINUTES) or settings.ORDER_EXPIRY_MINUTES)
    except (TypeError, ValueError):
        return settings.ORDER_EXPIRY_MINUTES


def upsert_user(telegram_id, full_name=None, username=None, photo_url=None):
    admin_ids = get_admin_ids()
    is_admin = 1 if telegram_id in admin_ids else 0
    conn = get_connection()
    conn.execute(
        """INSERT INTO users (telegram_id, full_name, username, photo_url, is_admin, created_at, last_seen_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(telegram_id) DO UPDATE SET
             full_name = excluded.full_name,
             username = excluded.username,
             photo_url = excluded.photo_url,
             is_admin = excluded.is_admin,
             last_seen_at = excluded.last_seen_at""",
        (telegram_id, full_name, username, photo_url, is_admin, now_iso(), now_iso()),
    )
    conn.commit()
    conn.close()


def get_user(telegram_id):
    conn = get_connection()
    row = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def touch_user(telegram_id):
    conn = get_connection()
    conn.execute("UPDATE users SET last_seen_at = ? WHERE telegram_id = ?", (now_iso(), telegram_id))
    conn.commit()
    conn.close()


def log_admin_action(admin_id, action, target_type=None, target_id=None, detail=None):
    conn = get_connection()
    conn.execute(
        "INSERT INTO admin_actions (admin_id, action, target_type, target_id, detail, created_at) VALUES (?,?,?,?,?,?)",
        (admin_id, action, target_type, str(target_id) if target_id is not None else None, detail, now_iso()),
    )
    conn.commit()
    conn.close()


def get_audit_log(limit=200):
    conn = get_connection()
    rows = conn.execute(
        "SELECT a.*, u.full_name AS admin_name, u.username AS admin_username FROM admin_actions a LEFT JOIN users u ON u.telegram_id = a.admin_id ORDER BY a.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
