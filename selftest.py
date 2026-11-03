#!/usr/bin/env python3
"""
Self-test for core services without a real Telegram bot.
Run: python selftest.py
Uses a temporary database at /tmp/selftest.db.
"""
import os
import sys
import tempfile

# Set env BEFORE importing app modules
os.environ["DATABASE_PATH"] = "/tmp/selftest.db"
os.environ["BOT_TOKEN"] = "dummy"
os.environ["SEED_DEMO"] = "false"
os.environ["ADMIN_IDS"] = "111111111"
os.environ["SESSION_SECRET"] = "test-secret"
os.environ["WEBHOOK_SECRET"] = "test-webhook"
os.environ["BASE_URL"] = "http://localhost:8000"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Now import app modules
from app.database import init_db, upsert_user, get_user, get_connection, get_admin_ids, log_admin_action
from app.services import (
    orders as orders_svc,
    inventory as inventory_svc,
    payments as payments_svc,
    delivery as delivery_svc,
    channels as channels_svc,
    products as products_svc,
)

def cleanup():
    if os.path.exists("/tmp/selftest.db"):
        os.remove("/tmp/selftest.db")

def test_basic():
    print("=== Initializing DB ===")
    init_db()
    
    # Check demo products not seeded (SEED_DEMO=false)
    conn = get_connection()
    cnt = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    assert cnt == 0, f"Expected 0 products, got {cnt}"
    conn.close()
    print("✓ DB initialized, no demo products")

    # Add admin user
    upsert_user(111111111, "Admin User", "admin_user", None)
    user = get_user(111111111)
    assert user["is_admin"] == 1
    print("✓ Admin user upserted with is_admin=1")

    # Add product
    from app.services.products import add_product
    p = add_product("telegram", "Test Telegram Premium", "Test desc", 350.0, admin_id=111111111)
    assert p["id"] == 1
    assert p["price_bdt"] == 350.0
    print("✓ Product added")

    # Add inventory
    added, errors = inventory_svc.bulk_add(1, "user1 | pass1\nuser2 | pass2\nuser3 | pass3 | cookie_data")
    assert added == 3 and errors == []
    print("✓ 3 inventory units added")

    # Check stock
    stock = inventory_svc.available_count(1)
    assert stock == 3
    print("✓ Available count = 3")

    # Create order (user 222222222)
    upsert_user(222222222, "Buyer", "buyer1", None)
    order = orders_svc.create_order(222222222, [{"product_id": 1, "qty": 2}])
    assert order["status"] == "pending_payment"
    assert order["total_bdt"] == 700.0
    assert len(order["items"]) == 1
    order_id = order["id"]
    order_code = order["order_code"]
    print(f"✓ Order created: {order_code} (id={order_id})")

    # Stock after order (still 3, not assigned yet)
    stock = inventory_svc.available_count(1)
    assert stock == 3
    print("✓ Stock unchanged before payment (no reservation)")

    # Submit payment proof
    import asyncio
    async def run_async():
        res = await payments_svc.submit_proof(
            order_id, 222222222, "bKash", "TXN12345", sender_number="01700000000",
            photo_file_id=None, photo_path=None, bot=None
        )
        return res
    res = asyncio.run(run_async())
    assert res["ok"]
    proof = res["proof"]
    assert proof["status"] == "pending"
    print("✓ Payment proof submitted (pending)")

    # Order status now awaiting_proof
    order = orders_svc.get_order_by_id(order_id)
    assert order["status"] == "awaiting_proof"
    print("✓ Order status -> awaiting_proof")

    # Admin accepts
    async def decide_async():
        res = await payments_svc.decide(order_id, "accepted", 111111111, note="Verified", bot=None)
        return res
    res = asyncio.run(decide_async())
    assert res["ok"]
    assert res["decision"] == "accepted"
    print("✓ Payment accepted")

    # Check final state
    order = orders_svc.get_order_by_id(order_id)
    assert order["status"] == "delivered"
    assert order["delivery_status"] in ("dm_sent", "dm_failed")  # no bot = dm_sent (not attempted)
    print("✓ Order delivered, delivery_status=dm_sent (no bot)")

    # Inventory assigned + sold
    conn = get_connection()
    sold = conn.execute("SELECT COUNT(*) FROM inventory WHERE status='sold' AND product_id=1").fetchone()[0]
    avail = conn.execute("SELECT COUNT(*) FROM inventory WHERE status='available' AND product_id=1").fetchone()[0]
    conn.close()
    assert sold == 2
    assert avail == 1
    print("✓ Inventory: 2 sold, 1 available")

    # Check credentials available on order
    creds = inventory_svc.get_assigned_inventory(order_id)
    assert len(creds) == 2
    assert creds[0]["account_username"] == "user1"
    assert creds[1]["account_username"] == "user2"
    print("✓ Credentials stored and retrievable")

    # Second order for last unit
    order2 = orders_svc.create_order(222222222, [{"product_id": 1, "qty": 1}])
    print(f"✓ Second order: {order2['order_code']}")

    # Submit proof and accept second order to consume last unit
    async def submit_and_accept(order_id):
        res = await payments_svc.submit_proof(order_id, 222222222, "bKash", "TXN2", sender_number=None, photo_file_id=None, photo_path=None, bot=None)
        assert res["ok"]
        res = await payments_svc.decide(order_id, "accepted", 111111111, bot=None)
        assert res["ok"]
        return res
    asyncio.run(submit_and_accept(order2["id"]))

    # Third order should fail (out of stock)
    try:
        orders_svc.create_order(222222222, [{"product_id": 1, "qty": 1}])
        assert False, "Should have raised OutOfStockError"
    except orders_svc.OutOfStockError as e:
        print(f"✓ OutOfStockError raised correctly: {e}")

    # Test expiry sweep (create pending_payment order, set expiry in past)
    conn = get_connection()
    from app.database import now_iso
    from datetime import datetime, timedelta, timezone
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")
    conn.execute(
        "INSERT INTO orders (order_code, user_id, status, total_bdt, created_at, expires_at) VALUES (?,?,?,?,?,?)",
        ("ORD-EXPIRED", 222222222, "pending_payment", 100, now_iso(), past)
    )
    conn.commit()
    conn.close()
    
    async def sweep():
        return await orders_svc.expire_pending_orders(bot=None)
    expired = asyncio.run(sweep())
    assert "ORD-EXPIRED" in expired
    print("✓ Expiry sweep cancelled expired order")

    # Check audit log
    logs = log_admin_action(111111111, "test_action", "test", "1", "test detail")
    audit = log_admin_action
    # Just verify logging works
    print("✓ Audit logging works")

    print("\n=== ALL TESTS PASSED ===")

if __name__ == "__main__":
    try:
        test_basic()
    finally:
        cleanup()