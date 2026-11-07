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
from app.services import (
    channels,
    inventory as inventory_svc,
    orders as orders_svc,
    payments as payments_svc,
    products as products_svc,
)
from app.services.channels import STATUS_BADGES, buyer_label, esc, fmt_money

ADD_PLATFORM, ADD_TITLE, ADD_PRICE, ADD_DESC = range(4)
INV_SELECT, INV_PASTE = range(2)

ADMIN_PAGE_SIZE = 10


def is_admin(tg_id):
    return tg_id in get_admin_ids()


def _require_admin(update):
    return is_admin(update.effective_user.id)


def _clear_admin_flow(context):
    for key in ("add_platform", "add_title", "add_price", "inv_product_id"):
        context.user_data.pop(key, None)


# ---------------------------------------------------------------- menu

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.full_name, u.username, None)
    if not _require_admin(update):
        await update.message.reply_text("⛔ This command is for admins only.")
        return
    await _admin_menu(update.message.chat_id, context)


async def _admin_menu(chat_id, context):
    pending = len(payments_svc.list_proofs(status="pending"))
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"💳 Pending Payments ({pending})", callback_data="adm_pending")],
            [
                InlineKeyboardButton("🧾 Recent Orders", callback_data="adm_orders"),
                InlineKeyboardButton("📦 Stock", callback_data="adm_inventory"),
            ],
            [
                InlineKeyboardButton("➕ Add Product", callback_data="adm_add_product"),
                InlineKeyboardButton("📥 Add Inventory", callback_data="adm_add_inv"),
            ],
            [
                InlineKeyboardButton("📊 Stats", callback_data="adm_stats"),
                InlineKeyboardButton("⚙️ Settings", callback_data="adm_settings"),
            ],
            [InlineKeyboardButton("✖️ Close", callback_data="adm_close")],
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


async def adm_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        await q.message.delete()
    except Exception:
        await q.message.reply_text("Admin menu closed.")


# ---------------------------------------------------------------- orders

async def adm_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.message.reply_text("⛔ Admins only.")
        return
    await _send_admin_orders(q.message.chat_id, context)


async def _send_admin_orders(chat_id, context):
    orders = orders_svc.list_orders(limit=ADMIN_PAGE_SIZE)
    if not orders:
        await context.bot.send_message(chat_id=chat_id, text="No orders yet.")
        return
    lines = ["🧾 <b>Recent Orders</b>", ""]
    keyboard = []
    for o in orders:
        buyer = get_user(o["user_id"])
        badge = STATUS_BADGES.get(o["status"], o["status"])
        lines.append(
            f"• <code>{o['order_code']}</code> — {fmt_money(o['total_bdt'])} — {badge} — "
            f"{esc((buyer or {}).get('full_name') or o['user_id'])}"
        )
        keyboard.append([InlineKeyboardButton(f"{o['order_code']} — {badge}", callback_data=f"adm_ord_{o['order_code']}")])
    keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data="adm_orders")])
    keyboard.append([InlineKeyboardButton("⬅️ Admin menu", callback_data="adm_menu")])
    await context.bot.send_message(
        chat_id=chat_id,
        text="\n".join(lines) + "\n\nTip: open any order or use /order &lt;code&gt;.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cmd_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _require_admin(update):
        await update.message.reply_text("⛔ Admins only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /order <order_code>")
        return
    await _send_admin_order(context.bot, update.message.chat_id, context.args[0].strip().upper())


async def adm_order_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.message.reply_text("⛔ Admins only.")
        return
    m = re.match(r"^adm_ord_([A-Z]+-\d+)$", q.data)
    if not m:
        return
    await _send_admin_order(context.bot, q.message.chat_id, m.group(1))


async def _send_admin_order(bot, chat_id, code):
    order = orders_svc.get_order_with_items(code)
    if order is None:
        await bot.send_message(chat_id=chat_id, text="Order not found.")
        return
    buyer = get_user(order["user_id"])
    proof = payments_svc.get_proof_by_order(order["id"])
    lines = [
        f"🧾 <b>Order {order['order_code']}</b>",
        f"Status: {STATUS_BADGES.get(order['status'], order['status'])}",
        f"👤 Buyer: {buyer_label(buyer)}",
        f"💰 Total: {fmt_money(order['total_bdt'])}",
        f"🕒 Placed: {order['created_at']}",
    ]
    if order.get("expires_at") and order["status"] == "pending_payment":
        lines.append(f"⏰ Expires: {order['expires_at']}")
    lines.append("")
    lines.append("Items:")
    for it in order["items"]:
        lines.append(f"• {esc(it.get('product_title'))} × {it['quantity']} — {fmt_money(it['unit_price'] * it['quantity'])}")
    if proof:
        lines += [
            "",
            "💳 Payment proof",
            f"• Method: {esc(proof.get('method'))}",
            f"• TrxID: <code>{esc(proof.get('transaction_id'))}</code>",
            f"• Status: {esc(proof.get('status'))}",
        ]
    if order["status"] == "delivered":
        inv = inventory_svc.get_assigned_inventory(order["id"])
        lines.append(f"\n🔑 Delivered credentials: {len(inv)} account(s)")
    text = "\n".join(lines)

    buttons = []
    if order["status"] == "awaiting_proof" and proof and proof.get("status") == "pending":
        buttons.append([
            InlineKeyboardButton("✅ Accept", callback_data=f"adm_acc_{order['id']}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"adm_rej_{order['id']}"),
        ])
    if order["status"] in ("pending_payment", "awaiting_proof"):
        buttons.append([InlineKeyboardButton("🚫 Cancel order", callback_data=f"adm_cxl_{order['id']}")])
    buttons.append([
        InlineKeyboardButton("🔄 Refresh", callback_data=f"adm_ord_{order['order_code']}"),
        InlineKeyboardButton("⬅️ Orders", callback_data="adm_orders"),
    ])
    await bot.send_message(
        chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons)
    )
    if proof and proof.get("photo_file_id"):
        try:
            await bot.send_photo(
                chat_id=chat_id,
                photo=proof["photo_file_id"],
                caption=f"📸 Payment proof — {order['order_code']}",
            )
        except Exception as e:
            print(f"[admin] proof photo send failed for {order['order_code']}: {e}")


async def adm_cancel_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("⛔ Admins only", show_alert=True)
        return
    await q.answer()
    m = re.match(r"^adm_cxl_(\d+)$", q.data)
    if not m:
        return
    order = orders_svc.get_order_by_id(int(m.group(1)))
    if order is None:
        await q.message.reply_text("Order not found.")
        return
    await q.message.reply_text(
        f"Cancel order {order['order_code']}?\n"
        "The buyer will be notified and any pending proof will be closed.",
        reply_markup=InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Yes, cancel", callback_data=f"adm_cxlok_{order['id']}"),
                InlineKeyboardButton("↩️ No", callback_data=f"adm_ord_{order['order_code']}"),
            ]]
        ),
    )


async def adm_cancel_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("⛔ Admins only", show_alert=True)
        return
    m = re.match(r"^adm_cxlok_(\d+)$", q.data)
    if not m:
        return
    order_id = int(m.group(1))
    await q.answer("Cancelling...")
    order = orders_svc.get_order_by_id(order_id)
    if order is None:
        await q.message.reply_text("Order not found.")
        return
    if order["status"] not in ("pending_payment", "awaiting_proof"):
        await q.message.reply_text(
            f"Order {order['order_code']} is {STATUS_BADGES.get(order['status'], order['status'])} — cannot cancel."
        )
        return
    orders_svc.mark_cancelled(order_id, reason="admin_cancelled")
    conn = get_connection()
    conn.execute(
        "UPDATE payment_proofs SET status='rejected', note='Order cancelled by admin' "
        "WHERE order_id=? AND status='pending'",
        (order_id,),
    )
    conn.commit()
    conn.close()

    fresh = orders_svc.get_order_by_id(order_id)
    items = orders_svc.get_order_items(order_id)
    buyer = get_user(order["user_id"])
    await channels.edit_public_order(context.bot, fresh, items, buyer)
    if context.bot is not None:
        try:
            await context.bot.send_message(
                chat_id=order["user_id"],
                text=f"🚫 Your order {order['order_code']} was cancelled by an admin. Contact support if you have questions.",
            )
        except Exception as e:
            print(f"[admin] cancel DM failed for {order['order_code']}: {e}")
    log_admin_action(
        q.from_user.id, "order_cancelled", "order", order["order_code"], "Cancelled from bot admin menu"
    )
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await q.message.reply_text(f"✅ Order {order['order_code']} cancelled and buyer notified.")


# ---------------------------------------------------------------- payments

async def adm_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.message.reply_text("⛔ Admins only.")
        return
    proofs = payments_svc.list_proofs(status="pending", limit=ADMIN_PAGE_SIZE)
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
            f"💰 Amount: {fmt_money(order['total_bdt'])} — {esc(proof['method'])}",
            f"🔢 TrxID: {esc(proof['transaction_id'])}",
            f"📱 Sender: {esc(proof.get('sender_number')) or 'n/a'}",
            "",
            "Items:",
        ]
        for it in items:
            lines.append(f"• {esc(it['product_title'])} × {it['quantity']}")
        text = "\n".join(lines)
        keyboard = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Accept", callback_data=f"adm_acc_{order['id']}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"adm_rej_{order['id']}"),
            ],
            [InlineKeyboardButton("🔍 Open order", callback_data=f"adm_ord_{order['order_code']}")]]
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
    if len(proofs) >= ADMIN_PAGE_SIZE:
        await q.message.reply_text(f"Showing the latest {ADMIN_PAGE_SIZE} pending proofs. Clear them to see older ones.")


async def adm_decide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("⛔ Admins only", show_alert=True)
        return
    m = re.match(r"^adm_(acc|rej)_(\d+)$", q.data)
    if not m:
        await q.answer("Unknown action", show_alert=True)
        return
    decision = "accepted" if m.group(1) == "acc" else "rejected"
    order_id = int(m.group(2))
    await q.answer("Processing...")
    result = await payments_svc.decide(order_id, decision, q.from_user.id, bot=context.bot)
    if result.get("reason") == "already_processed":
        await q.answer("Already processed by another admin", show_alert=True)
    elif result.get("ok"):
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await q.message.reply_text(f"✅ Order {result.get('order_code')}: proof {decision}.")
    else:
        await q.message.reply_text(f"⚠️ Failed: {result.get('error', 'unknown error')}")


# ---------------------------------------------------------------- stats & settings

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


async def adm_inventory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.message.reply_text("⛔ Admins only.")
        return
    products = products_svc.list_products(include_inactive=True)
    counts = inventory_svc.counts_by_product()
    if not products:
        await q.message.reply_text("No products yet.")
        return
    lines = ["📦 <b>Stock</b>", ""]
    for p in products:
        c = counts.get(p["id"], {})
        state = "active" if p["is_active"] else "disabled"
        lines.append(
            f"• <b>{esc(p['title'])}</b> (id {p['id']}, {state}) — "
            f"available {c.get('available', 0)}, sold {c.get('sold', 0)}"
        )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📥 Add inventory", callback_data="adm_add_inv")],
            [InlineKeyboardButton("⬅️ Admin menu", callback_data="adm_menu")],
        ]
    )
    await q.message.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=keyboard)


def _settings_text():
    public_id, private_id = get_channel_ids()
    return (
        "⚙️ <b>Settings</b>\n\n"
        f"Site name: {esc(get_setting('site_name', ''))}\n"
        f"bKash: {esc(get_setting('bkash_number', '-')) or '-'}\n"
        f"Nagad: {esc(get_setting('nagad_number', '-')) or '-'}\n"
        f"Public channel: {esc(public_id) or '-'}\n"
        f"Private channel: {esc(private_id) or '-'}\n"
        f"Admin IDs: {esc(get_setting('admin_ids', '-')) or '-'}\n"
        f"Expiry minutes: {esc(get_setting('expiry_minutes', '30'))}\n\n"
        f"Instructions: {esc(get_setting('payment_instructions', '-')) or '-'}\n\n"
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
    await update.message.reply_text(f"✅ {label} updated: {esc(value)}", parse_mode="HTML")


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
    ids = list(dict.fromkeys(p.strip() for p in raw.replace(",", " ").split() if p.strip().isdigit()))
    if not ids:
        await update.message.reply_text("No valid Telegram IDs found.")
        return
    set_setting("admin_ids", ",".join(ids))
    conn = get_connection()
    conn.execute("UPDATE users SET is_admin = 0")
    for i in ids:
        conn.execute("UPDATE users SET is_admin = 1 WHERE telegram_id = ?", (int(i),))
    conn.commit()
    conn.close()
    log_admin_action(update.effective_user.id, "settings_change", "settings", "admin_ids", raw)
    await update.message.reply_text(f"✅ Admin IDs updated: {', '.join(ids)}")


async def cmd_set_expiry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _require_admin(update):
        return
    if not context.args or not context.args[0].isdigit() or int(context.args[0]) < 1:
        await update.message.reply_text("Usage: /set_expiry <minutes> (1 or more)")
        return
    await _set_simple(update, context, "expiry_minutes", "Expiry minutes", context.args[0], update.effective_user.id)


async def cmd_set_instructions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _require_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /set_instructions <text>")
        return
    await _set_simple(update, context, "payment_instructions", "Payment instructions", " ".join(context.args), update.effective_user.id)


# ---------------------------------------------------------------- add product

async def adm_add_product_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.message.reply_text("⛔ Admins only.")
        return ConversationHandler.END
    _clear_admin_flow(context)
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
    m = re.match(r"^plat_(telegram|crunchyroll|cookie)$", q.data)
    if not m:
        return ADD_PLATFORM
    context.user_data["add_platform"] = m.group(1)
    await q.message.reply_text(f"Platform: <b>{m.group(1)}</b>\n\nSend the product title:", parse_mode="HTML")
    return ADD_TITLE


async def add_product_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = (update.message.text or "").strip()
    if not title:
        await update.message.reply_text("Title cannot be empty. Send the product title:")
        return ADD_TITLE
    context.user_data["add_title"] = title
    context.user_data["add_platform"] = context.user_data.get("add_platform", "telegram")
    await update.message.reply_text("Price in BDT (e.g. 350):")
    return ADD_PRICE


async def add_product_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip().replace(",", "").replace("৳", "")
    try:
        price = float(text)
        if price < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Invalid price. Send a number (e.g. 350):")
        return ADD_PRICE
    context.user_data["add_price"] = price
    await update.message.reply_text(
        "Send a short description for the product page, or /skip to leave it empty:"
    )
    return ADD_DESC


async def add_product_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    description = "" if text.lower() in ("/skip", "skip", "-") else text
    try:
        product = products_svc.add_product(
            context.user_data.get("add_platform", "telegram"),
            context.user_data.get("add_title", ""),
            description,
            context.user_data.get("add_price", 0),
            admin_id=update.effective_user.id,
        )
    except Exception as e:
        await update.message.reply_text(f"⚠️ Could not add product: {esc(e)}", parse_mode="HTML")
        _clear_admin_flow(context)
        return ConversationHandler.END
    _clear_admin_flow(context)
    await update.message.reply_text(
        f"✅ Product added: <b>{esc(product['title'])}</b> — {fmt_money(product['price_bdt'])} (id {product['id']})\n"
        "Next: add inventory units so buyers can order it.",
        parse_mode="HTML",
    )
    return ConversationHandler.END


# ---------------------------------------------------------------- add inventory

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
    keyboard = []
    for p in products:
        label = f"{p['title']} (id {p['id']}, {inventory_svc.available_count(p['id'])} available)"
        if len(label) > 60:
            label = label[:57] + "…"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"inv_{p['id']}")])
    await q.message.reply_text(
        "📥 <b>Add Inventory</b>\n\nSelect product:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return INV_SELECT


async def add_inv_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    m = re.match(r"^inv_(\d+)$", q.data)
    if not m:
        return INV_SELECT
    product_id = int(m.group(1))
    product = products_svc.get_product(product_id)
    if product is None:
        await q.message.reply_text("Product not found.")
        return ConversationHandler.END
    context.user_data["inv_product_id"] = product_id
    await q.message.reply_text(
        f"📦 <b>{esc(product['title'])}</b> — currently {inventory_svc.available_count(product_id)} available\n\n"
        "Paste inventory lines, one per line:\n"
        "<code>username | password | cookie</code>\n"
        "(cookie is optional for account products)\n\nSend the text now:",
        parse_mode="HTML",
    )
    return INV_PASTE


async def add_inv_paste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    product_id = context.user_data.get("inv_product_id")
    if not product_id or products_svc.get_product(product_id) is None:
        _clear_admin_flow(context)
        await update.message.reply_text("Product no longer exists. Start again.")
        return ConversationHandler.END
    added, errors = inventory_svc.bulk_add(product_id, update.message.text or "")
    if added:
        log_admin_action(
            update.effective_user.id, "inventory_add", "product", product_id, f"Added {added} units via bot"
        )
    msg = f"✅ Added {added} units to product {product_id}. Total available: {inventory_svc.available_count(product_id)}."
    if errors:
        msg += f"\n⚠️ {len(errors)} line(s) skipped:\n" + "\n".join(errors[:5])
    _clear_admin_flow(context)
    await update.message.reply_text(msg)
    return ConversationHandler.END


async def conv_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _clear_admin_flow(context)
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


def register(app):
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("order", cmd_order))
    app.add_handler(CommandHandler("admin_stats", cmd_stats))
    app.add_handler(CommandHandler("admin_settings", cmd_settings))
    app.add_handler(CommandHandler("set_bkash", cmd_set_bkash))
    app.add_handler(CommandHandler("set_nagad", cmd_set_nagad))
    app.add_handler(CommandHandler("set_channels", cmd_set_channels))
    app.add_handler(CommandHandler("set_admins", cmd_set_admins))
    app.add_handler(CommandHandler("set_expiry", cmd_set_expiry))
    app.add_handler(CommandHandler("set_instructions", cmd_set_instructions))

    app.add_handler(CallbackQueryHandler(adm_menu_callback, pattern=r"^adm_menu$"))
    app.add_handler(CallbackQueryHandler(adm_close, pattern=r"^adm_close$"))
    app.add_handler(CallbackQueryHandler(adm_pending, pattern=r"^adm_pending$"))
    app.add_handler(CallbackQueryHandler(adm_orders, pattern=r"^adm_orders$"))
    app.add_handler(CallbackQueryHandler(adm_order_view, pattern=r"^adm_ord_([A-Z]+-\d+)$"))
    app.add_handler(CallbackQueryHandler(adm_cancel_prompt, pattern=r"^adm_cxl_(\d+)$"))
    app.add_handler(CallbackQueryHandler(adm_cancel_confirm, pattern=r"^adm_cxlok_(\d+)$"))
    app.add_handler(CallbackQueryHandler(adm_decide, pattern=r"^adm_(acc|rej)_(\d+)$"))
    app.add_handler(CallbackQueryHandler(adm_stats_callback, pattern=r"^adm_stats$"))
    app.add_handler(CallbackQueryHandler(adm_inventory, pattern=r"^adm_inventory$"))
    app.add_handler(CallbackQueryHandler(adm_settings_callback, pattern=r"^adm_settings$"))

    app.add_handler(
        ConversationHandler(
            entry_points=[CallbackQueryHandler(adm_add_product_start, pattern=r"^adm_add_product$")],
            states={
                ADD_PLATFORM: [CallbackQueryHandler(add_product_platform, pattern=r"^plat_(telegram|crunchyroll|cookie)$")],
                ADD_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_title)],
                ADD_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_price)],
                ADD_DESC: [
                    CommandHandler("skip", add_product_desc),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_desc),
                ],
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
