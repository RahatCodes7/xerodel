import os

from app.config import settings
from app.database import get_connection, get_user, log_admin_action, now_iso
from app.services import channels, delivery, orders as orders_svc


def get_proof(proof_id):
    conn = get_connection()
    row = conn.execute("SELECT * FROM payment_proofs WHERE id = ?", (proof_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_proof_by_order(order_id):
    conn = get_connection()
    row = conn.execute("SELECT * FROM payment_proofs WHERE order_id = ?", (order_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_proofs(status=None, limit=100):
    conn = get_connection()
    if status:
        rows = conn.execute(
            "SELECT * FROM payment_proofs WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM payment_proofs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


async def submit_proof(order_id, user_id, method, transaction_id, sender_number=None,
                       photo_file_id=None, photo_path=None, bot=None):
    order = orders_svc.get_order_by_id(order_id)
    if order is None or order["user_id"] != user_id:
        return {"ok": False, "error": "order_not_found"}
    if order["status"] not in ("pending_payment", "awaiting_proof"):
        return {"ok": False, "error": "invalid_status", "status": order["status"]}
    if not transaction_id or not transaction_id.strip():
        return {"ok": False, "error": "trxid_required"}

    transaction_id = transaction_id.strip()
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        duplicate = conn.execute(
            """SELECT id FROM payment_proofs
               WHERE order_id <> ? AND status <> 'rejected'
                 AND UPPER(transaction_id) = UPPER(?)""",
            (order_id, transaction_id),
        ).fetchone()
        if duplicate is not None:
            conn.rollback()
            return {"ok": False, "error": "trxid_in_use"}
        existing = conn.execute("SELECT * FROM payment_proofs WHERE order_id = ?", (order_id,)).fetchone()
        if existing is not None:
            if existing["status"] != "pending":
                conn.rollback()
                return {"ok": False, "error": "proof_already_reviewed"}
            conn.execute(
                """UPDATE payment_proofs SET method=?, transaction_id=?, sender_number=?,
                   photo_file_id=COALESCE(?, photo_file_id), photo_path=COALESCE(?, photo_path), created_at=?
                   WHERE id=?""",
                (method, transaction_id.strip(), sender_number, photo_file_id, photo_path, now_iso(), existing["id"]),
            )
            proof_id = existing["id"]
        else:
            cur = conn.execute(
                """INSERT INTO payment_proofs (order_id, method, transaction_id, sender_number,
                   photo_file_id, photo_path, status, created_at)
                   VALUES (?,?,?,?,?,?,'pending',?) RETURNING id""",
                (order_id, method, transaction_id.strip(), sender_number, photo_file_id, photo_path, now_iso()),
            )
            proof_id = cur.fetchone()["id"]
        conn.execute(
            "UPDATE orders SET status='awaiting_proof', payment_method=? WHERE id=?",
            (method, order_id),
        )
        conn.commit()
    finally:
        conn.close()

    proof = get_proof(proof_id)

    if bot is not None and photo_file_id and not photo_path:
        try:
            tg_file = await bot.get_file(photo_file_id)
            dest = os.path.join(settings.PROOF_DIR, f"{proof_id}.jpg")
            await tg_file.download_to_destination(dest)
            proof["photo_path"] = dest
            conn = get_connection()
            conn.execute("UPDATE payment_proofs SET photo_path=? WHERE id=?", (dest, proof_id))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[payments] photo download failed for proof {proof_id}: {e}")

    items = orders_svc.get_order_items(order_id)
    buyer = get_user(user_id)
    private_msg_id = await channels.post_private_proof(bot, proof, order, items, buyer)
    if private_msg_id:
        conn = get_connection()
        conn.execute("UPDATE payment_proofs SET private_msg_id=? WHERE id=?", (private_msg_id, proof_id))
        conn.commit()
        conn.close()
        proof["private_msg_id"] = private_msg_id

    fresh_order = orders_svc.get_order_by_id(order_id)
    await channels.edit_public_order(bot, fresh_order, items, buyer)
    return {"ok": True, "proof": proof}


async def decide(order_id, decision, admin_id, note="", bot=None):
    decision = (decision or "").strip().lower()
    if decision not in ("accepted", "rejected"):
        return {"ok": False, "error": "bad_decision"}

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE payment_proofs SET status=?, reviewed_by=?, reviewed_at=?, note=? WHERE order_id=? AND status='pending'",
            (decision, admin_id, now_iso(), (note or None), order_id),
        )
        if cur.rowcount == 0:
            conn.rollback()
            return {"ok": False, "reason": "already_processed"}
        proof_row = conn.execute("SELECT * FROM payment_proofs WHERE order_id=?", (order_id,)).fetchone()
        order_row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if decision == "accepted":
            conn.execute("UPDATE orders SET status='paid', paid_at=? WHERE id=?", (now_iso(), order_id))
        else:
            conn.execute("UPDATE orders SET status='rejected' WHERE id=?", (order_id,))
        conn.commit()
        proof = dict(proof_row)
        order = dict(order_row)
    finally:
        conn.close()

    items = orders_svc.get_order_items(order_id)
    buyer = get_user(order["user_id"])
    admin_user = get_user(admin_id) if admin_id else None

    if decision == "accepted":
        result = await delivery.deliver_order(bot, order_id, admin_id)
        if result.get("ok"):
            decision_text = "✅ Accepted — paid & delivered"
        elif result.get("reason") == "out_of_stock":
            decision_text = "⚠️ Accepted, but order cancelled — item went out of stock before verification"
        else:
            decision_text = "✅ Accepted"
        await channels.edit_private_proof(bot, proof, order, items, decision_text, admin_user)
    else:
        fresh_order = orders_svc.get_order_by_id(order_id)
        await channels.edit_public_order(bot, fresh_order, items, buyer)
        await channels.edit_private_proof(bot, proof, order, items, "❌ Rejected", admin_user)
        if bot is not None:
            try:
                msg = f"❌ Your payment proof for order {order['order_code']} was rejected."
                if note:
                    msg += f"\nReason: {note}"
                await bot.send_message(chat_id=order["user_id"], text=msg)
            except Exception as e:
                print(f"[payments] reject DM failed for {order['order_code']}: {e}")

    log_admin_action(
        admin_id, f"proof_{decision}", "order", order["order_code"],
        f"Proof {proof['id']} ({proof['method']} {proof['transaction_id']}); note: {note or '-'}",
    )
    return {"ok": True, "decision": decision, "order_code": order["order_code"]}
