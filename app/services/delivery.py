import io
from pathlib import Path

from app.database import get_channel_ids, get_user, log_admin_action
from app.services import channels, inventory as inventory_svc, orders as orders_svc
from app.services.channels import esc

ID_BOT_USERNAME = "id_bot"
SUPPORT_USERNAME = "zyokr"
LOGIN_PHRASE = "Xerodel Cookie To Login"

VIDEO_PATH = Path(__file__).resolve().parents[2] / "Xerodel.MP4"
_VIDEO_FILE_ID = None


def login_guide_video_path():
    return VIDEO_PATH


def cookie_filename(order_code, index):
    return f"cookie-{order_code}-{index}.txt"


def cookie_bytes(cookie_data):
    """Cookie files are handed over as a .txt attachment, never inline text."""
    return ((cookie_data or "").strip() + "\n").encode("utf-8")


def cookie_rows(inventory_rows):
    return [inv for inv in inventory_rows if (inv.get("cookie_data") or "").strip()]


def cookie_username_list(inventory_rows):
    return [(inv.get("account_username") or "Cookie") for inv in cookie_rows(inventory_rows)]


def cookie_login_lines(usernames, use_html=False):
    """Bilingual how-to-login steps shown with the guide video."""
    bold = (lambda s: f"<b>{s}</b>") if use_html else (lambda s: s)
    code = (lambda s: f"<code>{s}</code>") if use_html else (lambda s: s)
    esc_name = esc if use_html else (lambda s: s)
    names = ", ".join(esc_name(n) for n in usernames) or "-"
    return [
        bold("🔐 Cookie login — কীভাবে লগইন লিংক পাবেন"),
        "",
        bold("English"),
        f"1) Open t.me/{ID_BOT_USERNAME} and send /start — copy the ID it gives you.",
        f"2) Message t.me/{SUPPORT_USERNAME} with: {bold(LOGIN_PHRASE)}",
        f"3) Send your Username: {code(names)}",
        "4) Send that ID too — then you get your Xerodel Cookie login link.",
        "",
        bold("বাংলা"),
        f"১) t.me/{ID_BOT_USERNAME} এ যান, /start দিন — যে ID আসবে সেটা কপি করুন।",
        f"২) t.me/{SUPPORT_USERNAME} কে মেসেজ দিন: {bold(LOGIN_PHRASE)}",
        f"৩) আপনার Username দিন: {code(names)}",
        "৪) সাথে ওই ID টাও পাঠান — তখনই Xerodel Cookie লগইন লিংক পাবেন।",
    ]


async def send_login_guide_video(bot, chat_id, caption=None):
    """Send Xerodel.MP4 once per process, reusing the Telegram file_id after that."""
    global _VIDEO_FILE_ID
    if bot is None or not VIDEO_PATH.exists():
        return False
    try:
        if _VIDEO_FILE_ID:
            await bot.send_video(
                chat_id=chat_id, video=_VIDEO_FILE_ID, caption=caption, supports_streaming=True
            )
        else:
            with open(VIDEO_PATH, "rb") as fh:
                msg = await bot.send_video(
                    chat_id=chat_id,
                    video=fh,
                    filename="Xerodel.mp4",
                    caption=caption,
                    supports_streaming=True,
                )
            if getattr(msg, "video", None):
                _VIDEO_FILE_ID = msg.video.file_id
        return True
    except Exception as e:
        print(f"[delivery] guide video send failed: {e}")
        return False


async def send_cookie_login_guide(bot, chat_id, inventory_rows, with_video=True):
    """Video + bilingual instructions telling the buyer how to get their login link."""
    rows = cookie_rows(inventory_rows)
    if bot is None or not rows:
        return
    if with_video:
        await send_login_guide_video(bot, chat_id, caption="🎬 Xerodel — Cookie login guide")
    text = "\n".join(cookie_login_lines(cookie_username_list(rows), use_html=True))
    try:
        await bot.send_message(
            chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=True
        )
    except Exception as e:
        print(f"[delivery] login guide text failed: {e}")


def build_credentials_text(order, items, inventory_rows):
    lines = [f"🎉 Order <b>{order['order_code']}</b> confirmed and paid!", ""]
    for item in items:
        invs = [i for i in inventory_rows if i["product_id"] == item["product_id"]]
        lines.append(f"📦 {esc(item.get('product_title', 'Product'))} × {item['quantity']}")
        for idx, inv in enumerate(invs, 1):
            lines.append("")
            lines.append(f"Account {idx}:")
            lines.append(f"Username: <code>{esc(inv['account_username'])}</code>")
            lines.append(f"Password: <code>{esc(inv['account_password'])}</code>")
            if (inv.get("cookie_data") or "").strip():
                lines.append("Cookie: sent as a .txt file below")
        lines.append("")
    lines.append("Thank you for your purchase!")
    return "\n".join(lines)


def build_credentials_plain(order, items, inventory_rows):
    lines = [f"Order {order['order_code']} confirmed and paid!", ""]
    for item in items:
        invs = [i for i in inventory_rows if i["product_id"] == item["product_id"]]
        lines.append(f"{item.get('product_title', 'Product')} x {item['quantity']}")
        for idx, inv in enumerate(invs, 1):
            lines.append("")
            lines.append(f"Account {idx}:")
            lines.append(f"Username: {inv['account_username'] or ''}")
            lines.append(f"Password: {inv['account_password'] or ''}")
            if (inv.get("cookie_data") or "").strip():
                lines.append("Cookie: see the attached .txt file")
        lines.append("")
    lines.append("Thank you for your purchase!")
    return "\n".join(lines)


async def deliver_order(bot, order_id, admin_id=0):
    order = orders_svc.get_order_by_id(order_id)
    if order is None:
        return {"ok": False, "reason": "order_not_found"}
    items = orders_svc.get_order_items(order_id)
    buyer = get_user(order["user_id"])

    result = inventory_svc.assign_to_order(order_id, items)
    if not result["ok"]:
        orders_svc.mark_cancelled(order_id, reason="out_of_stock")
        fresh = orders_svc.get_order_by_id(order_id)
        await channels.edit_public_order(bot, fresh, items, buyer)
        if bot is not None:
            try:
                await bot.send_message(
                    chat_id=order["user_id"],
                    text=(
                        f"❌ Order {order['order_code']} was cancelled because the item went out of stock "
                        "before your payment was verified. If you already sent money, contact support with "
                        "your TrxID for a refund."
                    ),
                )
            except Exception as e:
                print(f"[delivery] refund DM failed for {order['order_code']}: {e}")
        log_admin_action(
            admin_id, "order_cancelled_out_of_stock", "order", order["order_code"],
            "Inventory race: insufficient stock at verification time; order cancelled",
        )
        return {"ok": False, "reason": "out_of_stock"}

    assigned_ids = result["assigned"]
    inventory_rows = inventory_svc.get_assigned_inventory(order_id)
    credentials_html = build_credentials_text(order, items, inventory_rows)
    credentials_plain = build_credentials_plain(order, items, inventory_rows)

    dm_ok = True
    dm_error = None
    if bot is not None:
        try:
            await bot.send_message(chat_id=order["user_id"], text=credentials_html, parse_mode="HTML")
        except Exception as e:
            dm_ok = False
            dm_error = str(e)
            print(f"[delivery] DM failed for order {order['order_code']}: {e}")
        if dm_ok:
            for idx, inv in enumerate(cookie_rows(inventory_rows), 1):
                try:
                    await bot.send_document(
                        chat_id=order["user_id"],
                        document=io.BytesIO(cookie_bytes(inv["cookie_data"])),
                        filename=cookie_filename(order["order_code"], idx),
                        caption=f"🍪 Cookie {idx} · {inv['account_username'] or 'account'}",
                    )
                except Exception as e:
                    print(f"[delivery] cookie file DM failed for order {order['order_code']}: {e}")
            await send_cookie_login_guide(bot, order["user_id"], inventory_rows)

    inventory_svc.mark_sold(assigned_ids)
    orders_svc.mark_delivered(
        order_id,
        delivery_status="dm_sent" if dm_ok else "dm_failed",
        delivery_note=None if dm_ok else f"DM failed: {dm_error}. Credentials visible on website order page.",
    )

    fresh = orders_svc.get_order_by_id(order_id)
    await channels.edit_public_order(bot, fresh, items, buyer)

    if not dm_ok:
        _, private_id = get_channel_ids()
        if bot is not None and private_id:
            try:
                await bot.send_message(
                    chat_id=int(private_id),
                    text=(
                        f"⚠️ Could not DM credentials to buyer of order {order['order_code']} "
                        f"(user {order['user_id']} may have blocked the bot). "
                        "Credentials are visible on the buyer's website order page."
                    ),
                )
            except Exception as e:
                print(f"[delivery] admin alert failed: {e}")

    return {"ok": True, "dm_ok": dm_ok, "credentials": credentials_plain}
