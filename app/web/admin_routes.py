import os
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.database import (
    get_audit_log,
    get_channel_ids,
    get_connection,
    get_setting,
    get_user,
    log_admin_action,
    set_setting,
)
from app.services import (
    inventory as inventory_svc,
    orders as orders_svc,
    payments as payments_svc,
    products as products_svc,
)
from app.services.channels import PLATFORM_LABELS, STATUS_BADGES, channel_post_link, fmt_money
from app.telegram import bot as tg_bot
from app.web.auth import get_admin_user, get_current_user

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def render(request, name, user, **ctx):
    ctx["request"] = request
    ctx["user"] = user
    ctx["site"] = {"site_name": get_setting("site_name", settings.SITE_NAME)}
    ctx["labels"] = PLATFORM_LABELS
    ctx["badges"] = STATUS_BADGES
    return templates.TemplateResponse(request, name, ctx)


def _admin_or_redirect(request, user):
    if user is None:
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
    if not user.get("is_admin"):
        return RedirectResponse("/?err=admin_required", status_code=303)
    return None


def _proof_view(proof):
    order = orders_svc.get_order_by_id(proof["order_id"])
    buyer = get_user(order["user_id"]) if order else None
    items = orders_svc.get_order_items(order["id"]) if order else []
    return {"proof": proof, "order": order, "buyer": buyer, "items": items}


@router.get("/admin")
async def admin_dashboard(request: Request, user=Depends(get_current_user)):
    redirect = _admin_or_redirect(request, user)
    if redirect:
        return redirect
    stats = orders_svc.stats_summary()
    recent_orders = orders_svc.list_orders(limit=10)
    for o in recent_orders:
        b = get_user(o["user_id"])
        o["buyer_name"] = (b or {}).get("full_name") or "Unknown"
        o["buyer_username"] = (b or {}).get("username")
    pending_proofs = [_proof_view(p) for p in payments_svc.list_proofs(status="pending", limit=10)]
    audit = get_audit_log(limit=10)
    low_stock = []
    for p in products_svc.list_products():
        if inventory_svc.available_count(p["id"]) == 0:
            low_stock.append(p)
    return render(
        request, "admin/dashboard.html", user,
        stats=stats, recent_orders=recent_orders, pending_proofs=pending_proofs,
        audit=audit, low_stock=low_stock,
    )


@router.get("/admin/products")
async def admin_products(request: Request, user=Depends(get_current_user)):
    redirect = _admin_or_redirect(request, user)
    if redirect:
        return redirect
    products = products_svc.list_products(include_inactive=True)
    counts = inventory_svc.counts_by_product()
    for p in products:
        p["counts"] = counts.get(p["id"], {"available": 0, "assigned": 0, "sold": 0})
    return render(request, "admin/products.html", user, products=products)


@router.post("/admin/products")
async def admin_product_add(
    request: Request,
    platform: str = Form(...),
    title: str = Form(...),
    description: str = Form(""),
    price_bdt: float = Form(...),
    user=Depends(get_current_user),
):
    redirect = _admin_or_redirect(request, user)
    if redirect:
        return redirect
    try:
        products_svc.add_product(platform, title, description, price_bdt, admin_id=user["telegram_id"])
    except ValueError as e:
        return RedirectResponse(f"/admin/products?err={e}", status_code=303)
    return RedirectResponse("/admin/products?ok=added", status_code=303)


@router.post("/admin/products/{pid}/edit")
async def admin_product_edit(
    request: Request,
    pid: int,
    platform: str = Form(...),
    title: str = Form(...),
    description: str = Form(""),
    price_bdt: float = Form(...),
    user=Depends(get_current_user),
):
    redirect = _admin_or_redirect(request, user)
    if redirect:
        return redirect
    products_svc.update_product(pid, platform=platform, title=title, description=description, price_bdt=price_bdt, admin_id=user["telegram_id"])
    return RedirectResponse("/admin/products?ok=updated", status_code=303)


@router.post("/admin/products/{pid}/toggle")
async def admin_product_toggle(request: Request, pid: int, user=Depends(get_current_user)):
    redirect = _admin_or_redirect(request, user)
    if redirect:
        return redirect
    products_svc.toggle_product(pid, admin_id=user["telegram_id"])
    return RedirectResponse("/admin/products?ok=toggled", status_code=303)


@router.post("/admin/products/{pid}/delete")
async def admin_product_delete(request: Request, pid: int, user=Depends(get_current_user)):
    redirect = _admin_or_redirect(request, user)
    if redirect:
        return redirect
    try:
        products_svc.delete_product(pid, admin_id=user["telegram_id"])
    except ValueError as e:
        return RedirectResponse(f"/admin/products?err={e}", status_code=303)
    return RedirectResponse("/admin/products?ok=deleted", status_code=303)


@router.get("/admin/inventory")
async def admin_inventory(request: Request, product_id: int = 0, user=Depends(get_current_user)):
    redirect = _admin_or_redirect(request, user)
    if redirect:
        return redirect
    products = products_svc.list_products(include_inactive=True)
    if product_id == 0 and products:
        product_id = products[0]["id"]
    product = products_svc.get_product(product_id) if product_id else None
    items = inventory_svc.list_for_product(product_id) if product else []
    counts = inventory_svc.counts_by_product().get(product_id, {"available": 0, "assigned": 0, "sold": 0}) if product else {}
    return render(request, "admin/inventory.html", user, products=products, product=product, items=items, counts=counts)


@router.post("/admin/inventory/add")
async def admin_inventory_add(
    request: Request,
    product_id: int = Form(...),
    bulk_text: str = Form(""),
    user=Depends(get_current_user),
):
    redirect = _admin_or_redirect(request, user)
    if redirect:
        return redirect
    added, errors = inventory_svc.bulk_add(product_id, bulk_text)
    log_admin_action(user["telegram_id"], "inventory_add", "product", product_id, f"Added {added} units via website")
    return RedirectResponse(f"/admin/inventory?product_id={product_id}&added={added}", status_code=303)


@router.post("/admin/inventory/zip-add")
async def admin_inventory_zip_add(
    request: Request,
    product_id: int = Form(...),
    zip_file: UploadFile = File(...),
    user=Depends(get_current_user),
):
    redirect = _admin_or_redirect(request, user)
    if redirect:
        return redirect
    import zipfile, io

    product = products_svc.get_product(product_id)
    if product is None:
        return RedirectResponse(f"/admin/inventory?product_id={product_id}&err=product_not_found", status_code=303)
    is_cookie = product["platform"] == "cookie"

    data = await zip_file.read()
    added = 0
    errors = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for name in zf.namelist():
                if name.endswith("/"):
                    continue
                content = zf.read(name).decode("utf-8", errors="replace").strip()
                if not content:
                    continue
                if is_cookie:
                    # Each .txt file = one cookie account; cookie_data = full file content
                    try:
                        inventory_svc.add_item(
                            product_id,
                            account_username=name.split("/")[-1] or "Cookie",
                            account_password="",
                            cookie_data=content,
                        )
                        added += 1
                    except Exception as e:
                        errors.append(f"{name}: {e}")
                else:
                    added_batch, batch_errors = inventory_svc.bulk_add(product_id, content)
                    added += added_batch
                    errors.extend(batch_errors)
    except Exception as e:
        errors.append(f"Zip read error: {e}")
    log_admin_action(user["telegram_id"], "inventory_zip_add", "product", product_id, f"Added {added} units via zip")
    return RedirectResponse(f"/admin/inventory?product_id={product_id}&added={added}", status_code=303)


@router.post("/admin/inventory/{iid}/delete")
async def admin_inventory_delete(request: Request, iid: int, product_id: int = 0, user=Depends(get_current_user)):
    redirect = _admin_or_redirect(request, user)
    if redirect:
        return redirect
    inventory_svc.delete_item(iid)
    return RedirectResponse(f"/admin/inventory?product_id={product_id}", status_code=303)


@router.get("/admin/orders")
async def admin_orders(request: Request, status: str = "", user=Depends(get_current_user)):
    redirect = _admin_or_redirect(request, user)
    if redirect:
        return redirect
    orders = orders_svc.list_orders(status=status or None, limit=200)
    for o in orders:
        b = get_user(o["user_id"])
        o["buyer_name"] = (b or {}).get("full_name") or "Unknown"
        o["buyer_username"] = (b or {}).get("username")
    return render(request, "admin/orders.html", user, orders=orders, status=status)


@router.get("/admin/orders/{code}")
async def admin_order_detail(request: Request, code: str, user=Depends(get_current_user)):
    redirect = _admin_or_redirect(request, user)
    if redirect:
        return redirect
    order = orders_svc.get_order_with_items(code)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    buyer = get_user(order["user_id"])
    proof = payments_svc.get_proof_by_order(order["id"])
    credentials = inventory_svc.get_assigned_inventory(order["id"])
    public_id, private_id = get_channel_ids()
    public_link = channel_post_link(public_id, order.get("public_msg_id"))
    private_link = None
    if proof and proof.get("private_msg_id"):
        private_link = channel_post_link(private_id, proof["private_msg_id"])
    return render(
        request, "admin/order_detail.html", user,
        order=order, buyer=buyer, proof=proof, credentials=credentials,
        public_link=public_link, private_link=private_link,
    )


@router.get("/admin/payments")
async def admin_payments(request: Request, user=Depends(get_current_user)):
    redirect = _admin_or_redirect(request, user)
    if redirect:
        return redirect
    pending = [_proof_view(p) for p in payments_svc.list_proofs(status="pending")]
    history = [v for v in (_proof_view(p) for p in payments_svc.list_proofs(limit=200)) if v["proof"]["status"] != "pending"]
    return render(request, "admin/payments.html", user, pending=pending, history=history)


@router.post("/api/admin/payments/{order_id}/decide")
async def api_admin_decide(
    request: Request,
    order_id: int,
    decision: str = Form(...),
    note: str = Form(""),
    user=Depends(get_admin_user),
):
    result = await payments_svc.decide(order_id, decision, user["telegram_id"], note=note, bot=tg_bot.get_bot())
    if result.get("reason") == "already_processed":
        return RedirectResponse("/admin/payments?err=already", status_code=303)
    return RedirectResponse("/admin/payments?ok=1", status_code=303)


@router.get("/api/admin/payments/pending")
async def api_admin_pending_payments(user=Depends(get_admin_user)):
    out = []
    for v in (_proof_view(p) for p in payments_svc.list_proofs(status="pending")):
        proof, order, buyer = v["proof"], v["order"], v["buyer"]
        out.append(
            {
                "proof_id": proof["id"],
                "order_id": order["id"] if order else None,
                "order_code": order["order_code"] if order else None,
                "amount": order["total_bdt"] if order else None,
                "method": proof["method"],
                "transaction_id": proof["transaction_id"],
                "sender_number": proof.get("sender_number"),
                "buyer_name": (buyer or {}).get("full_name"),
                "buyer_username": (buyer or {}).get("username"),
                "buyer_id": (buyer or {}).get("telegram_id"),
                "photo_url": f"/admin/proofs/{proof['id']}/photo" if proof.get("photo_path") or proof.get("photo_file_id") else None,
                "created_at": proof["created_at"],
            }
        )
    return {"pending": out}


@router.get("/api/admin/stats")
async def api_admin_stats(user=Depends(get_admin_user)):
    return orders_svc.stats_summary()


@router.get("/admin/settings")
async def admin_settings(request: Request, user=Depends(get_current_user)):
    redirect = _admin_or_redirect(request, user)
    if redirect:
        return redirect
    keys = ["site_name", "bkash_number", "nagad_number", "payment_instructions", "admin_ids", "public_channel_id", "private_channel_id", "expiry_minutes"]
    current = {k: get_setting(k, "") for k in keys}
    return render(request, "admin/settings.html", user, settings=current)


@router.post("/admin/settings")
async def admin_settings_save(
    request: Request,
    site_name: str = Form(""),
    bkash_number: str = Form(""),
    nagad_number: str = Form(""),
    payment_instructions: str = Form(""),
    admin_ids: str = Form(""),
    public_channel_id: str = Form(""),
    private_channel_id: str = Form(""),
    expiry_minutes: str = Form("30"),
    user=Depends(get_current_user),
):
    redirect = _admin_or_redirect(request, user)
    if redirect:
        return redirect
    set_setting("site_name", site_name.strip() or settings.SITE_NAME)
    set_setting("bkash_number", bkash_number.strip())
    set_setting("nagad_number", nagad_number.strip())
    set_setting("payment_instructions", payment_instructions.strip())
    set_setting("public_channel_id", public_channel_id.strip())
    set_setting("private_channel_id", private_channel_id.strip())
    try:
        expiry = int(expiry_minutes)
        if expiry < 1:
            expiry = 30
    except ValueError:
        expiry = 30
    set_setting("expiry_minutes", expiry)
    old_admins = get_setting("admin_ids", "")
    new_admins = ",".join(p.strip() for p in admin_ids.replace(",", " ").split() if p.strip().isdigit())
    set_setting("admin_ids", new_admins)
    if new_admins != old_admins:
        conn_ids = [int(x) for x in new_admins.split(",") if x] if new_admins else []
        conn = get_connection()
        conn.execute("UPDATE users SET is_admin = 0")
        for i in conn_ids:
            conn.execute("UPDATE users SET is_admin = 1 WHERE telegram_id = ?", (i,))
        conn.commit()
        conn.close()
    log_admin_action(user["telegram_id"], "settings_change", "settings", "site", "Settings updated via website")
    return RedirectResponse("/admin/settings?ok=1", status_code=303)


@router.get("/admin/audit")
async def admin_audit(request: Request, user=Depends(get_current_user)):
    redirect = _admin_or_redirect(request, user)
    if redirect:
        return redirect
    return render(request, "admin/audit.html", user, logs=get_audit_log(limit=300))


@router.get("/admin/proofs/{proof_id}/photo")
async def admin_proof_photo(proof_id: int, user=Depends(get_admin_user)):
    proof = payments_svc.get_proof(proof_id)
    if proof is None:
        raise HTTPException(status_code=404)
    if proof.get("photo_path") and os.path.exists(proof["photo_path"]):
        return FileResponse(proof["photo_path"])
    raise HTTPException(status_code=404, detail="Photo not stored locally")
