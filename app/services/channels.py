import os

from app.config import settings
from app.database import get_connection, get_channel_ids

STATUS_BADGES = {
    "pending_payment": "⏳ Pending Payment",
    "awaiting_proof": "📨 Awaiting Proof",
    "paid": "✅ Paid",
    "delivered": "📦 Delivered",
    "cancelled": "❌ Cancelled",
    "rejected": "❌ Rejected",
}

PLATFORM_LABELS = {"telegram": "Telegram", "crunchyroll": "Crunchyroll", "cookie": "Cookie"}


def fmt_money(amount):
    try:
        if float(amount) == int(float(amount)):
            return f"{settings.SITE_CURRENCY}{int(float(amount)):,}"
    except (TypeError, ValueError):
        pass
    return f"{settings.SITE_CURRENCY}{amount:,.2f}"


def masked_buyer(user):
    if user and user.get("username"):
        return f"@{user['username']}"
    if user:
        return f"Buyer #{str(user['telegram_id'])[-4:]}"
    return "Buyer"


def buyer_label(user):
    name = (user or {}).get("full_name") or "Unknown"
    username = (user or {}).get("username")
    uid = (user or {}).get("telegram_id")
    parts = [name]
    if username:
        parts.append(f"@{username}")
    if uid:
        parts.append(f"[id: {uid}]")
    return " ".join(parts)


def items_summary(items):
    lines = []
    for it in items:
        title = it.get("product_title") or "Product"
        lines.append(f"• {title} × {it['quantity']}")
    return "\n".join(lines)


def public_order_text(order, items, buyer_user=None):
    badge = STATUS_BADGES.get(order["status"], order["status"])
    lines = [
        "🛒 <b>New Order</b>" if order["status"] == "pending_payment" else "🛒 <b>Order Update</b>",
        "",
        f"🔢 Order: <code>{order['order_code']}</code>",
        items_summary(items),
        f"💰 Amount: {fmt_money(order['total_bdt'])}",
        f"👤 Buyer: {masked_buyer(buyer_user)}",
        "",
        f"Status: {badge}",
    ]
    if order["status"] == "pending_payment":
        lines.append("⏰ Pay within the time limit shown on your order page")
    return "\n".join(lines)


def private_proof_text(proof, order, items, buyer_user=None):
    lines = [
        f"💳 <b>Payment Proof — {order['order_code']}</b>",
        "",
        f"👤 Buyer: {buyer_label(buyer_user)}",
        items_summary(items),
        f"💰 Amount: {fmt_money(order['total_bdt'])}",
        f"🏦 Method: {proof.get('method') or 'n/a'}",
        f"🔢 TrxID: {proof.get('transaction_id') or 'n/a'}",
    ]
    if proof.get("sender_number"):
        lines.append(f"📱 Sender: {proof['sender_number']}")
    lines += ["", "Review the transaction and accept or reject."]
    return "\n".join(lines)


def channel_post_link(channel_id, msg_id):
    if not channel_id or not msg_id:
        return None
    try:
        cid = str(channel_id)
        if cid.startswith("-100"):
            cid = cid[4:]
        return f"https://t.me/c/{cid}/{msg_id}"
    except Exception:
        return None


async def post_public_order(bot, order, items, buyer_user=None):
    public_id, _ = get_channel_ids()
    if bot is None or not public_id:
        return None
    text = public_order_text(order, items, buyer_user)
    try:
        msg = await bot.send_message(chat_id=int(public_id), text=text)
        return msg.message_id
    except Exception as e:
        print(f"[channels] public post failed for {order['order_code']}: {e}")
        return None


async def edit_public_order(bot, order, items, buyer_user=None):
    public_id, _ = get_channel_ids()
    if bot is None or not public_id or not order.get("public_msg_id"):
        return False
    text = public_order_text(order, items, buyer_user)
    try:
        await bot.edit_message_text(
            chat_id=int(public_id), message_id=order["public_msg_id"], text=text
        )
        return True
    except Exception as e:
        print(f"[channels] public edit failed for {order['order_code']}: {e}")
        return False


async def post_private_proof(bot, proof, order, items, buyer_user=None):
    _, private_id = get_channel_ids()
    if bot is None or not private_id:
        return None
    caption = private_proof_text(proof, order, items, buyer_user)
    markup = {
        "inline_keyboard": [
            [
                {"text": "✅ Accept", "callback_data": f"pf_acc_{order['id']}"},
                {"text": "❌ Reject", "callback_data": f"pf_rej_{order['id']}"},
            ]
        ]
    }
    try:
        if proof.get("photo_file_id"):
            msg = await bot.send_photo(
                chat_id=int(private_id), photo=proof["photo_file_id"], caption=caption, reply_markup=markup
            )
        elif proof.get("photo_path") and os.path.exists(proof["photo_path"]):
            with open(proof["photo_path"], "rb") as f:
                msg = await bot.send_photo(
                    chat_id=int(private_id), photo=f, caption=caption, reply_markup=markup
                )
        else:
            msg = await bot.send_message(chat_id=int(private_id), text=caption, reply_markup=markup)
        return msg.message_id
    except Exception as e:
        print(f"[channels] private proof post failed for order {order['order_code']}: {e}")
        return None


async def edit_private_proof(bot, proof, order, items, decision_text, admin_user=None):
    _, private_id = get_channel_ids()
    if bot is None or not private_id or not proof.get("private_msg_id"):
        return False
    caption = private_proof_text(proof, order, items)
    admin_name = ""
    if admin_user:
        admin_name = admin_user.get("username") or admin_user.get("full_name") or str(admin_user.get("telegram_id", ""))
    caption += f"\n\n{decision_text}"
    if admin_name:
        caption += f" by @{admin_name}" if admin_name.startswith("@") or admin_name.isdigit() is False else f" by {admin_name}"
    try:
        await bot.edit_message_caption(
            chat_id=int(private_id),
            message_id=proof["private_msg_id"],
            caption=caption,
            reply_markup={"inline_keyboard": []},
        )
        return True
    except Exception:
        try:
            await bot.edit_message_text(
                chat_id=int(private_id),
                message_id=proof["private_msg_id"],
                text=caption,
                reply_markup={"inline_keyboard": []},
            )
            return True
        except Exception as e:
            print(f"[channels] private proof edit failed for order {order['order_code']}: {e}")
            return False


def get_proof_with_order(proof_id):
    conn = get_connection()
    row = conn.execute("SELECT * FROM payment_proofs WHERE id = ?", (proof_id,)).fetchone()
    conn.close()
    return dict(row) if row else None
