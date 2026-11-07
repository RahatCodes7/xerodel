import io

from app.database import get_channel_ids, get_user, log_admin_action
from app.services import channels, inventory as inventory_svc, orders as orders_svc
from app.services.channels import esc


def cookie_filename(order_code, index):
    return f"cookie-{order_code}-{index}.txt"


def cookie_bytes(cookie_data):
    """Cookie files are handed over as a .txt attachment, never inline text."""
    return ((cookie_data or "").strip() + "\n").encode("utf-8")


def cookie_rows(inventory_rows):
    return [inv for inv in inventory_rows if (inv.get("cookie_data") or "").strip()]


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
