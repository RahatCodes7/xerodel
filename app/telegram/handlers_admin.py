import os
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

from app.database import (
    get_admin_ids,
    get_channel_ids,
    get_connection,
    get_setting,
    get_user,
    log_admin_action,
    set_setting,
    upsert_user,
)
from app.services import inventory as inventory_svc, orders as orders_svc, payments as payments_svc, products as products_svc
from app.services.channels import STATUS_BADGES, buyer_label, fmt_money

ADD_PLATFORM, ADD_TITLE, ADD_PRICE = range(3)
INV_SELECT, INV_PASTE = range(2)


def is_admin(tg_id):
    return tg_id in get_admin_ids()


def _require_admin(update):
    return is_admin(update.effective_user.id)


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.full_name, u.username, (u.photo_url if u.photo else None))
    if not _require_admin(update):
        await update.message.reply_text("⛔ This command is for admins only.")
        return
    await _admin_menu(update.message.chat_id, context)


async def _admin_menu(chat_id, context):
    pending = len(payments_svc.list_proofs(status="pending"))
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"💳 Pending Payments ({pending})", callback_data="adm_pending")],
            [InlineKeyboardButton("➕ Add Product", callback_data="adm_add_product")],
            [InlineKeyboardButton("📦 Add Inventory", callback_data="adm_add_inv")],
            [InlineKeyboardButton("📊 Stats", callback_data="adm_stats")],
            [InlineKeyboardButton("⚙️ Settings", callback_data="adm_settings")],
        ]
    )
    await context.bot.send_message(chat_id=chat_id, text="🔧 <b>Admin Menu</b>", parse_mode="HTML", reply_markup=keyboard)


async def adm_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.message.reply_text("⛔ Admins only.")
        return
    await _admin_menu(q.message.chat_id, context)


async def adm_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.message.reply_text("⛔ Admins only.")
        return
    proofs = payments_svc.list_proofs(status="pending")
    if not proofs:
        await q.message.reply_text("✅ No pending payments.")
        return
    for proof in proofs:
        order = orders_svc.get_order_by_id(proof["order_id"])
        if order is None:
            continue
        buyer = get_user(order["user_id"])
        items = orders_svc.get_order_items(order["id"])
        lines = [
            f"💳 <b>Order {order['order_code']}</b>",
            f"👤 Buyer: {buyer_label(buyer)}",
            f"💰 Amount: {fmt_money(order['total_bdt'])} — {proof['method']}",
            f"🔢 TrxID: {proof['transaction_id']}",
            f"📱 Sender: {proof.get('sender_number') or 'n/a'}",
            "",
            "Items:",
        ]
        for it in items:
            lines.append(f"• {it['product_title']} × {it['quantity']}")
        text = "\n".join(lines)
        keyboard = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Accept", callback_data=f"adm_acc_{order['id']}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"adm_rej_{order['id']}"),
            ]]
        )
        sent = False
        if proof.get("photo_file_id"):
            try:
                await context.bot.send_photo(chat_id=q.message.chat_id, photo=proof["photo_file_id"], caption=text, reply_markup=keyboard, parse_mode="HTML")
                sent = True
            except Exception:
                sent = False
        if not sent and proof.get("photo_path") and os.path.exists(proof["photo_path"]):
            try:
                with open(proof["photo_path"], "rb") as f:
                    await context.bot.send_photo(chat_id=q.message.chat_id, photo=f, caption=text, reply_markup=keyboard, parse_mode="HTML")
                sent = True
            except Exception:
                sent = False
        if not sent:
            await context.bot.send_message(chat_id=q.message.chat_id, text=text + "\n\n(photo unavailable)", reply_markup=keyboard, parse_mode="HTML")


async def adm_decide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("⛔ Admins only", show_alert=True)
        return
    m = re.match(r"^adm_(acc|rej)_(\d+)$", q.data)
    decision = "accepted" if m.group(1) == "acc" else "rejected"
    order_id = int(m.group(2))
    await q.answer("Processing...")
    result = await payments_svc.decide(order_id, decision, q.from_user.id, bot=context.bot)
    if result.get("reason") == "already_processed":
        await q.message.reply_text("⚠️ This proof was already processed by another admin.")
    elif result.get("ok"):
        await q.message.reply_text(f"✅ Order {result.get('order_code')}: proof {decision}.")
    else:
        await q.message.reply_text(f"⚠️ Failed: {result.get('error', 'unknown error')}")


async def _stats_text():
    s = orders_svc.stats_summary()
    inv = inventory_svc.inventory_stats()
    return (
        "📊 <b>Stats</b>\n\n"
        f"Total orders: {s['orders_total']}\n"
        f"Pending payment: {s['pending_payment']}\n"
        f"Awaiting proof: {s['awaiting_proof']}\n"
        f"Delivered: {s['delivered']}\n"
        f"Revenue: {fmt_money(s['revenue'])}\n"
        f"Pending proofs: {s['proofs_pending']}\n"
        f"Users: {s['users_count']}\n\n"
        f"Inventory — available: {inv['available']}, assigned: {inv['assigned']}, sold: {inv['sold']}"
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _require_admin(update):
        await update.message.reply_text("⛔ Admins only.")
        return
    await update.message.reply_text(await _stats_text(), parse_mode="HTML")


async def adm_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.message.reply_text("⛔ Admins only.")
        return
    await q.message.reply_text(await _stats_text(), parse_mode="HTML")


def _settings_text():
    public_id, private_id = get_channel_ids()
    return (
        "⚙️ <b>Settings</b>\n\n"
        f"Site name: {get_setting('site_name', '')}\n"
        f"bKash: {get_setting('bkash_number', '-') or '-'}\n"
        f"Nagad: {get_setting('nagad_number', '-') or '-'}\n"
        f"Public channel: {public_id or '-'}\n"
        f"Private channel: {private_id or '-'}\n"
        f"Admin IDs: {get_setting('admin_ids', '-') or '-'}\n"
        f"Expiry minutes: {get_setting('expiry_minutes', '30')}\n\n"
        f"Instructions: {get_setting('payment_instructions', '-') or '-'}\n\n"
        "Edit via /set_bkash, /set_nagad, /set_channels, /set_admins, /set_expiry, /set_instructions or the website admin panel."
    )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _require_admin(update):
        await update.message.reply_text("⛔ Admins only.")
        return
    await update.message.reply_text(_settings_text(), parse_mode="HTML")


async def adm_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.message.reply_text("⛔ Admins only.")
        return
    await q.message.reply_text(_settings_text(), parse_mode="HTML")


async def _set_simple(update, context, key, label, value, admin_id):
    set_setting(key, value)
    log_admin_action(admin_id, "settings_change", "settings", key, f"{label} -> {value}")
    await update.message.reply_text(f"✅ {label} updated: {value}")


async def cmd_set_bkash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _require_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /set_bkash <number>")
        return
    await _set_simple(update, context, "bkash_number", "bKash number", context.args[0], update.effective_user.id)


async def cmd_set_nagad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _require_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /set_nagad <number>")
        return
    await _set_simple(update, context, "nagad_number", "Nagad number", context.args[0], update.effective_user.id)


async def cmd_set_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _require_admin(update):
        return
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /set_channels <public_channel_id> <private_channel_id>")
        return
    set_setting("public_channel_id", context.args[0])
    set_setting("private_channel_id", context.args[1])
    log_admin_action(update.effective_user.id, "settings_change", "settings", "channels", f"public={context.args[0]} private={context.args[1]}")
    await update.message.reply_text(f"✅ Channels updated.\nPublic: {context.args[0]}\nPrivate: {context.args[1]}")


async def cmd_set_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _require_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /set_admins <id1,id2,...>")
        return
    raw = context.args[0]
    ids = [p.strip() for p in raw.replace(",", " ").split() if p.strip().isdigit()]
    set_setting("admin_ids", ",".join(ids))
    conn = get_connection()
    conn.execute("UPDATE users SET is_admin = 0")
    for i in ids:
        conn.execute("UPDATE users SET is_admin = 1 WHERE telegram_id = ?", (int(i),))
    conn.commit()
    conn.close()
    log_admin_action(update.effective_user.id, "settings_change", "settings", "admin_ids", raw)
    await update.message.reply_text(f"✅ Admin IDs updated: {raw}")


async def cmd_set_expiry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _require_admin(update):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /set_expiry <minutes>")
        return
    await _set_simple(update, context, "expiry_minutes", "Expiry minutes", context.args[0], update.effective_user.id)


async def cmd_set_instructions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _require_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /set_instructions <text>")
        return
    await _set_simple(update, context, "payment_instructions", "Payment instructions", " ".join(context.args), update.effective_user.id)


async def adm_add_product_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.message.reply_text("⛔ Admins only.")
        return ConversationHandler.END
    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Telegram", callback_data="plat_telegram"),
            InlineKeyboardButton("Crunchyroll", callback_data="plat_crunchyroll"),
            InlineKeyboardButton("Cookie", callback_data="plat_cookie"),
        ]]
    )
    await q.message.reply_text("➕ <b>Add Product</b>\n\nSelect platform:", parse_mode="HTML", reply_markup=keyboard)
    return ADD_PLATFORM


async def add_product_platform(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    platform = re.match(r"^plat_(telegram|crunchyroll|cookie)$", q.data).group(1)
    context.user_data["add_platform"] = platform
    await q.message.reply_text(f"Platform: <b>{platform}</b>\n\nSend the product title:", parse_mode="HTML")
    return ADD_TITLE


async def add_product_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["add_title"] = update.message.text.strip()
    await update.message.reply_text("Title saved. Send the price in BDT (number):")
    return ADD_PRICE


async def add_product_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", "")
    try:
        price = float(text)
    except ValueError:
        await update.message.reply_text("Invalid price. Send a number (e.g. 350):")
        return ADD_PRICE
    product = products_svc.add_product(
        context.user_data["add_platform"], context.user_data["add_title"], "", price, admin_id=update.effective_user.id
    )
    await update.message.reply_text(f"✅ Product added: {product['title']} — {fmt_money(price)} (id {product['id']})")
    return ConversationHandler.END


async def adm_add_inv_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.message.reply_text("⛔ Admins only.")
        return ConversationHandler.END
    products = products_svc.list_products(include_inactive=True)
    if not products:
        await q.message.reply_text("No products yet. Add a product first.")
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton(f"{p['title']} (id {p['id']})", callback_data=f"inv_{p['id']}")] for p in products]
    await q.message.reply_text("📦 <b>Add Inventory</b>\n\nSelect product:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    return INV_SELECT


async def add_inv_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    product_id = int(re.match(r"^inv_(\d+)$", q.data).group(1))
    context.user_data["inv_product_id"] = product_id
    await q.message.reply_text(
        "Paste inventory lines, one per line:\n<code>username | password | cookie</code>\n"
        "(cookie is optional for account products)\n\nSend the text now:",
        parse_mode="HTML",
    )
    return INV_PASTE


async def add_inv_paste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    product_id = context.user_data.get("inv_product_id")
    added, errors = inventory_svc.bulk_add(product_id, update.message.text)
    log_admin_action(update.effective_user.id, "inventory_add", "product", product_id, f"Added {added} units via bot")
    msg = f"✅ Added {added} units to product {product_id}."
    if errors:
        msg += f"\n⚠️ {len(errors)} line(s) skipped:\n" + "\n".join(errors[:5])
    await update.message.reply_text(msg)
    return ConversationHandler.END


async def conv_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


def register(app):
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("admin_stats", cmd_stats))
    app.add_handler(CommandHandler("admin_settings", cmd_settings))
    app.add_handler(CommandHandler("set_bkash", cmd_set_bkash))
    app.add_handler(CommandHandler("set_nagad", cmd_set_nagad))
    app.add_handler(CommandHandler("set_channels", cmd_set_channels))
    app.add_handler(CommandHandler("set_admins", cmd_set_admins))
    app.add_handler(CommandHandler("set_expiry", cmd_set_expiry))
    app.add_handler(CommandHandler("set_instructions", cmd_set_instructions))

    app.add_handler(CallbackQueryHandler(adm_menu_callback, pattern=r"^adm_menu$"))
    app.add_handler(CallbackQueryHandler(adm_pending, pattern=r"^adm_pending$"))
    app.add_handler(CallbackQueryHandler(adm_decide, pattern=r"^adm_(acc|rej)_(\d+)$"))
    app.add_handler(CallbackQueryHandler(adm_stats_callback, pattern=r"^adm_stats$"))
    app.add_handler(CallbackQueryHandler(adm_settings_callback, pattern=r"^adm_settings$"))

    app.add_handler(
        ConversationHandler(
            entry_points=[CallbackQueryHandler(adm_add_product_start, pattern=r"^adm_add_product$")],
            states={
                ADD_PLATFORM: [CallbackQueryHandler(add_product_platform, pattern=r"^plat_(telegram|crunchyroll|cookie)$")],
                ADD_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_title)],
                ADD_PRICE: [MessageHandler(filters.Regex(r"^\d+(\.\d+)?$"), add_product_price)],
            },
            fallbacks=[CommandHandler("cancel", conv_cancel)],
        )
    )

    app.add_handler(
        ConversationHandler(
            entry_points=[CallbackQueryHandler(adm_add_inv_start, pattern=r"^adm_add_inv$")],
            states={
                INV_SELECT: [CallbackQueryHandler(add_inv_select, pattern=r"^inv_(\d+)$")],
                INV_PASTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_inv_paste)],
            },
            fallbacks=[CommandHandler("cancel", conv_cancel)],
        )
    )
