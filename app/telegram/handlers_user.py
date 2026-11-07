import html
import re
from datetime import datetime, timezone
from math import ceil

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from app.config import settings
from app.database import get_admin_ids, get_connection, get_setting, upsert_user
from app.services import inventory as inventory_svc, orders as orders_svc, payments as payments_svc
from app.services.channels import STATUS_BADGES, fmt_money
from app.services.products import get_product

BUY_QTY, BUY_CONFIRM = range(2)
PROOF_METHOD, PROOF_PHOTO = range(2)

PAGE_SIZE = 5


# ---------------------------------------------------------------- helpers

def _esc(value):
    return html.escape(str(value or ""))


def _site_link(path):
    base = (settings.BASE_URL or "").rstrip("/")
    if not base.startswith("http") or "localhost" in base or "127.0.0.1" in base:
        return None
    return base + path


def _order_url(order_code):
    return _site_link(f"/order/{order_code}")


def _catalog():
    conn = get_connection()
    rows = conn.execute(
        """SELECT p.*, (SELECT COUNT(*) FROM inventory i
                        WHERE i.product_id = p.id AND i.status = 'available') AS stock
           FROM products p WHERE p.is_active = 1 ORDER BY p.id DESC"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _catalog_page(page):
    items = _catalog()
    pages = max(1, ceil(len(items) / PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    return items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE], page, pages, len(items)


def _catalog_keyboard(page=0):
    items, page, pages, total = _catalog_page(page)
    keyboard = []
    for p in items:
        stock = p["stock"]
        prefix = "⛔" if stock <= 0 else "🛍"
        label = f"{prefix} {p['title']} — {fmt_money(p['price_bdt'])} ({stock} left)"
        if len(label) > 60:
            label = label[:57] + "…"
        keyboard.append([
            InlineKeyboardButton(label, callback_data=f"prod_{p['id']}_{page}")
        ])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"browse_{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"browse_{page + 1}"))
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🏠 Main menu", callback_data="menu")])
    header = "🛍 <b>Products</b>"
    if total:
        header += f"\n<i>{total} product{'s' if total != 1 else ''} available</i>"
    return header, keyboard, total


def _main_menu(user_id=None):
    keyboard = [
        [InlineKeyboardButton("🛍 Browse Products", callback_data="browse_0")],
        [InlineKeyboardButton("📦 My Orders", callback_data="my_orders")],
        [InlineKeyboardButton("💳 Submit Payment Proof", callback_data="proof_start")],
        [InlineKeyboardButton("❓ Help", callback_data="help")],
    ]
    if user_id is not None and user_id in get_admin_ids():
        keyboard.append([InlineKeyboardButton("🔧 Admin Panel", callback_data="adm_menu")])
    return InlineKeyboardMarkup(keyboard)


def _clear_flow(context):
    for key in (
        "buy_product_id", "buy_page", "buy_stock", "buy_title", "buy_price", "buy_qty",
        "proof_order_id", "proof_order_code", "proof_method",
    ):
        context.user_data.pop(key, None)


def _remaining_minutes(order):
    try:
        exp = datetime.fromisoformat(order["expires_at"])
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        remaining = (exp - datetime.now(timezone.utc)).total_seconds() / 60
        return max(0, int(remaining))
    except Exception:
        return 0


# ---------------------------------------------------------------- entry points

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.full_name, u.username, None)
    await update.message.reply_text(
        f"👋 Welcome, {_esc(u.first_name)}!\n\n"
        "Buy Telegram accounts, Crunchyroll accounts and cookies. "
        "Pay via bKash/Nagad and get credentials instantly after verification.\n\n"
        "Use the menu below, or send /help to see all commands.",
        reply_markup=_main_menu(u.id),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = [
        "❓ <b>How to order</b>",
        "",
        "1. <b>/browse</b> — pick a product",
        "2. Choose quantity and confirm",
        "3. Pay the total via bKash or Nagad",
        "4. Submit the payment screenshot with <b>/proof</b>",
        "5. An admin verifies it and your credentials arrive here",
        "",
        "📋 <b>Commands</b>",
        "/browse — product catalog",
        "/buy &lt;id&gt; — start an order",
        "/orders — your orders and status",
        "/proof &lt;order_code&gt; — submit payment proof",
        "/cancel — stop the current step",
        "/help — this message",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text("Main menu:", reply_markup=_main_menu(q.from_user.id))


async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text(
        "❓ <b>Quick help</b>\n\n"
        "🛍 Browse → choose a product → pick quantity → confirm.\n"
        "💳 Pay with bKash/Nagad, then send /proof with the order code and a screenshot caption.\n"
        "📦 Track everything with /orders — status updates appear there and on the website.",
        parse_mode="HTML",
    )


async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


# ---------------------------------------------------------------- catalog

async def cmd_browse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, keyboard, total = _catalog_keyboard(0)
    if not total:
        await update.message.reply_text("No products available right now. Please check back soon.")
        return
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def browse_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    m = re.match(r"^browse(?:_(\d+))?$", q.data)
    page = int(m.group(1)) if m and m.group(1) else 0
    text, keyboard, total = _catalog_keyboard(page)
    if not total:
        await q.message.reply_text("No products available right now. Please check back soon.")
        return
    await q.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def product_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    m = re.match(r"^prod_(\d+)(?:_(\d+))?$", q.data)
    if not m:
        return
    product_id = int(m.group(1))
    page = int(m.group(2)) if m.group(2) else 0
    p = get_product(product_id)
    if p is None or not p["is_active"]:
        await q.message.reply_text("This product is no longer available.")
        return
    stock = inventory_svc.available_count(product_id)
    desc = (p.get("description") or "").strip()
    if len(desc) > 500:
        desc = desc[:500] + "…"
    text = (
        f"📦 <b>{_esc(p['title'])}</b>\n\n"
        f"💰 Price: {fmt_money(p['price_bdt'])} each\n"
        f"📊 In stock: {stock}\n"
    )
    if desc:
        text += f"\n{_esc(desc)}"
    buttons = []
    if stock > 0:
        buttons.append([InlineKeyboardButton("🛒 Buy now", callback_data=f"buy_{product_id}_{page}")])
    else:
        buttons.append([InlineKeyboardButton("⛔ Out of stock", callback_data="noop")])
    buttons.append([InlineKeyboardButton("⬅️ Back to products", callback_data=f"browse_{page}")])
    await q.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


# ---------------------------------------------------------------- buy flow

async def buy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args and context.args[0].isdigit():
        return await buy_start_by_id(update, context, int(context.args[0]), 0, via_command=True)
    text, keyboard, total = _catalog_keyboard(0)
    if not total:
        await update.message.reply_text("No products available right now.")
        return ConversationHandler.END
    await update.message.reply_text(
        f"{text}\n\nSend <code>/buy &lt;product_id&gt;</code> or pick a product:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ConversationHandler.END


async def buy_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    m = re.match(r"^buy_(\d+)(?:_(\d+))?$", q.data)
    if not m:
        return ConversationHandler.END
    return await buy_start_by_id(update, context, int(m.group(1)), int(m.group(2) or 0), via_command=False)


async def buy_start_by_id(update, context, product_id, page, via_command):
    _clear_flow(context)
    p = get_product(product_id)
    if p is None or not p["is_active"]:
        await _reply(update, "This product is no longer available.", via_command)
        return ConversationHandler.END
    stock = inventory_svc.available_count(product_id)
    if stock <= 0:
        await _reply(update, "❌ This product is out of stock right now.", via_command)
        return ConversationHandler.END
    context.user_data.update(
        buy_product_id=product_id,
        buy_page=page,
        buy_stock=stock,
        buy_title=p["title"],
        buy_price=p["price_bdt"],
    )
    max_qty = min(stock, orders_svc.MAX_QTY_PER_ITEM)
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_buy")]]
    )
    await _reply(
        update,
        f"📦 <b>{_esc(p['title'])}</b> — {fmt_money(p['price_bdt'])} each\n"
        f"📊 Stock: {stock}\n\nSend quantity (1-{max_qty}):",
        via_command,
        reply_markup=keyboard,
        parse_mode="HTML",
    )
    return BUY_QTY


async def buy_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    product_id = context.user_data.get("buy_product_id")
    if not product_id:
        await update.message.reply_text("This purchase session expired. Please start again with /browse.")
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    if not text.isdigit():
        await update.message.reply_text("Please send a number for the quantity, or /cancel.")
        return BUY_QTY
    p = get_product(product_id)
    if p is None or not p["is_active"]:
        _clear_flow(context)
        await update.message.reply_text("This product is no longer available.")
        return ConversationHandler.END
    stock = inventory_svc.available_count(product_id)
    max_qty = min(stock, orders_svc.MAX_QTY_PER_ITEM)
    if stock <= 0:
        _clear_flow(context)
        await update.message.reply_text("❌ This product just went out of stock.")
        return ConversationHandler.END
    qty = int(text)
    if qty < 1 or qty > max_qty:
        await update.message.reply_text(f"Quantity must be between 1 and {max_qty}.")
        return BUY_QTY
    context.user_data["buy_qty"] = qty
    context.user_data["buy_stock"] = stock
    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Confirm", callback_data=f"conf_{product_id}_{qty}"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_buy"),
        ]]
    )
    await update.message.reply_text(
        f"🧾 <b>{_esc(p['title'])}</b> × {qty}\n"
        f"💰 Total: {fmt_money(p['price_bdt'] * qty)}\n\nConfirm this order?",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    return BUY_CONFIRM


async def buy_qty_invalid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Please send a number for the quantity, or /cancel.")
    return BUY_QTY


async def buy_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    m = re.match(r"^conf_(\d+)_(\d+)$", q.data)
    if not m:
        return ConversationHandler.END
    product_id, qty = int(m.group(1)), int(m.group(2))
    user_id = q.from_user.id
    try:
        order = await orders_svc.place_order(user_id, [{"product_id": product_id, "qty": qty}], bot=context.bot)
    except orders_svc.OutOfStockError as e:
        _clear_flow(context)
        await q.message.reply_text(
            f"❌ Out of stock: {_esc(e.title)} (available: {e.available}).",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛍 Browse products", callback_data="browse_0")]]),
        )
        return ConversationHandler.END
    except Exception as e:
        _clear_flow(context)
        await q.message.reply_text(f"⚠️ Could not place the order: {_esc(e)}")
        return ConversationHandler.END

    _clear_flow(context)
    bkash = get_setting("bkash_number", "")
    nagad = get_setting("nagad_number", "")
    methods = []
    if bkash:
        methods.append(f"• bKash: <code>{_esc(bkash)}</code>")
    if nagad:
        methods.append(f"• Nagad: <code>{_esc(nagad)}</code>")
    methods_text = "\n".join(methods) if methods else "• Payment details are shown on the website order page"
    minutes = _remaining_minutes(order)
    text = (
        f"🧾 <b>Order {order['order_code']} created!</b>\n\n"
        f"💰 Total: {fmt_money(order['total_bdt'])}\n\n"
        f"💳 <b>Pay via:</b>\n{methods_text}\n\n"
        f"⏰ Expires in {minutes} minute{'s' if minutes != 1 else ''}.\n"
        "After paying, submit the screenshot with the TrxID as caption."
    )
    keyboard = [[InlineKeyboardButton("💳 Submit payment proof", callback_data=f"proof_{order['order_code']}")]]
    url = _order_url(order["order_code"])
    if url:
        keyboard.append([InlineKeyboardButton("🌐 Open order page", url=url)])
    keyboard.append([InlineKeyboardButton("📦 My orders", callback_data="my_orders")])
    await q.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    return ConversationHandler.END


async def buy_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _clear_flow(context)
    await q.message.reply_text(
        "Purchase cancelled.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛍 Browse products", callback_data="browse_0")]]),
    )
    return ConversationHandler.END


async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _clear_flow(context)
    await update.message.reply_text("Cancelled. Send /browse to start again.")
    return ConversationHandler.END


# ---------------------------------------------------------------- orders

async def cmd_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_orders_list(update.message.chat_id, update.effective_user.id, context.bot)


async def _send_orders_list(chat_id, user_id, bot):
    orders = orders_svc.get_user_orders(user_id, limit=10)
    if not orders:
        await bot.send_message(
            chat_id=chat_id,
            text="You have no orders yet. Browse the shop to place your first order.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛍 Browse products", callback_data="browse_0")]]),
        )
        return
    keyboard = []
    for o in orders:
        badge = STATUS_BADGES.get(o["status"], o["status"])
        keyboard.append([
            InlineKeyboardButton(f"{o['order_code']} — {fmt_money(o['total_bdt'])} — {badge}", callback_data=f"ord_{o['order_code']}")
        ])
    keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data="my_orders")])
    await bot.send_message(
        chat_id=chat_id,
        text="📦 <b>Your Orders</b>\nSelect an order to see details and delivery status.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def my_orders_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await _send_orders_list(q.message.chat_id, q.from_user.id, context.bot)


async def order_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    m = re.match(r"^ord_([A-Z]+-\d+)$", q.data)
    if not m:
        return
    order = orders_svc.get_order_with_items(m.group(1))
    if order is None or order["user_id"] != q.from_user.id:
        await q.message.reply_text("Order not found.")
        return
    await _send_order_detail(context.bot, q.from_user.id, order)


async def _send_order_detail(bot, chat_id, order):
    from app.services.channels import items_summary

    lines = [
        f"📦 <b>Order {order['order_code']}</b>",
        "",
        items_summary(order["items"]),
        f"💰 Total: {fmt_money(order['total_bdt'])}",
        f"Status: {STATUS_BADGES.get(order['status'], order['status'])}",
    ]
    buttons = []
    if order["status"] == "pending_payment":
        minutes = _remaining_minutes(order)
        if minutes > 0:
            lines.append(f"⏰ {minutes} minute{'s' if minutes != 1 else ''} left to pay — then submit your proof.")
        else:
            lines.append("⏰ Payment window closed — this order will be cancelled automatically.")
        buttons.append([InlineKeyboardButton("💳 Submit payment proof", callback_data=f"proof_{order['order_code']}")])
        buttons.append([InlineKeyboardButton("🔄 Refresh status", callback_data=f"ord_{order['order_code']}")])
    elif order["status"] == "awaiting_proof":
        lines.append("📨 Proof submitted — an admin is reviewing it. You'll be notified here.")
        buttons.append([InlineKeyboardButton("🔄 Refresh status", callback_data=f"ord_{order['order_code']}")])
    elif order["status"] == "delivered":
        inv = inventory_svc.get_assigned_inventory(order["id"])
        if inv:
            lines.append("")
            lines.append("🔑 <b>Credentials</b>")
            for i, it in enumerate(inv, 1):
                lines.append(f"\nAccount {i}:")
                lines.append(f"Username: <code>{_esc(it['account_username'])}</code>")
                lines.append(f"Password: <code>{_esc(it['account_password'])}</code>")
                if it.get("cookie_data"):
                    lines.append(f"Cookie: <code>{_esc(it['cookie_data'])}</code>")
        buttons.append([InlineKeyboardButton("🔄 Refresh status", callback_data=f"ord_{order['order_code']}")])
    elif order["status"] in ("cancelled", "rejected"):
        reason = order.get("cancel_reason")
        if reason == "expired":
            lines.append("This order expired because payment was not completed in time.")
        elif reason == "out_of_stock":
            lines.append("The item went out of stock before verification. Contact support with your TrxID if you paid.")
        else:
            lines.append("This order is closed. Contact support if you need help.")
        buttons.append([InlineKeyboardButton("🛍 Browse products", callback_data="browse_0")])

    url = _order_url(order["order_code"])
    if url:
        buttons.append([InlineKeyboardButton("🌐 Open on website", url=url)])
    buttons.append([InlineKeyboardButton("⬅️ My orders", callback_data="my_orders")])
    await bot.send_message(
        chat_id=chat_id,
        text="\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ---------------------------------------------------------------- proof flow

async def proof_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if context.args:
        code = context.args[0].strip().upper()
        order = orders_svc.get_order_by_code(code)
        if order is None or order["user_id"] != user_id:
            await update.message.reply_text("Order not found.")
            return ConversationHandler.END
        if order["status"] not in ("pending_payment", "awaiting_proof"):
            await update.message.reply_text(
                f"Order {code} is {STATUS_BADGES.get(order['status'], order['status'])} — no proof needed."
            )
            return ConversationHandler.END
        _clear_flow(context)
        context.user_data["proof_order_id"] = order["id"]
        context.user_data["proof_order_code"] = order["order_code"]
        return await _ask_proof_method(update, context, via_message=True)
    await _list_payable_orders(update.message.chat_id, user_id, context.bot)
    return ConversationHandler.END


async def proof_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await _list_payable_orders(q.message.chat_id, q.from_user.id, context.bot)
    return ConversationHandler.END


async def _list_payable_orders(chat_id, user_id, bot):
    orders = [
        o for o in orders_svc.get_user_orders(user_id, limit=20)
        if o["status"] in ("pending_payment", "awaiting_proof")
    ]
    if not orders:
        await bot.send_message(
            chat_id=chat_id,
            text="No orders are waiting for payment. Place an order first.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛍 Browse products", callback_data="browse_0")]]),
        )
        return
    keyboard = [
        [InlineKeyboardButton(f"{o['order_code']} — {fmt_money(o['total_bdt'])}", callback_data=f"proof_{o['order_code']}")]
        for o in orders
    ]
    await bot.send_message(
        chat_id=chat_id,
        text="💳 Select an order to submit payment proof:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def proof_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    m = re.match(r"^proof_([A-Z]+-\d+)$", q.data)
    if not m:
        return ConversationHandler.END
    code = m.group(1)
    order = orders_svc.get_order_by_code(code)
    if order is None or order["user_id"] != q.from_user.id:
        await q.message.reply_text("Order not found.")
        return ConversationHandler.END
    if order["status"] not in ("pending_payment", "awaiting_proof"):
        await q.message.reply_text(
            f"Order {code} is {STATUS_BADGES.get(order['status'], order['status'])} — no proof needed."
        )
        return ConversationHandler.END
    _clear_flow(context)
    context.user_data["proof_order_id"] = order["id"]
    context.user_data["proof_order_code"] = order["order_code"]
    return await _ask_proof_method(update, context, via_message=False)


async def _ask_proof_method(update, context, via_message):
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("bKash", callback_data="method_bKash"),
                InlineKeyboardButton("Nagad", callback_data="method_Nagad"),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_proof")],
        ]
    )
    text = (
        "🏦 Select payment method, then send the payment screenshot photo with the "
        "<b>TrxID as caption</b> (optional: sender number after a space)."
    )
    if via_message:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await update.callback_query.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)
    return PROOF_METHOD


async def proof_method_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    m = re.match(r"^method_(bKash|Nagad)$", q.data)
    if not m:
        return PROOF_METHOD
    context.user_data["proof_method"] = m.group(1)
    await q.message.reply_text(
        f"📸 Now send the payment screenshot for <b>{m.group(1)}</b>.\n"
        "Caption format: <code>&lt;TrxID&gt; [sender_number]</code>\n\n"
        "Tip: send the photo as a file/photo, not as a document.",
        parse_mode="HTML",
    )
    return PROOF_PHOTO


async def proof_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    order_id = context.user_data.get("proof_order_id")
    order_code = context.user_data.get("proof_order_code")
    if not order_id:
        await msg.reply_text("This proof session expired. Send /proof to start again.")
        return ConversationHandler.END
    if not msg.photo:
        await msg.reply_text("Please send the payment screenshot as a photo, with the TrxID as caption.")
        return PROOF_PHOTO
    method = context.user_data.get("proof_method", "bKash")
    caption = (msg.caption or "").strip()
    parts = caption.split()
    if not parts:
        await msg.reply_text(
            "Caption is required. Send the photo again with the TrxID as caption "
            "(e.g. <code>ABC123 01700000000</code>).",
            parse_mode="HTML",
        )
        return PROOF_PHOTO
    trxid = parts[0]
    sender = parts[1] if len(parts) > 1 else None
    result = await payments_svc.submit_proof(
        order_id, msg.from_user.id, method, trxid, sender_number=sender,
        photo_file_id=msg.photo[-1].file_id, bot=context.bot,
    )
    _clear_flow(context)
    if not result.get("ok"):
        error = result.get("error", "unknown error")
        messages = {
            "order_not_found": "Order not found.",
            "invalid_status": "This order can no longer receive a proof.",
            "proof_already_reviewed": "Your payment proof was already reviewed. Contact support if you need help.",
            "trxid_required": "TrxID is required.",
            "trxid_in_use": "This TrxID has already been used for another order. Double-check the number or contact support.",
        }
        await msg.reply_text(f"⚠️ {messages.get(error, 'Could not submit proof: ' + str(error))}")
        return ConversationHandler.END
    keyboard = []
    if order_code:
        keyboard.append([InlineKeyboardButton("🔄 Check status", callback_data=f"ord_{order_code}")])
        url = _order_url(order_code)
        if url:
            keyboard.append([InlineKeyboardButton("🌐 Open order page", url=url)])
    keyboard.append([InlineKeyboardButton("📦 My orders", callback_data="my_orders")])
    await msg.reply_text(
        "✅ <b>Payment proof submitted!</b>\n\n"
        f"TrxID: <code>{_esc(trxid)}</code>\n"
        "An admin will verify it shortly. You'll get the credentials here as soon as it's accepted.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ConversationHandler.END


async def proof_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _clear_flow(context)
    await q.message.reply_text("Payment proof cancelled.")
    return ConversationHandler.END


async def _reply(update, text, via_command, reply_markup=None, parse_mode=None):
    if via_command:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    else:
        await update.callback_query.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)


def register(app):
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("browse", cmd_browse))
    app.add_handler(CommandHandler("orders", cmd_orders))

    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu$"))
    app.add_handler(CallbackQueryHandler(help_callback, pattern=r"^help$"))
    app.add_handler(CallbackQueryHandler(noop_callback, pattern=r"^noop$"))
    app.add_handler(CallbackQueryHandler(browse_callback, pattern=r"^browse(?:_\d+)?$"))
    app.add_handler(CallbackQueryHandler(product_callback, pattern=r"^prod_\d+(?:_\d+)?$"))
    app.add_handler(CallbackQueryHandler(my_orders_callback, pattern=r"^my_orders$"))
    app.add_handler(CallbackQueryHandler(order_detail_callback, pattern=r"^ord_([A-Z]+-\d+)$"))

    app.add_handler(
        ConversationHandler(
            entry_points=[
                CommandHandler("buy", buy_command),
                CallbackQueryHandler(buy_start, pattern=r"^buy_\d+(?:_\d+)?$"),
            ],
            states={
                BUY_QTY: [
                    MessageHandler(filters.Regex(r"^\d+$"), buy_qty),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, buy_qty_invalid),
                    CallbackQueryHandler(buy_cancel, pattern=r"^cancel_buy$"),
                ],
                BUY_CONFIRM: [
                    CallbackQueryHandler(buy_confirm, pattern=r"^conf_\d+_\d+$"),
                    CallbackQueryHandler(buy_cancel, pattern=r"^cancel_buy$"),
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel_conv)],
        )
    )

    app.add_handler(
        ConversationHandler(
            entry_points=[
                CommandHandler("proof", proof_command),
                CallbackQueryHandler(proof_start_callback, pattern=r"^proof_start$"),
                CallbackQueryHandler(proof_select_callback, pattern=r"^proof_([A-Z]+-\d+)$"),
            ],
            states={
                PROOF_METHOD: [
                    CallbackQueryHandler(proof_method_callback, pattern=r"^method_(bKash|Nagad)$"),
                    CallbackQueryHandler(proof_cancel, pattern=r"^cancel_proof$"),
                ],
                PROOF_PHOTO: [
                    MessageHandler(filters.PHOTO, proof_photo),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, proof_photo_invalid),
                    CallbackQueryHandler(proof_cancel, pattern=r"^cancel_proof$"),
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel_conv)],
        )
    )

    # Global fallbacks for stale buttons and /cancel outside a conversation.
    app.add_handler(CommandHandler("cancel", cancel_conv))
    app.add_handler(CallbackQueryHandler(buy_cancel, pattern=r"^cancel_buy$"))
    app.add_handler(CallbackQueryHandler(proof_cancel, pattern=r"^cancel_proof$"))


async def proof_photo_invalid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Please send the payment screenshot as a photo, with the TrxID as caption.")
    return PROOF_PHOTO
