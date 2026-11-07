from app.database import get_connection, log_admin_action, now_iso


def available_count(product_id):
    conn = get_connection()
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM inventory WHERE product_id = ? AND status = 'available'", (product_id,)
    ).fetchone()["n"]
    conn.close()
    return n


def counts_by_product():
    conn = get_connection()
    rows = conn.execute(
        "SELECT product_id, status, COUNT(*) AS n FROM inventory GROUP BY product_id, status"
    ).fetchall()
    conn.close()
    counts = {}
    for r in rows:
        counts.setdefault(r["product_id"], {"available": 0, "assigned": 0, "sold": 0})
        counts[r["product_id"]][r["status"]] = r["n"]
    return counts


def add_item(product_id, account_username, account_password, cookie_data=None):
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO inventory (product_id, account_username, account_password, cookie_data, status, created_at) VALUES (?,?,?,?, 'available', ?) RETURNING id",
        (product_id, account_username, account_password, cookie_data, now_iso()),
    )
    inventory_id = cur.fetchone()["id"]
    conn.commit()
    conn.close()
    return inventory_id


def bulk_add(product_id, text):
    added = 0
    errors = []
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        for lineno, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 2 or not parts[0] or not parts[1]:
                errors.append(f"Line {lineno}: expected format 'username | password | cookie'")
                continue
            username, password = parts[0], parts[1]
            cookie = parts[2] if len(parts) > 2 else None
            conn.execute(
                "INSERT INTO inventory (product_id, account_username, account_password, cookie_data, status, created_at) VALUES (?,?,?,?, 'available', ?)",
                (product_id, username, password, cookie, now_iso()),
            )
            added += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return added, errors


def assign_to_order(order_id, items):
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        assigned_ids = []
        for item in items:
            product_id = int(item["product_id"])
            qty = int(item.get("quantity") or item.get("qty") or 0)
            rows = conn.execute(
                "SELECT id FROM inventory WHERE product_id = ? AND status = 'available' "
                "ORDER BY id LIMIT ? FOR UPDATE SKIP LOCKED",
                (product_id, qty),
            ).fetchall()
            if len(rows) < qty:
                conn.rollback()
                return {"ok": False, "reason": "out_of_stock", "product_id": product_id, "assigned": []}
            ids = [r["id"] for r in rows]
            placeholders = ",".join("?" * len(ids))
            conn.execute(
                f"UPDATE inventory SET status = 'assigned', assigned_order_id = ? WHERE id IN ({placeholders})",
                (order_id, *ids),
            )
            for inv_id in ids:
                conn.execute(
                    "INSERT INTO order_inventory (order_id, inventory_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
                    (order_id, inv_id),
                )
            assigned_ids.extend(ids)
        conn.commit()
        return {"ok": True, "assigned": assigned_ids}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_sold(inventory_ids):
    if not inventory_ids:
        return
    conn = get_connection()
    placeholders = ",".join("?" * len(inventory_ids))
    conn.execute(
        f"UPDATE inventory SET status = 'sold', sold_at = ? WHERE id IN ({placeholders})",
        (now_iso(), *inventory_ids),
    )
    conn.commit()
    conn.close()


def release_order_inventory(order_id):
    conn = get_connection()
    conn.execute(
        "UPDATE inventory SET status = 'available', assigned_order_id = NULL WHERE assigned_order_id = ? AND status = 'assigned'",
        (order_id,),
    )
    conn.execute("DELETE FROM order_inventory WHERE order_id = ?", (order_id,))
    conn.commit()
    conn.close()


def get_assigned_inventory(order_id):
    conn = get_connection()
    rows = conn.execute(
        """SELECT i.* FROM inventory i JOIN order_inventory oi ON oi.inventory_id = i.id
           WHERE oi.order_id = ? ORDER BY i.id""",
        (order_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_for_product(product_id, status=None, limit=500):
    conn = get_connection()
    if status:
        rows = conn.execute(
            "SELECT * FROM inventory WHERE product_id = ? AND status = ? ORDER BY id DESC LIMIT ?",
            (product_id, status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM inventory WHERE product_id = ? ORDER BY id DESC LIMIT ?",
            (product_id, limit),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_item(inventory_id):
    conn = get_connection()
    row = conn.execute("SELECT * FROM inventory WHERE id = ?", (inventory_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_item(inventory_id):
    conn = get_connection()
    cur = conn.execute("DELETE FROM inventory WHERE id = ? AND status = 'available'", (inventory_id,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def inventory_stats():
    conn = get_connection()
    available = conn.execute("SELECT COUNT(*) AS n FROM inventory WHERE status='available'").fetchone()["n"]
    assigned = conn.execute("SELECT COUNT(*) AS n FROM inventory WHERE status='assigned'").fetchone()["n"]
    sold = conn.execute("SELECT COUNT(*) AS n FROM inventory WHERE status='sold'").fetchone()["n"]
    conn.close()
    return {"available": available, "assigned": assigned, "sold": sold}


def delete_inventory_for_product(product_id):
    conn = get_connection()
    conn.execute("DELETE FROM inventory WHERE product_id = ? AND status = 'available'", (product_id,))
    conn.commit()
    conn.close()


def log_inventory_add(product_id, count, admin_id):
    log_admin_action(admin_id, "inventory_add", "product", product_id, f"Added {count} units")
