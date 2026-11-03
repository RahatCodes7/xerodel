import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from app.database import get_setting, get_user, upsert_user
from app.services import inventory as inventory_svc, orders as orders_svc, payments as payments_svc
from app.services.channels import STATUS_BADGES, fmt_money

BUY_QTY, BUY_CONFIRM = range(2)
PROOF_METHOD, PROOF_PHOTO = range(2)


def _products_keyboard():
    from app.database import get_connection

    conn = get_connection()
    rows = conn.execute(
        """SELECT p.*, (SELECT COUNT(*) FROM inventory i WHERE i.product_id = p.id AND i.status = 'available') AS stock
           FROM products p WHERE p.is_active = 1 ORDER BY p.id"""
    ).fetchall()
    conn.close()
    keyboard = []
    for p in rows:
        stock = p["stock"] if "stock" in p.keys() else 0
        keyboard.append(
            [InlineKeyboardButton(f"{p['title']} — {fmt_money(p['price_bdt'])} (stock: {stock})", callback_data=f"prod_{p['id']}")]
        )
    return keyboard, [dict(r) for r in rows]


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.full_name, u.username, None)
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🛍 Browse Products", callback_data="browse")],
            [InlineKeyboardButton("📦 My Orders", callback_data="my_orders")],
            [InlineKeyboardButton("💳 Submit Payment Proof", callback_data="proof_start")],
        ]
    )
    await update.message.reply_text(
        f"👋 Welcome, {u.first_name}!\n\nBuy Telegram accounts, Crunchyroll accounts and cookies. "
        "Pay via bKash/Nagad and get credentials instantly after verification.",
        reply_markup=keyboard,
    )


async def cmd_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_orders_list(update.message.chat_id, update.effective_user.id)


async def _send_orders_list(chat_id, user_id):
    orders = orders_svc.get_user_orders(user_id, limit=10)
    if not orders:
        text = "You have no orders yet. Browse products to place an order."
    else:
        lines = ["📦 <b>Your Orders</b>", ""]
        for o in orders:
            badge = STATUS_BADGES.get(o["status"], o["status"])
            lines.append(f"• <code>{o['order_code']}</code> — {fmt_money(o['total_bdt'])} — {badge}")
        text = "\n".join(lines)
    bot = context.bot if context else None
    if bot:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")


async def browse_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    keyboard, _ = _products_keyboard()
    if not keyboard:
        await q.message.reply_text("No products available right now.")
        return
    await q.message.reply_text(
        "🛍 <b>Products</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
    )


async def product_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    product_id = int(re.match(r"^prod_(\d+)$", q.data).group(1))
    from app.database import get_connection

    conn = get_connection()
    p = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    stock = conn.execute(
        "SELECT COUNT(*) FROM inventory WHERE product_id = ? AND status = 'available'", (product_id,)
    ).fetchone()[0]
    conn.close()
    if p is None or not p["is_active"]:
        await q.message.reply_text("Product not available.")
        return
    text = (
        f"📦 <b>{p['title']}</b>\n\n"
        f"💰 Price: {fmt_money(p['price_bdt'])}\n"
        f"📊 In stock: {stock}\n\n"
        f"{p['description'] or ''}"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🛒 Buy", callback_data=f"buy_{p['id']}")],
            [InlineKeyboardButton("⬅️ Back", callback_data="browse")],
        ]
    )
    await q.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def buy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args and context.args[0].isdigit():
        return await buy_start_by_id(update, context, int(context.args[0]), via_command=True)
    keyboard, _ = _products_keyboard()
    if not keyboard:
        await update.message.reply_text("No products available right now.")
        return ConversationHandler.END
    await update.message.reply_text(
        "🛍 <b>Products</b>\n\nSend /buy <product_id> or pick from the menu:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ConversationHandler.END


async def buy_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    product_id = int(re.match(r"^buy_(\d+)$", q.data).group(1))
    return await buy_start_by_id(update, context, product_id, via_command=False)


async def buy_start_by_id(update, context, product_id, via_command):
    from app.database import get_connection

    conn = get_connection()
    p = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    stock = conn.execute(
        "SELECT COUNT(*) FROM inventory WHERE product_id = ? AND status = 'available'", (product_id,)
    ).fetchone()[0]
    conn.close()
    if p is None or not p["is_active"]:
        await _reply(update, "Product not available.", via_command)
        return ConversationHandler.END
    if stock <= 0:
        await _reply(update, "❌ Out of stock.", via_command)
        return ConversationHandler.END
    context.user_data["buy_product_id"] = product_id
    context.user_data["buy_stock"] = stock
    await _reply(
        update,
        f"📦 {p['title']} — {fmt_money(p['price_bdt'])} each\nStock: {stock}\n\nSend quantity (1-{min(stock, orders_svc.MAX_QTY_PER_ITEM)}):",
        via_command,
    )
    return BUY_QTY


async def buy_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    product_id = context.user_data.get("buy_product_id")
    stock = context.user_data.get("buy_stock", 0)
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("Please send a number (quantity).")
        return BUY_QTY
    qty = int(text)
    if qty < 1 or qty > min(stock, orders_svc.MAX_QTY_PER_ITEM):
        await update.message.reply_text(f"Quantity must be between 1 and {min(stock, orders_svc.MAX_QTY_PER_ITEM)}.")
        return BUY_QTY
    from app.database import get_connection

    conn = get_connection()
    p = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    conn.close()
    context.user_data["buy_qty"] = qty
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Confirm", callback_data=f"conf_{product_id}_{qty}"),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel_buy"),
            ]
        ]
    )
    await update.message.reply_text(
        f"🧾 <b>{p['title']}</b> × {qty}\n💰 Total: {fmt_money(p['price_bdt'] * qty)}\n\nConfirm?",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    return BUY_CONFIRM


async def buy_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    m = re.match(r"^conf_(\d+)_(\d+)$", q.data)
    product_id, qty = int(m.group(1)), int(m.group(2))
    user_id = q.from_user.id
    try:
        order = await orders_svc.place_order(user_id, [{"product_id": product_id, "qty": qty}], bot=context.bot)
    except orders_svc.OutOfStockError as e:
        await q.message.reply_text(f"❌ Out of stock: {e.title} (available: {e.available}).")
        return ConversationHandler.END
    except ValueError as e:
        await q.message.reply_text(f"⚠️ {e}")
        return ConversationHandler.END

    bkash = get_setting("bkash_number", "")
    nagad = get_setting("nagad_number", "")
    methods = []
    if bkash:
        methods.append(f"• bKash: <code>{bkash}</code>")
    if nagad:
        methods.append(f"• Nagad: <code>{nagad}</code>")
    methods_text = "\n".join(methods) if methods else "• See website for payment details"
    text = (
        f"🧾 <b>Order {order['order_code']} created!</b>\n\n"
        f"💰 Total: {fmt_money(order['total_bdt'])}\n\n"
        f"💳 <b>Pay via:</b>\n{methods_text}\n\n"
        f"⏰ Expires in {_remaining_minutes(order)} minutes.\n"
        f"After paying, send the payment screenshot with the TrxID as caption:\n"
        f"<code>/proof {order['order_code']}</code>"
    )
    await q.message.reply_text(text, parse_mode="HTML")
    return ConversationHandler.END


async def buy_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text("Purchase cancelled.")
    return ConversationHandler.END


async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


async def my_orders_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    orders = orders_svc.get_user_orders(q.from_user.id, limit=10)
    if not orders:
        await q.message.reply_text("You have no orders yet.")
        return
    keyboard = []
    for o in orders:
        badge = STATUS_BADGES.get(o["status"], o["status"])
        keyboard.append([InlineKeyboardButton(f"{o['order_code']} — {badge}", callback_data=f"ord_{o['order_code']}")])
    await q.message.reply_text(
        "📦 <b>Your Orders</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
    )


async def order_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    code = re.match(r"^ord_([A-Z]+-\d+)$", q.data).group(1)
    order = orders_svc.get_order_with_items(code)
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
    if order["status"] == "pending_payment":
        lines.append("⏰ Pay within 30 minutes, then submit your proof with /proof " + order["order_code"])
    if order["status"] == "awaiting_proof":
        lines.append("📨 Proof submitted — waiting for admin verification.")
    if order["status"] == "delivered":
        inv = inventory_svc.get_assigned_inventory(order["id"])
        if inv:
            lines.append("")
            lines.append("🔑 <b>Credentials</b>")
            for i, it in enumerate(inv, 1):
                lines.append(f"\nAccount {i}:")
                lines.append(f"Username: <code>{it['account_username'] or ''}</code>")
                lines.append(f"Password: <code>{it['account_password'] or ''}</code>")
                if it.get("cookie_data"):
                    lines.append(f"Cookie: <code>{it['cookie_data']}</code>")
    await bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="HTML")


async def proof_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if context.args:
        code = context.args[0].strip().upper()
        order = orders_svc.get_order_by_code(code)
        if order is None or order["user_id"] != user_id:
            await update.message.reply_text("Order not found.")
            return ConversationHandler.END
        if order["status"] not in ("pending_payment", "awaiting_proof"):
            await update.message.reply_text(f"Order {code} is {STATUS_BADGES.get(order['status'], order['status'])} — no proof needed.")
            return ConversationHandler.END
        context.user_data["proof_order_id"] = order["id"]
        return await _ask_proof_method(update, context, via_message=True)
    await _list_payable_orders(update.message.chat_id, user_id, context)
    return ConversationHandler.END


async def proof_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await _list_payable_orders(q.message.chat_id, q.from_user.id, context)
    return ConversationHandler.END


async def _list_payable_orders(chat_id, user_id, context):
    orders = [o for o in orders_svc.get_user_orders(user_id, limit=20) if o["status"] in ("pending_payment", "awaiting_proof")]
    if not orders:
        if context and context.bot:
            await context.bot.send_message(chat_id=chat_id, text="No orders awaiting payment. Place an order first.")
        return
    keyboard = []
    for o in orders:
        keyboard.append([InlineKeyboardButton(f"{o['order_code']} — {fmt_money(o['total_bdt'])}", callback_data=f"proof_{o['order_code']}")])
    if context and context.bot:
        await context.bot.send_message(
            chat_id=chat_id, text="💳 Select an order to submit payment proof:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


async def proof_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    code = re.match(r"^proof_([A-Z]+-\d+)$", q.data).group(1)
    order = orders_svc.get_order_by_code(code)
    if order is None or order["user_id"] != q.from_user.id:
        await q.message.reply_text("Order not found.")
        return ConversationHandler.END
    if order["status"] not in ("pending_payment", "awaiting_proof"):
        await q.message.reply_text(f"Order {code} is {STATUS_BADGES.get(order['status'], order['status'])} — no proof needed.")
        return ConversationHandler.END
    context.user_data["proof_order_id"] = order["id"]
    return await _ask_proof_method(update, context, via_message=False)


async def _ask_proof_method(update, context, via_message):
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("bKash", callback_data="method_bKash"), InlineKeyboardButton("Nagad", callback_data="method_Nagad")],
        ]
    )
    text = "🏦 Select payment method, then send the payment screenshot photo with the <b>TrxID as caption</b> (optional: sender number after a space)."
    if via_message:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await update.callback_query.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)
    return PROOF_METHOD


async def proof_method_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    method = re.match(r"^method_(bKash|Nagad)$", q.data).group(1)
    context.user_data["proof_method"] = method
    await q.message.reply_text(
        f"📸 Now send the payment screenshot photo for method <b>{method}</b>.\n"
        "Caption format: <code>&lt;TrxID&gt; [sender_number]</code>",
        parse_mode="HTML",
    )
    return PROOF_PHOTO


async def proof_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg.photo:
        await msg.reply_text("Please send a photo of the payment screenshot.")
        return PROOF_PHOTO
    order_id = context.user_data.get("proof_order_id")
    method = context.user_data.get("proof_method", "bKash")
    caption = (msg.caption or "").strip()
    parts = caption.split()
    if not parts:
        await msg.reply_text("Caption is required. Send the photo again with the TrxID as caption (e.g. <code>ABC123 01700000000</code>).", parse_mode="HTML")
        return PROOF_PHOTO
    trxid = parts[0]
    sender = parts[1] if len(parts) > 1 else None
    result = await payments_svc.submit_proof(
        order_id, msg.from_user.id, method, trxid, sender_number=sender,
        photo_file_id=msg.photo[-1].file_id, bot=context.bot,
    )
    if result.get("ok"):
        await msg.reply_text(
            f"✅ Proof submitted for order verification. You'll be notified once an admin reviews it.\n"
            f"TrxID: <code>{trxid}</code>",
            parse_mode="HTML",
        )
    else:
        await msg.reply_text(f"⚠️ Could not submit proof: {result.get('error', 'unknown error')}")
    return ConversationHandler.END


def _remaining_minutes(order):
    try:
        from datetime import datetime, timezone

        exp = datetime.fromisoformat(order["expires_at"])
        remaining = (exp - datetime.now(timezone.utc)).total_seconds() / 60
        return max(0, int(remaining))
    except Exception:
        return 30


async def _reply(update, text, via_command):
    if via_command:
        await update.message.reply_text(text)
    else:
        await update.callback_query.message.reply_text(text)


def register(app):
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("orders", cmd_orders))
    app.add_handler(CallbackQueryHandler(browse_callback, pattern=r"^browse$"))
    app.add_handler(CallbackQueryHandler(product_callback, pattern=r"^prod_(\d+)$"))
    app.add_handler(CallbackQueryHandler(my_orders_callback, pattern=r"^my_orders$"))
    app.add_handler(CallbackQueryHandler(order_detail_callback, pattern=r"^ord_([A-Z]+-\d+)$"))

    app.add_handler(
        ConversationHandler(
            entry_points=[
                CommandHandler("buy", buy_command),
                CallbackQueryHandler(buy_start, pattern=r"^buy_(\d+)$"),
            ],
            states={
                BUY_QTY: [
                    MessageHandler(filters.Regex(r"^\d+$"), buy_qty),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: u.message.reply_text("Please send a number (quantity).") or BUY_QTY),
                ],
                BUY_CONFIRM: [
                    CallbackQueryHandler(buy_confirm, pattern=r"^conf_(\d+)_(\d+)$"),
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
                PROOF_METHOD: [CallbackQueryHandler(proof_method_callback, pattern=r"^method_(bKash|Nagad)$")],
                PROOF_PHOTO: [
                    MessageHandler(filters.PHOTO, proof_photo),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: u.message.reply_text("Please send the payment screenshot photo with the TrxID as caption.") or PROOF_PHOTO),
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel_conv)],
        )
    )
