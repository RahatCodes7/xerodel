from app.database import get_connection, get_setting, log_admin_action, now_iso

PLATFORMS = ("telegram", "crunchyroll", "cookie")


def list_products(include_inactive=False):
    conn = get_connection()
    if include_inactive:
        rows = conn.execute("SELECT * FROM products ORDER BY id DESC").fetchall()
    else:
        rows = conn.execute("SELECT * FROM products WHERE is_active = 1 ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_product(product_id):
    conn = get_connection()
    row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def add_product(platform, title, description, price_bdt, admin_id=None):
    if platform not in PLATFORMS:
        raise ValueError("Invalid platform")
    price = float(price_bdt)
    if price < 0:
        raise ValueError("Price must be positive")
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO products (platform, title, description, price_bdt, is_active, created_at) VALUES (?,?,?,?,1,?)",
        (platform, title.strip(), (description or "").strip(), price, now_iso()),
    )
    conn.commit()
    product_id = cur.lastrowid
    conn.close()
    if admin_id is not None:
        log_admin_action(admin_id, "product_add", "product", product_id, f"{platform}: {title} @ {price}")
    return get_product(product_id)


def update_product(product_id, title=None, description=None, price_bdt=None, platform=None, admin_id=None):
    product = get_product(product_id)
    if product is None:
        return None
    fields = []
    values = []
    if title is not None:
        fields.append("title = ?")
        values.append(title.strip())
    if description is not None:
        fields.append("description = ?")
        values.append((description or "").strip())
    if price_bdt is not None:
        fields.append("price_bdt = ?")
        values.append(float(price_bdt))
    if platform is not None:
        if platform not in PLATFORMS:
            raise ValueError("Invalid platform")
        fields.append("platform = ?")
        values.append(platform)
    if not fields:
        return product
    values.append(product_id)
    conn = get_connection()
    conn.execute(f"UPDATE products SET {', '.join(fields)} WHERE id = ?", values)
    conn.commit()
    conn.close()
    if admin_id is not None:
        log_admin_action(admin_id, "product_edit", "product", product_id, f"Updated {', '.join(fields)}")
    return get_product(product_id)


def toggle_product(product_id, admin_id=None):
    product = get_product(product_id)
    if product is None:
        return None
    new_state = 0 if product["is_active"] else 1
    conn = get_connection()
    conn.execute("UPDATE products SET is_active = ? WHERE id = ?", (new_state, product_id))
    conn.commit()
    conn.close()
    if admin_id is not None:
        log_admin_action(admin_id, "product_toggle", "product", product_id, f"is_active -> {new_state}")
    return get_product(product_id)


def delete_product(product_id, admin_id=None):
    conn = get_connection()
    count = conn.execute("SELECT COUNT(*) FROM inventory WHERE product_id = ?", (product_id,)).fetchone()[0]
    if count > 0:
        conn.close()
        raise ValueError("Product has inventory units — delete inventory first")
    cur = conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
    conn.commit()
    conn.close()
    if cur.rowcount and admin_id is not None:
        log_admin_action(admin_id, "product_delete", "product", product_id, "Product deleted")
    return bool(cur.rowcount)
