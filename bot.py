import os
import html
import random
import string
import sqlite3
import logging
from datetime import datetime, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
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

# Read from environment (Railway) or fall back to the literal below.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN")

# Comma separated admin IDs, e.g. "111111,222222"
ADMIN_IDS = [
    int(x)
    for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",")
    if x.strip().lstrip("-").isdigit()
]

# Support username shown to users (with or without @)
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "@yourusername")

# Paths (Linux compatible, relative to bot.py)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
QR_PATH = os.path.join(BASE_DIR, "qr.png")
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "store.db"))

# ------------------------------------------------------------
# PRODUCTS  (edit here — no other place hard-codes products)
# ------------------------------------------------------------
PRODUCTS = {
    "p1": {
        "name": "Meesho JSON ₹120 off",
        "price": 20,
        "emoji": "📦",
        "note": "",
    },
    "p2": {
        "name": "Meesho JSON ₹205 off",
        "price": 27,
        "emoji": "📦",
        "note": "",
    },
    "p3": {
        "name": "Meesho Fresh Number",
        "price": 15,
        "emoji": "🌿",
        "note": "🎯 Random discount/value",
    },
}

MIN_DEPOSIT = 50
MAX_DEPOSIT = 1000

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
                order_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER,
                product_id  TEXT,
                price       INTEGER,
                item_content TEXT,
                created_at  TEXT
            )
        """)

        # Seed / sync products from config
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


def total_stock() -> int:
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM inventory WHERE sold=0"
        ).fetchone()["c"]
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


def get_stats() -> dict:
    conn = get_conn()
    try:
        users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        stock = conn.execute(
            "SELECT COUNT(*) AS c FROM inventory WHERE sold=0"
        ).fetchone()["c"]
        row = conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(price),0) AS s FROM orders"
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
    """Atomically approve a deposit and credit the user's wallet."""
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


# --------------------- Purchase ----------------------------

def do_purchase(user_id: int, product_id: str):
    """
    Atomic purchase:
      1. check balance
      2. check stock
      3. deduct balance
      4. mark one inventory item sold
      5. record order
    Returns (status, payload)
      status in: ok | insufficient | out_of_stock | invalid | error
    """
    if product_id not in PRODUCTS:
        return "invalid", None

    price = PRODUCTS[product_id]["price"]
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

        conn.execute(
            "UPDATE users SET balance = balance - ? WHERE user_id=?",
            (price, user_id),
        )
        conn.execute("UPDATE inventory SET sold=1 WHERE id=?", (inv["id"],))
        conn.execute(
            "INSERT INTO orders(user_id, product_id, price, item_content, created_at) "
            "VALUES(?,?,?,?,?)",
            (user_id, product_id, price, inv["content"], now()),
        )
        conn.commit()
        return "ok", inv["content"]
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
    """Edit the current message, but fall back if it was a photo message."""
    chat_id = query.message.chat_id
    try:
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=kb
        )
        return
    except Exception:
        pass
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
        f"💰 Wallet Balance: ₹<b>{user_row['balance']}</b>\n"
        f"📦 Live Stock: <b>{stock}</b>\n\n"
        "⚡ Fast • Secure • Automated"
    )


def products_text() -> str:
    lines = ["🛍️ <b>OUR PRODUCTS</b>\n"]
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


# ============================================================
#                    USER COMMANDS
# ============================================================

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_row = get_or_create_user(user)
    stock = total_stock()
    await update.message.reply_text(
        menu_text(user_row, stock),
        parse_mode=ParseMode.HTML,
        reply_markup=menu_kb(),
    )


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("admin_stock", None)
    ctx.user_data.pop("awaiting_amount", None)
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

    # Notify admins
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
            await msg.reply_text(
                f"❌ Minimum deposit is ₹{MIN_DEPOSIT}.",
            )
            return
        if amount > MAX_DEPOSIT:
            await msg.reply_text(
                f"❌ Maximum deposit is ₹{MAX_DEPOSIT}.",
            )
            return

        ctx.user_data.pop("awaiting_amount", None)
        dep_id = generate_deposit_id()
        create_deposit(dep_id, user.id, amount)
        ctx.user_data["pending_deposit"] = dep_id

        caption = (
            "💳 <b>PAYMENT DETAILS</b>\n\n"
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

    # ---- Fallback ----
    await msg.reply_text("Use /start to open the menu.")


# ============================================================
#                   CALLBACK HANDLERS
# ============================================================

async def show_menu(query, ctx):
    user_row = get_or_create_user(query.from_user)
    stock = total_stock()
    await safe_edit(query, ctx, menu_text(user_row, stock), menu_kb())


async def show_products(query, ctx):
    await safe_edit(query, ctx, products_text(), products_kb())


async def show_stats(query, ctx):
    s = get_stats()
    text = (
        "📊 <b>STORE STATISTICS</b>\n\n"
        f"👥 Users: <b>{s['users']}</b>\n"
        f"📦 Live Stock: <b>{s['stock']}</b>\n"
        f"🛒 Orders Completed: <b>{s['orders']}</b>\n"
        f"💰 Total Sales: ₹<b>{s['sales']}</b>"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="menu")]])
    await safe_edit(query, ctx, text, kb)


async def show_support(query, ctx):
    handle = SUPPORT_USERNAME[1:] if SUPPORT_USERNAME.startswith("@") else SUPPORT_USERNAME
    text = (
        "🎧 <b>SUPPORT</b>\n\n"
        "Need help with an order or payment?\n\n"
        "📩 Contact Admin"
    )
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "💬 Contact Support", url=f"https://t.me/{handle}"
                )
            ],
            [InlineKeyboardButton("⬅️ Back", callback_data="menu")],
        ]
    )
    await safe_edit(query, ctx, text, kb)


async def show_addfunds(query, ctx):
    text = (
        "💰 <b>ADD FUNDS</b>\n\n"
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
        "💳 <b>PAYMENT DETAILS</b>\n\n"
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
    if pid not in PRODUCTS:
        await safe_answer(query, "❌ Invalid product", True)
        return
    p = PRODUCTS[pid]
    if get_stock(pid) <= 0:
        await safe_answer(query, "⚠️ OUT OF STOCK", True)
        return
    user_row = get_user(query.from_user.id)
    text = (
        "🛒 <b>CONFIRM PURCHASE</b>\n\n"
        f"{p['emoji']} <b>{p['name']}</b>\n"
        f"💰 Price: ₹<b>{p['price']}</b>\n"
        f"💼 Your Balance: ₹<b>{user_row['balance']}</b>\n\n"
        "Proceed with this purchase?"
    )
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Confirm", callback_data=f"confirm:{pid}"
                )
            ],
            [InlineKeyboardButton("⬅️ Back", callback_data="browse")],
        ]
    )
    await safe_edit(query, ctx, text, kb)


async def cb_confirm(query, pid: str, ctx):
    if pid not in PRODUCTS:
        await safe_answer(query, "❌ Invalid product", True)
        return

    status, payload = do_purchase(query.from_user.id, pid)

    if status == "insufficient":
        await safe_answer(query, "❌ Insufficient balance", True)
        return
    if status == "out_of_stock":
        await safe_answer(query, "⚠️ OUT OF STOCK", True)
        return
    if status != "ok":
        await safe_answer(query, "⚠️ Something went wrong. Try again.", True)
        return

    p = PRODUCTS[pid]
    text = (
        "✅ <b>ORDER DELIVERED</b>\n\n"
        f"{p['emoji']} <b>{html.escape(p['name'])}</b>\n"
        f"💰 Paid: ₹<b>{p['price']}</b>\n\n"
        "📦 <b>Your Item:</b>\n"
        f"<code>{html.escape(payload)}</code>"
    )
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🛍️ Buy More", callback_data="browse")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="menu")],
        ]
    )
    await safe_edit(query, ctx, text, kb)


# ---------------- Admin callbacks ----------------

async def admin_deposits_view(query, ctx):
    deps = get_pending_deposits()
    if not deps:
        text = "💰 <b>PENDING DEPOSITS</b>\n\nNo pending deposits."
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back", callback_data="admin:panel")]]
        )
        await safe_edit(query, ctx, text, kb)
        return
    rows = []
    for d in deps:
        rows.append(
            [
                InlineKeyboardButton(
                    f"{d['deposit_id']} • ₹{d['amount']}",
                    callback_data=f"admin:dep:{d['deposit_id']}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="admin:panel")])
    text = f"💰 <b>PENDING DEPOSITS</b> ({len(deps)})\n\nSelect one to review:"
    await safe_edit(query, ctx, text, InlineKeyboardMarkup(rows))


async def admin_deposit_view_one(query, ctx, dep_id: str):
    d = get_deposit(dep_id)
    if not d:
        await safe_answer(query, "Not found", True)
        return
    if d["status"] != "pending":
        await safe_answer(query, f"Already {d['status']}", True)
        return

    u = get_user(d["user_id"])
    uname = html.escape((u["first_name"] if u else "") or "")
    caption = (
        "💰 <b>DEPOSIT REVIEW</b>\n\n"
        f"Order ID: <code>{d['deposit_id']}</code>\n"
        f"User: {uname} (<code>{d['user_id']}</code>)\n"
        f"Amount: ₹<b>{d['amount']}</b>\n"
        f"Created: {d['created_at']}"
    )
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Approve", callback_data=f"admin:appr:{dep_id}"
                ),
                InlineKeyboardButton(
                    "❌ Reject", callback_data=f"admin:rej:{dep_id}"
                ),
            ],
            [InlineKeyboardButton("⬅️ Back", callback_data="admin:deposits")],
        ]
    )

    if d["screenshot"]:
        try:
            await ctx.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=d["screenshot"],
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
            try:
                await query.message.delete()
            except Exception:
                pass
            return
        except Exception:
            logger.exception("Failed to send deposit screenshot")

    await safe_edit(query, ctx, caption, kb)


async def admin_users_view(query, ctx):
    users = get_all_users(50)
    if not users:
        text = "👥 <b>USERS</b>\n\nNo users yet."
    else:
        lines = [f"👥 <b>USERS</b> (latest {len(users)})\n"]
        for u in users:
            uname = u["username"] and f"@{u['username']}" or "—"
            lines.append(
                f"• <code>{u['user_id']}</code> — "
                f"{html.escape(u['first_name'] or '')} ({html.escape(uname)}) "
                f"— ₹{u['balance']}"
            )
        text = "\n".join(lines)
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Back", callback_data="admin:panel")]]
    )
    await safe_edit(query, ctx, text, kb)


async def handle_admin_callback(query, ctx, data: str):
    if not is_admin(query.from_user.id):
        await safe_answer(query, "❌ Access denied", True)
        return

    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "panel":
        await safe_edit(query, ctx, "🔐 <b>ADMIN PANEL</b>", admin_panel_kb())

    elif action == "addstock":
        text, kb = addstock_content()
        await safe_edit(query, ctx, text, kb)

    elif action == "stock":
        pid = parts[2] if len(parts) > 2 else ""
        if pid not in PRODUCTS:
            await safe_answer(query, "❌ Invalid product", True)
            return
        ctx.user_data["admin_stock"] = pid
        p = PRODUCTS[pid]
        text = (
            "📦 <b>ADD STOCK</b>\n\n"
            f"Product: <b>{html.escape(p['name'])}</b> (₹{p['price']})\n\n"
            "Send inventory items now.\n"
            "• One item per line\n"
            "• Send multiple messages to add more\n"
            "• Send /cancel when done"
        )
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Done", callback_data="admin:panel"
                    )
                ]
            ]
        )
        await safe_edit(query, ctx, text, kb)

    elif action == "stats":
        s = get_stats()
        text = (
            "📊 <b>STORE STATISTICS</b>\n\n"
            f"👥 Users: <b>{s['users']}</b>\n"
            f"📦 Live Stock: <b>{s['stock']}</b>\n"
            f"🛒 Orders Completed: <b>{s['orders']}</b>\n"
            f"💰 Total Sales: ₹<b>{s['sales']}</b>"
        )
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back", callback_data="admin:panel")]]
        )
        await safe_edit(query, ctx, text, kb)

    elif action == "deposits":
        await admin_deposits_view(query, ctx)

    elif action == "dep":
        dep_id = parts[2] if len(parts) > 2 else ""
        await admin_deposit_view_one(query, ctx, dep_id)

    elif action in ("appr", "rej"):
        dep_id = parts[2] if len(parts) > 2 else ""
        if action == "appr":
            ok, d = approve_deposit(dep_id)
            if not ok:
                await safe_answer(query, "Already processed", True)
                return
            try:
                await ctx.bot.send_message(
                    chat_id=d["user_id"],
                    text=(
                        "✅ <b>Deposit Approved</b>\n\n"
                        f"₹{d['amount']} has been added to your wallet.\n"
                        f"Order ID: <code>{d['deposit_id']}</code>"
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            await safe_answer(query, "✅ Approved", True)
        else:
            ok, d = reject_deposit(dep_id)
            if not ok:
                await safe_answer(query, "Already processed", True)
                return
            try:
                await ctx.bot.send_message(
                    chat_id=d["user_id"],
                    text=(
                        "❌ <b>Deposit Rejected</b>\n\n"
                        f"Order ID: <code>{d['deposit_id']}</code>\n"
                        f"Amount: ₹{d['amount']}\n\n"
                        "Please contact support if you believe this is a mistake."
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            await safe_answer(query, "❌ Rejected", True)

        await admin_deposits_view(query, ctx)

    elif action == "users":
        await admin_users_view(query, ctx)

    else:
        await safe_answer(query, "❌ Invalid callback", True)


# ---------------- Main callback router ----------------

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is None or query.message is None:
        return

    data = query.data or ""

    try:
        if data == "menu":
            await safe_answer(query)
            await show_menu(query, ctx)

        elif data == "browse":
            await safe_answer(query)
            await show_products(query, ctx)

        elif data == "addfunds":
            await safe_answer(query)
            await show_addfunds(query, ctx)

        elif data.startswith("amt:"):
            await safe_answer(query)
            try:
                amount = int(data.split(":", 1)[1])
            except (ValueError, IndexError):
                await safe_answer(query, "❌ Invalid amount", True)
                return
            if amount < MIN_DEPOSIT:
                await safe_answer(
                    query, f"❌ Minimum deposit is ₹{MIN_DEPOSIT}", True
                )
                return
            await create_deposit_flow(query, ctx, amount)

        elif data == "amt_custom":
            await safe_answer(query)
            ctx.user_data["awaiting_amount"] = True
            text = (
                "✏️ <b>CUSTOM AMOUNT</b>\n\n"
                f"Send the amount you want to deposit (minimum ₹{MIN_DEPOSIT})."
            )
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Cancel", callback_data="addfunds")]]
            )
            await safe_edit(query, ctx, text, kb)

        elif data.startswith("buy:"):
            await safe_answer(query)
            await cb_buy(query, data.split(":", 1)[1], ctx)

        elif data.startswith("confirm:"):
            await safe_answer(query)
            await cb_confirm(query, data.split(":", 1)[1], ctx)

        elif data == "stats":
            await safe_answer(query)
            await show_stats(query, ctx)

        elif data == "support":
            await safe_answer(query)
            await show_support(query, ctx)

        elif data == "cancel_dep":
            await safe_answer(query)
            ctx.user_data.pop("pending_deposit", None)
            ctx.user_data.pop("awaiting_amount", None)
            await show_menu(query, ctx)

        elif data.startswith("admin:"):
            await safe_answer(query)
            await handle_admin_callback(query, ctx, data)

        else:
            await safe_answer(query, "❌ Invalid callback", True)

    except Exception:
        logger.exception("Callback handler error")
        await safe_answer(query, "⚠️ Something went wrong", True)


# ============================================================
#                      ERROR HANDLER
# ============================================================

async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled exception", exc_info=ctx.error)


# ============================================================
#                         MAIN
# ============================================================

def main():
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN":
        logger.error(
            "BOT_TOKEN is not configured. "
            "Set the BOT_TOKEN environment variable before starting."
        )
        raise SystemExit(1)

    if not ADMIN_IDS:
        logger.warning(
            "ADMIN_IDS is empty. Admin features will be inaccessible. "
            "Set ADMIN_IDS env var (e.g. ADMIN_IDS=123456789)."
        )

    db_init()
    logger.info("Database ready at %s", DB_PATH)
    logger.info("QR path: %s (exists=%s)", QR_PATH, os.path.exists(QR_PATH))

    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("addstock", cmd_addstock))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    # Callbacks
    app.add_handler(CallbackQueryHandler(on_callback))

    # Messages
    app.add_handler(
        MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo)
    )
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )

    # Global error handler
    app.add_error_handler(on_error)

    logger.info("Bot is starting (long polling)…")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()