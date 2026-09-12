import os
import html
import random
import string
import sqlite3
import logging
import time
from datetime import datetime, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode, ChatMemberStatus
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ============================================================
#                       CONFIGURATION
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")

# Comma separated admin IDs in env, or hardcode here.
_env_admins = os.environ.get("ADMIN_IDS", "").replace(" ", "")
ADMIN_IDS = [
    int(x)
    for x in _env_admins.split(",")
    if x.strip().lstrip("-").isdigit()
] or [123456789]  # fallback example — replace with your real admin ID

SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@yourusername")

FORCE_JOIN_ENABLED = os.getenv("FORCE_JOIN_ENABLED", "True").lower() in ("1", "true", "yes")
FORCE_JOIN_CHANNEL = os.getenv("FORCE_JOIN_CHANNEL", "@yourchannel")
FORCE_JOIN_URL = os.getenv("FORCE_JOIN_URL", "https://t.me/yourchannel")

MIN_DEPOSIT = 50
MAX_DEPOSIT = 100000

STOCK_REFRESH_MINUTES = 5
STOCK_CACHE_TTL = STOCK_REFRESH_MINUTES * 60

DEMO_STATS = False

# Demo values (used only when DEMO_STATS = True)
DEMO_USERS = 1250
DEMO_ORDERS = 842
DEMO_SALES = 21500
DEMO_STOCK = 340

# ------------------------------------------------------------
# PRODUCTS  (edit here — no other place hard-codes products)
# ------------------------------------------------------------
PRODUCTS = {
    "p1": {
        "name": "Meesho JSON ₹120 off",
        "price": 20,
        "emoji": "📦",
        "display_value": "₹120",
        "note": "",
    },
    "p2": {
        "name": "Meesho JSON ₹205 off",
        "price": 27,
        "emoji": "📦",
        "display_value": "₹205",
        "note": "",
    },
    "p3": {
        "name": "Meesho Fresh Number",
        "price": 15,
        "emoji": "🌿",
        "display_value": "Random discount/value",
        "note": "🎯 Random discount/value",
    },
}

# Paths (Linux compatible, relative to bot.py)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
QR_PATH = os.path.join(BASE_DIR, "qr.png")
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "store.db"))

# ============================================================
#                        LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("store-bot")

# ============================================================
#                        DATABASE
# ============================================================

def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def db_init() -> None:
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                first_name  TEXT,
                username    TEXT,
                balance     INTEGER DEFAULT 0,
                created_at  TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS products (
                product_id  TEXT PRIMARY KEY,
                name        TEXT,
                price       INTEGER
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS inventory (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id  TEXT NOT NULL,
                content     TEXT NOT NULL,
                sold        INTEGER DEFAULT 0,
                created_at  TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS deposits (
                deposit_id  TEXT PRIMARY KEY,
                user_id     INTEGER,
                amount      INTEGER,
                status      TEXT DEFAULT 'pending',
                screenshot  TEXT,
                created_at  TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id     TEXT PRIMARY KEY,
                user_id      INTEGER,
                product_id   TEXT,
                product_name TEXT,
                price        INTEGER,
                item_content TEXT,
                status       TEXT DEFAULT 'completed',
                created_at   TEXT
            )
        """)
        for pid, p in PRODUCTS.items():
            c.execute(
                "INSERT OR REPLACE INTO products(product_id, name, price) VALUES(?,?,?)",
                (pid, p["name"], p["price"]),
            )
        conn.commit()
    finally:
        conn.close()


def get_or_create_user(tg_user) -> sqlite3.Row:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO users(user_id, first_name, username, balance, created_at) "
            "VALUES(?,?,?,0,?)",
            (tg_user.id, tg_user.first_name or "", tg_user.username or "", now()),
        )
        conn.execute(
            "UPDATE users SET first_name=?, username=? WHERE user_id=?",
            (tg_user.first_name or "", tg_user.username or "", tg_user.id),
        )
        conn.commit()
        return conn.execute(
            "SELECT * FROM users WHERE user_id=?", (tg_user.id,)
        ).fetchone()
    finally:
        conn.close()


def get_user(user_id: int):
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
    finally:
        conn.close()


def get_stock(product_id: str) -> int:
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM inventory WHERE product_id=? AND sold=0",
            (product_id,),
        ).fetchone()["c"]
    finally:
        conn.close()


def _real_total_stock() -> int:
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM inventory WHERE sold=0"
        ).fetchone()["c"]
    finally:
        conn.close()


# ------- Live stock cache (smoothing / refresh) ------------

_stock_cache = {
    "value": 0,
    "ts": 0.0,
}


def get_display_stock() -> int:
    """
    Returns the stock to display.
    Never greater than real stock. Refreshes at most every STOCK_CACHE_TTL.
    """
    real = _real_total_stock()
    now_ts = time.time()

    if now_ts - _stock_cache["ts"] >= STOCK_CACHE_TTL:
        _stock_cache["value"] = real
        _stock_cache["ts"] = now_ts
    else:
        # If real dropped below cached, immediately use real (never overstate).
        if real < _stock_cache["value"]:
            _stock_cache["value"] = real
            _stock_cache["ts"] = now_ts

    return min(_stock_cache["value"], real)


def get_stats() -> dict:
    conn = get_conn()
    try:
        users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        stock = conn.execute(
            "SELECT COUNT(*) AS c FROM inventory WHERE sold=0"
        ).fetchone()["c"]
        row = conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(price),0) AS s "
            "FROM orders WHERE status='completed'"
        ).fetchone()
        return {
            "users": users,
            "stock": stock,
            "orders": row["c"],
            "sales": row["s"],
        }
    finally:
        conn.close()


# --------------------- Deposits ----------------------------

def generate_deposit_id() -> str:
    conn = get_conn()
    try:
        while True:
            dep_id = "DEP" + "".join(random.choices(string.digits, k=6))
            exists = conn.execute(
                "SELECT 1 FROM deposits WHERE deposit_id=?", (dep_id,)
            ).fetchone()
            if not exists:
                return dep_id
    finally:
        conn.close()


def create_deposit(dep_id: str, user_id: int, amount: int) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO deposits(deposit_id, user_id, amount, status, created_at) "
            "VALUES(?,?,?,'pending',?)",
            (dep_id, user_id, amount, now()),
        )
        conn.commit()
    finally:
        conn.close()


def get_deposit(dep_id: str):
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM deposits WHERE deposit_id=?", (dep_id,)
        ).fetchone()
    finally:
        conn.close()


def set_deposit_screenshot(dep_id: str, file_id: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE deposits SET screenshot=? WHERE deposit_id=?",
            (file_id, dep_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_pending_deposits(limit: int = 15):
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM deposits WHERE status='pending' "
            "ORDER BY created_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()


def approve_deposit(dep_id: str):
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        d = conn.execute(
            "SELECT * FROM deposits WHERE deposit_id=?", (dep_id,)
        ).fetchone()
        if not d or d["status"] != "pending":
            conn.rollback()
            return False, None
        conn.execute(
            "UPDATE deposits SET status='approved' WHERE deposit_id=?", (dep_id,)
        )
        conn.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id=?",
            (d["amount"], d["user_id"]),
        )
        conn.commit()
        return True, d
    except Exception:
        conn.rollback()
        logger.exception("approve_deposit failed")
        return False, None
    finally:
        conn.close()


def reject_deposit(dep_id: str):
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        d = conn.execute(
            "SELECT * FROM deposits WHERE deposit_id=?", (dep_id,)
        ).fetchone()
        if not d or d["status"] != "pending":
            conn.rollback()
            return False, None
        conn.execute(
            "UPDATE deposits SET status='rejected' WHERE deposit_id=?", (dep_id,)
        )
        conn.commit()
        return True, d
    except Exception:
        conn.rollback()
        return False, None
    finally:
        conn.close()


def get_all_users(limit: int = 50):
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM users ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()


# --------------------- Orders ------------------------------

def generate_order_id() -> str:
    """Generate a unique ORD-XXXXXX order ID."""
    conn = get_conn()
    try:
        while True:
            oid = "ORD-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
            exists = conn.execute(
                "SELECT 1 FROM orders WHERE order_id=?", (oid,)
            ).fetchone()
            if not exists:
                return oid
    finally:
        conn.close()


def do_purchase(user_id: int, product_id: str):
    """
    Atomic purchase:
      - check balance
      - check stock
      - deduct balance
      - reserve one inventory item
      - create order with unique order_id
    Returns (status, payload)
      status in: ok | insufficient | out_of_stock | invalid | error
      payload for ok = dict(order_id, item, new_balance, price, product_name)
    """
    if product_id not in PRODUCTS:
        return "invalid", None

    price = PRODUCTS[product_id]["price"]
    product_name = PRODUCTS[product_id]["name"]

    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")

        user = conn.execute(
            "SELECT balance FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if user is None:
            conn.rollback()
            return "error", None

        if user["balance"] < price:
            conn.rollback()
            return "insufficient", None

        inv = conn.execute(
            "SELECT id, content FROM inventory "
            "WHERE product_id=? AND sold=0 ORDER BY id ASC LIMIT 1",
            (product_id,),
        ).fetchone()
        if inv is None:
            conn.rollback()
            return "out_of_stock", None

        # generate order id inside transaction (checking uniqueness)
        oid = None
        for _ in range(10):
            candidate = "ORD-" + "".join(
                random.choices(string.ascii_uppercase + string.digits, k=6)
            )
            exists = conn.execute(
                "SELECT 1 FROM orders WHERE order_id=?", (candidate,)
            ).fetchone()
            if not exists:
                oid = candidate
                break
        if oid is None:
            conn.rollback()
            return "error", None

        conn.execute(
            "UPDATE users SET balance = balance - ? WHERE user_id=?",
            (price, user_id),
        )
        conn.execute("UPDATE inventory SET sold=1 WHERE id=?", (inv["id"],))
        conn.execute(
            "INSERT INTO orders(order_id, user_id, product_id, product_name, price, "
            "item_content, status, created_at) VALUES(?,?,?,?,?,?,'completed',?)",
            (oid, user_id, product_id, product_name, price, inv["content"], now()),
        )

        new_balance = user["balance"] - price
        conn.commit()
        return "ok", {
            "order_id": oid,
            "item": inv["content"],
            "new_balance": new_balance,
            "price": price,
            "product_name": product_name,
        }
    except Exception:
        conn.rollback()
        logger.exception("do_purchase failed")
        return "error", None
    finally:
        conn.close()


def add_inventory_items(product_id: str, items: list) -> int:
    if not items:
        return 0
    conn = get_conn()
    try:
        for it in items:
            conn.execute(
                "INSERT INTO inventory(product_id, content, sold, created_at) "
                "VALUES(?,?,0,?)",
                (product_id, it, now()),
            )
        conn.commit()
        return len(items)
    finally:
        conn.close()


# ============================================================
#                      HELPERS / UI
# ============================================================

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def safe_answer(query, text: str | None = None, show_alert: bool = False):
    try:
        await query.answer(text=text, show_alert=show_alert)
    except Exception:
        pass


async def safe_edit(query, ctx, text: str, kb):
    """Edit current message; fall back to delete+send if editing fails."""
    chat_id = query.message.chat_id
    try:
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=kb
        )
        return
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return
        logger.debug("edit_message_text failed: %s", e)
    except Exception:
        logger.exception("safe_edit edit failed")

    try:
        await query.message.delete()
    except Exception:
        pass
    try:
        await ctx.bot.send_message(
            chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=kb
        )
    except Exception:
        logger.exception("safe_edit fallback failed")


# ------------------------ Force Join ------------------------

async def is_member(ctx, user_id: int) -> bool:
    """Return True if user is a member of the force-join channel."""
    if not FORCE_JOIN_ENABLED:
        return True
    try:
        member = await ctx.bot.get_chat_member(FORCE_JOIN_CHANNEL, user_id)
        status = member.status
        if status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
            return False
        if status == ChatMemberStatus.RESTRICTED:
            return bool(getattr(member, "is_member", False))
        return True
    except BadRequest as e:
        logger.warning("Force-join check failed (BadRequest): %s", e)
        # Can't verify → let them through to avoid lockout; admin should fix config
        return True
    except Exception as e:
        logger.warning("Force-join check failed: %s", e)
        return True


def force_join_text() -> str:
    return (
        "━━━━━━━━━━━━━━━━━━\n"
        "🔒 <b>CHANNEL JOIN REQUIRED</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "👋 Welcome!\n\n"
        "To access the store, please join our official channel first.\n\n"
        "📢 Join the channel and then tap:"
    )


def force_join_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📢 Join Official Channel", url=FORCE_JOIN_URL)],
            [InlineKeyboardButton("✅ I've Joined", callback_data="check_join")],
        ]
    )


# ------------------------- Keyboards ------------------------

def menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("💎 Add Funds", callback_data="addfunds"),
                InlineKeyboardButton("🛍️ Browse Products", callback_data="browse"),
            ],
            [
                InlineKeyboardButton("📊 Store Stats", callback_data="stats"),
                InlineKeyboardButton("🎧 Support / Help", callback_data="support"),
            ],
        ]
    )


def menu_text(user_row, stock: int) -> str:
    name = html.escape(user_row["first_name"] or "User")
    return (
        "✨ <b>PREMIUM STORE</b> ✨\n\n"
        f"👋 Welcome, <b>{name}</b>!\n\n"
        f"🆔 User ID: <code>{user_row['user_id']}</code>\n"
        f"💰 Wallet Balance: <b>₹{user_row['balance']}</b>\n"
        f"📦 Live Stock: <b>{stock}</b>\n\n"
        "⚡ <i>Fast • Secure • Automated</i>\n\n"
        "━━━━━━━━━━━━━━━━━━"
    )


def products_text() -> str:
    lines = ["🛍️ <b>OUR PRODUCTS</b>", "━━━━━━━━━━━━━━━━━━\n"]
    for p in PRODUCTS.values():
        lines.append(f"{p['emoji']} <b>{p['name']}</b>")
        lines.append(f"💰 ₹{p['price']}")
        if p.get("note"):
            lines.append(p["note"])
        lines.append("")
    return "\n".join(lines).strip()


def products_kb() -> InlineKeyboardMarkup:
    rows = []
    for pid, p in PRODUCTS.items():
        rows.append(
            [
                InlineKeyboardButton(
                    f"🛒 Buy ₹{p['price']}", callback_data=f"buy:{pid}"
                )
            ]
        )
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


def addstock_content():
    rows = []
    for i, (pid, p) in enumerate(PRODUCTS.items(), 1):
        rows.append(
            [
                InlineKeyboardButton(
                    f"{i}️⃣ {p['name']} • ₹{p['price']}",
                    callback_data=f"admin:stock:{pid}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="admin:panel")])
    text = "📦 <b>ADD STOCK</b>\n\nSelect product:"
    return text, InlineKeyboardMarkup(rows)


def admin_panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📦 Add Stock", callback_data="admin:addstock"),
                InlineKeyboardButton("📊 Statistics", callback_data="admin:stats"),
            ],
            [
                InlineKeyboardButton(
                    "💰 Pending Deposits", callback_data="admin:deposits"
                ),
                InlineKeyboardButton("👥 Users", callback_data="admin:users"),
            ],
        ]
    )


def stats_text() -> str:
    s = get_stats()
    if DEMO_STATS:
        return (
            "📊 <b>STORE STATISTICS</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "🧪 <b>DEMO STATISTICS</b>\n"
            f"👥 Total Users: <b>{DEMO_USERS}</b>\n"
            f"📦 Live Stock: <b>{DEMO_STOCK}</b>\n"
            f"🛒 Orders Completed: <b>{DEMO_ORDERS}</b>\n"
            f"💰 Total Sales: ₹<b>{DEMO_SALES}</b>\n\n"
            "⚡ <i>Store Activity</i>\n"
            "━━━━━━━━━━━━━━━━━━"
        )
    return (
        "📊 <b>STORE STATISTICS</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 Total Users: <b>{s['users']}</b>\n"
        f"📦 Live Stock: <b>{s['stock']}</b>\n"
        f"🛒 Orders Completed: <b>{s['orders']}</b>\n"
        f"💰 Total Sales: ₹<b>{s['sales']}</b>\n\n"
        "⚡ <i>Store Activity</i>\n"
        "━━━━━━━━━━━━━━━━━━"
    )


# ============================================================
#                    USER COMMANDS
# ============================================================

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # Reset any in-progress state so /start always returns to home
    ctx.user_data.clear()

    get_or_create_user(user)

    # Force-join gate
    if FORCE_JOIN_ENABLED:
        member = await is_member(ctx, user.id)
        if not member:
            await update.message.reply_text(
                force_join_text(),
                parse_mode=ParseMode.HTML,
                reply_markup=force_join_kb(),
            )
            return

    user_row = get_user(user.id)
    stock = get_display_stock()
    await update.message.reply_text(
        menu_text(user_row, stock),
        parse_mode=ParseMode.HTML,
        reply_markup=menu_kb(),
    )


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text(
        "✅ Cancelled.\n\nUse /start to open the menu.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Access denied.")
        return
    await update.message.reply_text(
        "🔐 <b>ADMIN PANEL</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_panel_kb(),
    )


async def cmd_addstock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Access denied.")
        return
    text, kb = addstock_content()
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


# ============================================================
#                   MESSAGE HANDLERS
# ============================================================

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg is None:
        return
    user_id = update.effective_user.id

    dep_id = ctx.user_data.get("pending_deposit")
    if not dep_id:
        await msg.reply_text(
            "📸 No active deposit.\nUse 💎 Add Funds from /start first.",
        )
        return

    dep = get_deposit(dep_id)
    if not dep or dep["user_id"] != user_id or dep["status"] != "pending":
        ctx.user_data.pop("pending_deposit", None)
        await msg.reply_text("⚠️ This deposit is no longer active.")
        return

    if msg.photo:
        file_id = msg.photo[-1].file_id
    elif msg.document:
        file_id = msg.document.file_id
    else:
        return

    set_deposit_screenshot(dep_id, file_id)
    ctx.user_data.pop("pending_deposit", None)

    await msg.reply_text(
        "✅ <b>Screenshot Received</b>\n\n"
        f"Order ID: <code>{dep_id}</code>\n"
        f"Amount: ₹{dep['amount']}\n\n"
        "⏳ <b>Pending Verification</b>\n"
        "Your deposit will be credited after admin approval.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 Main Menu", callback_data="menu")]]
        ),
    )

    for admin_id in ADMIN_IDS:
        try:
            await ctx.bot.send_message(
                chat_id=admin_id,
                text=(
                    "🔔 <b>New deposit pending</b>\n\n"
                    f"Order ID: <code>{dep_id}</code>\n"
                    f"Amount: ₹{dep['amount']}\n"
                    f"User: <code>{user_id}</code>"
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg is None or not msg.text:
        return
    user = update.effective_user
    text = msg.text.strip()

    # ---- Admin adding inventory ----
    if is_admin(user.id) and ctx.user_data.get("admin_stock"):
        pid = ctx.user_data["admin_stock"]
        if pid not in PRODUCTS:
            ctx.user_data.pop("admin_stock", None)
            await msg.reply_text("⚠️ Invalid product state. Use /addstock again.")
            return
        items = [line.strip() for line in text.split("\n") if line.strip()]
        if not items:
            await msg.reply_text("Send at least one item (one per line).")
            return
        try:
            added = add_inventory_items(pid, items)
        except Exception:
            logger.exception("add_inventory_items failed")
            await msg.reply_text("⚠️ Database error. Try again.")
            return
        await msg.reply_text(
            f"✅ Added <b>{added}</b> item(s) to "
            f"<b>{html.escape(PRODUCTS[pid]['name'])}</b>.\n\n"
            "Send more items, or /cancel to stop.",
            parse_mode=ParseMode.HTML,
        )
        return

    # ---- Custom deposit amount ----
    if ctx.user_data.get("awaiting_amount"):
        try:
            amount = int(text)
        except ValueError:
            await msg.reply_text("❌ Invalid amount. Please send a number.")
            return
        if amount < MIN_DEPOSIT:
            await msg.reply_text(f"❌ Minimum deposit is ₹{MIN_DEPOSIT}.")
            return
        if amount > MAX_DEPOSIT:
            await msg.reply_text(f"❌ Maximum deposit is ₹{MAX_DEPOSIT}.")
            return

        ctx.user_data.pop("awaiting_amount", None)
        dep_id = generate_deposit_id()
        create_deposit(dep_id, user.id, amount)
        ctx.user_data["pending_deposit"] = dep_id

        caption = (
            "💳 <b>PAYMENT DETAILS</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"Amount: ₹<b>{amount}</b>\n"
            f"Order ID: <code>{dep_id}</code>\n\n"
            "Scan the QR and complete the payment.\n\n"
            "📸 <b>Send Payment Screenshot</b>"
        )
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Cancel", callback_data="cancel_dep")]]
        )

        if os.path.exists(QR_PATH):
            try:
                with open(QR_PATH, "rb") as f:
                    await ctx.bot.send_photo(
                        chat_id=msg.chat_id,
                        photo=f,
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                        reply_markup=kb,
                    )
                return
            except Exception:
                logger.exception("Failed sending QR photo")
        await ctx.bot.send_message(
            chat_id=msg.chat_id,
            text=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
        return

    await msg.reply_text("Use /start to open the menu.")


# ============================================================
#                   CALLBACK HANDLERS
# ============================================================

async def show_menu(query, ctx):
    user_row = get_or_create_user(query.from_user)
    stock = get_display_stock()
    await safe_edit(query, ctx, menu_text(user_row, stock), menu_kb())


async def show_products(query, ctx):
    await safe_edit(query, ctx, products_text(), products_kb())


async def show_stats(query, ctx):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="menu")]])
    await safe_edit(query, ctx, stats_text(), kb)


async def show_support(query, ctx):
    handle = SUPPORT_USERNAME[1:] if SUPPORT_USERNAME.startswith("@") else SUPPORT_USERNAME
    text = (
        "🎧 <b>SUPPORT</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "Need help with an order or payment?\n\n"
        "📩 Contact Admin"
    )
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💬 Contact Support", url=f"https://t.me/{handle}")],
            [InlineKeyboardButton("⬅️ Back", callback_data="menu")],
        ]
    )
    await safe_edit(query, ctx, text, kb)


async def show_addfunds(query, ctx):
    text = (
        "💰 <b>ADD FUNDS</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"Minimum deposit: ₹{MIN_DEPOSIT}\n\n"
        "Choose amount:"
    )
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("₹50", callback_data="amt:50"),
                InlineKeyboardButton("₹100", callback_data="amt:100"),
            ],
            [
                InlineKeyboardButton("₹200", callback_data="amt:200"),
                InlineKeyboardButton("₹500", callback_data="amt:500"),
            ],
            [InlineKeyboardButton("✏️ Custom Amount", callback_data="amt_custom")],
            [InlineKeyboardButton("⬅️ Back", callback_data="menu")],
        ]
    )
    await safe_edit(query, ctx, text, kb)


async def create_deposit_flow(query, ctx, amount: int):
    user_id = query.from_user.id
    dep_id = generate_deposit_id()
    create_deposit(dep_id, user_id, amount)
    ctx.user_data["pending_deposit"] = dep_id

    caption = (
        "💳 <b>PAYMENT DETAILS</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"Amount: ₹<b>{amount}</b>\n"
        f"Order ID: <code>{dep_id}</code>\n\n"
        "Scan the QR and complete the payment.\n\n"
        "📸 <b>Send Payment Screenshot</b>"
    )
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Cancel", callback_data="cancel_dep")]]
    )

    chat_id = query.message.chat_id
    try:
        await query.message.delete()
    except Exception:
        pass

    if os.path.exists(QR_PATH):
        try:
            with open(QR_PATH, "rb") as f:
                await ctx.bot.send_photo(
                    chat_id=chat_id,
                    photo=f,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb,
                )
            return
        except Exception:
            logger.exception("Failed sending QR photo")

    await ctx.bot.send_message(
        chat_id=chat_id,
        text=caption,
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


async def cb_buy(query, pid: str, ctx):
    """Show order confirmation (no deduction yet)."""
    if pid not in PRODUCTS:
        await safe_answer(query, "❌ Invalid product", True)
        return

    if get_stock(pid) <= 0:
        await safe_answer(query, "⚠️ OUT OF STOCK", True)
        return

    p = PRODUCTS[pid]
    user_row = get_user(query.from_user.id)
    balance = user_row["balance"]
    after = balance - p["price"]

    text = (
        "🛍️ <b>ORDER CONFIRMATION</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"📦 Product:\n<b>{html.escape(p['name'])}</b>\n\n"
        f"💰 Price: ₹<b>{p['price']}</b>\n"
        f"💳 Wallet Balance: ₹<b>{balance}</b>\n\n"
        f"💵 Balance After Purchase: ₹<b>{after if after >= 0 else 0}</b>\n\n"
        "Please confirm your purchase."
    )
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Confirm Purchase", callback_data=f"confirm:{pid}"
                )
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="browse")],
        ]
    )
    await safe_edit(query, ctx, text, kb)


async def cb_confirm(query, pid: str, ctx):
    """Execute purchase atomically after re-checking balance & stock."""
    if pid not in PRODUCTS:
        await safe_answer(query, "❌ Invalid product", True)
        return

    p = PRODUCTS[pid]
    user_id = query.from_user.id

    status, payload =