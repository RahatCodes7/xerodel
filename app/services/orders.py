import random
from datetime import datetime, timedelta, timezone

from app.database import get_connection, get_expiry_minutes, get_user, log_admin_action, now_iso


class OutOfStockError(Exception):
    def __init__(self, title, available):
        self.title = title
        self.available = available
        super().__init__(f"Out of stock: {title}")


MAX_QTY_PER_ITEM = 20


def gen_order_code():
    while True:
        code = f"ORD-{random.randint(10000, 99999)}"
        conn = get_connection()
        row = conn.execute("SELECT 1 FROM orders WHERE order_code = ?", (code,)).fetchone()
        conn.close()
        if row is None:
            return code


def create_order(user_id, items):
    if not items:
        raise ValueError("Order has no items")
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        total = 0.0
        parsed = []
        for item in items:
            product_id = int(item["product_id"])
            qty = int(item["qty"])
            if qty < 1 or qty > MAX_QTY_PER_ITEM:
                raise ValueError(f"Quantity must be between 1 and {MAX_QTY_PER_ITEM}")
            product = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
            if product is None or not product["is_active"]:
                raise ValueError("Product is not available")
            available = conn.execute(
                "SELECT COUNT(*) FROM inventory WHERE product_id = ? AND status = 'available'", (product_id,)
            ).fetchone()[0]
            if available < qty:
                raise OutOfStockError(product["title"], available)
            total += product["price_bdt"] * qty
            parsed.append((product_id, qty, product["price_bdt"]))
        code = gen_order_code()
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=get_expiry_minutes())).strftime("%Y-%m-%dT%H:%M:%S")
        cur = conn.execute(
            "INSERT INTO orders (order_code, user_id, status, total_bdt, payment_method, created_at, expires_at) VALUES (?,?, 'pending_payment', ?, NULL, ?, ?)",
            (code, user_id, total, now_iso(), expires_at),
        )
        order_id = cur.lastrowid
        for product_id, qty, unit_price in parsed:
            conn.execute(
                "INSERT INTO order_items (order_id, product_id, quantity, unit_price) VALUES (?,?,?,?)",
                (order_id, product_id, qty, unit_price),
            )
        conn.commit()
        return get_order_with_items(code)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


async def place_order(user_id, items, bot=None):
    from app.services import channels

    order = create_order(user_id, items)
    buyer = get_user(user_id)
    msg_id = await channels.post_public_order(bot, order, order["items"], buyer)
    if msg_id:
        set_public_msg(order["id"], msg_id)
    return order


def get_order_by_id(order_id):
    conn = get_connection()
    row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_order_by_code(order_code):
    conn = get_connection()
    row = conn.execute("SELECT * FROM orders WHERE order_code = ?", (order_code,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_order_items(order_id):
    conn = get_connection()
    rows = conn.execute(
        """SELECT oi.*, p.title AS product_title, p.platform AS product_platform
           FROM order_items oi JOIN products p ON p.id = oi.product_id
           WHERE oi.order_id = ? ORDER BY oi.id""",
        (order_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_order_with_items(order_code):
    order = get_order_by_code(order_code)
    if order is None:
        return None
    order["items"] = get_order_items(order["id"])
    return order


def get_user_orders(user_id, limit=50):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit)
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        order = dict(r)
        order["items"] = get_order_items(order["id"])
        result.append(order)
    return result


def list_orders(status=None, limit=100, offset=0):
    conn = get_connection()
    if status:
        rows = conn.execute(
            "SELECT * FROM orders WHERE status = ? ORDER BY id DESC LIMIT ? OFFSET ?",
            (status, limit, offset),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def count_orders(status=None):
    conn = get_connection()
    if status:
        n = conn.execute("SELECT COUNT(*) FROM orders WHERE status = ?", (status,)).fetchone()[0]
    else:
        n = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    conn.close()
    return n


def revenue_total():
    conn = get_connection()
    n = conn.execute(
        "SELECT COALESCE(SUM(total_bdt), 0) FROM orders WHERE status IN ('paid','delivered')"
    ).fetchone()[0]
    conn.close()
    return n


def set_public_msg(order_id, msg_id):
    conn = get_connection()
    conn.execute("UPDATE orders SET public_msg_id = ? WHERE id = ?", (msg_id, order_id))
    conn.commit()
    conn.close()


def mark_paid(order_id):
    conn = get_connection()
    conn.execute("UPDATE orders SET status = 'paid', paid_at = ? WHERE id = ?", (now_iso(), order_id))
    conn.commit()
    conn.close()


def mark_delivered(order_id, delivery_status="dm_sent", delivery_note=None):
    conn = get_connection()
    conn.execute(
        "UPDATE orders SET status = 'delivered', delivered_at = ?, delivery_status = ?, delivery_note = ? WHERE id = ?",
        (now_iso(), delivery_status, delivery_note, order_id),
    )
    conn.commit()
    conn.close()


def mark_cancelled(order_id, reason=None):
    conn = get_connection()
    conn.execute(
        "UPDATE orders SET status = 'cancelled', cancel_reason = ? WHERE id = ?",
        (reason, order_id),
    )
    conn.commit()
    conn.close()


def mark_rejected(order_id):
    conn = get_connection()
    conn.execute("UPDATE orders SET status = 'rejected' WHERE id = ?", (order_id,))
    conn.commit()
    conn.close()


def get_expired_pending():
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM orders WHERE status = 'pending_payment' AND expires_at <= ?",
        (now_iso(),),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def cancel_expired(order_id):
    conn = get_connection()
    cur = conn.execute(
        "UPDATE orders SET status = 'cancelled', cancel_reason = 'expired' WHERE id = ? AND status = 'pending_payment'",
        (order_id,),
    )
    conn.commit()
    conn.close()
    return cur.rowcount > 0


async def expire_pending_orders(bot=None):
    from app.services import channels

    expired = get_expired_pending()
    results = []
    for order in expired:
        if cancel_expired(order["id"]):
            items = get_order_items(order["id"])
            buyer = get_user(order["user_id"])
            await channels.edit_public_order(bot, order, items, buyer)
            if bot is not None:
                try:
                    await bot.send_message(
                        chat_id=order["user_id"],
                        text=(
                            f"⏰ Order {order['order_code']} was automatically cancelled because payment "
                            "was not completed within the time limit. You can place a new order anytime."
                        ),
                    )
                except Exception as e:
                    print(f"[expiry] notify failed for {order['order_code']}: {e}")
            log_admin_action(0, "order_expired", "order", order["order_code"], "Auto-cancelled by expiry sweep")
            results.append(order["order_code"])
    return results


def stats_summary():
    conn = get_connection()
    orders_total = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    pending_payment = conn.execute("SELECT COUNT(*) FROM orders WHERE status='pending_payment'").fetchone()[0]
    awaiting_proof = conn.execute("SELECT COUNT(*) FROM orders WHERE status='awaiting_proof'").fetchone()[0]
    delivered = conn.execute("SELECT COUNT(*) FROM orders WHERE status='delivered'").fetchone()[0]
    revenue = conn.execute("SELECT COALESCE(SUM(total_bdt),0) FROM orders WHERE status IN ('paid','delivered')").fetchone()[0]
    proofs_pending = conn.execute("SELECT COUNT(*) FROM payment_proofs WHERE status='pending'").fetchone()[0]
    stock_available = conn.execute("SELECT COUNT(*) FROM inventory WHERE status='available'").fetchone()[0]
    users_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    return {
        "orders_total": orders_total,
        "pending_payment": pending_payment,
        "awaiting_proof": awaiting_proof,
        "delivered": delivered,
        "revenue": revenue,
        "proofs_pending": proofs_pending,
        "stock_available": stock_available,
        "users_count": users_count,
    }
