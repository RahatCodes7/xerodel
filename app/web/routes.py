import os
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.database import get_setting, upsert_user
from app.services import (
    inventory as inventory_svc,
    orders as orders_svc,
    payments as payments_svc,
    products as products_svc,
)
from app.services.channels import PLATFORM_LABELS, STATUS_BADGES, fmt_money
from app.telegram import bot as tg_bot
from app.web.auth import COOKIE_NAME, SESSION_MAX_AGE, get_current_user, make_session, verify_telegram_auth

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def public_settings():
    return {
        "site_name": get_setting("site_name", settings.SITE_NAME),
        "bkash_number": get_setting("bkash_number", ""),
        "nagad_number": get_setting("nagad_number", ""),
        "payment_instructions": get_setting("payment_instructions", ""),
        "public_channel_id": get_setting("public_channel_id", ""),
    }


def render(request, name, user=None, **ctx):
    ctx["request"] = request
    ctx["user"] = user
    ctx["site"] = public_settings()
    ctx["labels"] = PLATFORM_LABELS
    ctx["badges"] = STATUS_BADGES
    return templates.TemplateResponse(request, name, ctx)


def _with_stock(products):
    counts = inventory_svc.counts_by_product()
    for p in products:
        c = counts.get(p["id"], {})
        p["stock"] = c.get("available", 0)
        p["sold"] = c.get("sold", 0)
    return products


@router.get("/")
async def index(request: Request, user=Depends(get_current_user)):
    products = _with_stock(products_svc.list_products())
    return render(request, "index.html", user, products=products[:6])


@router.get("/shop")
async def shop(request: Request, platform: str = "", user=Depends(get_current_user)):
    products = _with_stock(products_svc.list_products())
    platform_filter = platform if platform in products_svc.PLATFORMS else ""
    if platform_filter:
        products = [p for p in products if p["platform"] == platform_filter]
    return render(request, "shop.html", user, products=products, platform_filter=platform_filter)


@router.get("/product/{product_id}")
async def product_detail(request: Request, product_id: int, user=Depends(get_current_user)):
    p = products_svc.get_product(product_id)
    if p is None or not p["is_active"]:
        raise HTTPException(status_code=404, detail="Product not found")
    stock = inventory_svc.available_count(product_id)
    return render(request, "product.html", user, product=p, stock=stock)


@router.get("/login")
async def login_page(request: Request, user=Depends(get_current_user)):
    if user is not None:
        return RedirectResponse("/", status_code=303)
    nxt = request.query_params.get("next", "/")
    return render(request, "login.html", user, bot_username=settings.BOT_USERNAME, next_url=nxt)


@router.post("/api/auth/telegram")
async def auth_telegram(request: Request):
    data = await request.json()
    if not verify_telegram_auth(data, settings.BOT_TOKEN):
        return JSONResponse({"ok": False, "error": "verification failed"}, status_code=401)
    tg_id = int(data["id"])
    full_name = data.get("first_name") or ""
    if data.get("last_name"):
        full_name = f"{full_name} {data['last_name']}".strip()
    upsert_user(tg_id, full_name, data.get("username"), data.get("photo_url"))
    token = make_session(tg_id, full_name)
    resp = JSONResponse({"ok": True, "next": data.get("next") or "/"})
    secure = settings.BASE_URL.startswith("https")
    resp.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax", max_age=SESSION_MAX_AGE, secure=secure)
    return resp


@router.get("/logout")
async def logout(request: Request):
    resp = RedirectResponse("/", status_code=303)
    secure = settings.BASE_URL.startswith("https")
    resp.set_cookie(COOKIE_NAME, "", max_age=0, expires=0, httponly=True, samesite="lax", secure=secure, path="/")
    return resp


@router.get("/checkout")
async def checkout_get(request: Request, product_id: int = 0, qty: int = 1, err: str = "", user=Depends(get_current_user)):
    if user is None:
        return RedirectResponse(f"/login?next=/checkout?product_id={product_id}&qty={qty}", status_code=303)
    p = products_svc.get_product(product_id)
    if p is None or not p["is_active"]:
        raise HTTPException(status_code=404, detail="Product not found")
    qty = max(1, min(qty, orders_svc.MAX_QTY_PER_ITEM))
    stock = inventory_svc.available_count(product_id)
    return render(request, "checkout.html", user, product=p, qty=qty, stock=stock, err=err)


@router.post("/checkout")
async def checkout_post(request: Request, product_id: int = Form(...), qty: int = Form(...), user=Depends(get_current_user)):
    if user is None:
        return RedirectResponse("/login?next=/checkout", status_code=303)
    try:
        order = await orders_svc.place_order(user["telegram_id"], [{"product_id": product_id, "qty": qty}], bot=tg_bot.get_bot())
    except orders_svc.OutOfStockError:
        return RedirectResponse(f"/product/{product_id}?err=out_of_stock", status_code=303)
    except ValueError as e:
        return RedirectResponse(f"/checkout?product_id={product_id}&qty={qty}&err={e}", status_code=303)
    return RedirectResponse(f"/order/{order['order_code']}?new=1", status_code=303)


@router.get("/orders")
async def my_orders(request: Request, user=Depends(get_current_user)):
    if user is None:
        return RedirectResponse("/login?next=/orders", status_code=303)
    orders = orders_svc.get_user_orders(user["telegram_id"])
    return render(request, "orders.html", user, orders=orders)


@router.get("/order/{code}")
async def order_detail(request: Request, code: str, user=Depends(get_current_user)):
    if user is None:
        return RedirectResponse(f"/login?next=/order/{code}", status_code=303)
    order = orders_svc.get_order_with_items(code)
    if order is None or (order["user_id"] != user["telegram_id"] and not user["is_admin"]):
        raise HTTPException(status_code=404, detail="Order not found")
    proof = payments_svc.get_proof_by_order(order["id"])
    credentials = []
    if order["status"] == "delivered":
        titles = {it["product_id"]: it["product_title"] for it in order["items"]}
        for i in inventory_svc.get_assigned_inventory(order["id"]):
            credentials.append(
                {
                    "product_title": titles.get(i["product_id"], ""),
                    "username": i["account_username"],
                    "password": i["account_password"],
                    "cookie": i["cookie_data"],
                    "inventory_id": i["id"],
                }
            )
    return render(request, "order_detail.html", user, order=order, proof=proof, credentials=credentials)


@router.get("/order/{code}/cookie/{inventory_id}")
async def download_cookie(code: str, inventory_id: int, user=Depends(get_current_user)):
    """Hand over the cookie as a downloadable .txt file."""
    if user is None:
        return RedirectResponse(f"/login?next=/order/{code}", status_code=303)
    order = orders_svc.get_order_by_code(code)
    if order is None or (order["user_id"] != user["telegram_id"] and not user["is_admin"]):
        raise HTTPException(status_code=404, detail="Order not found")
    if order["status"] != "delivered":
        raise HTTPException(status_code=404, detail="Cookie not available yet")
    rows = inventory_svc.get_assigned_inventory(order["id"])
    cookies = [r for r in rows if (r.get("cookie_data") or "").strip()]
    row = next((r for r in cookies if r["id"] == inventory_id), None)
    if row is None:
        raise HTTPException(status_code=404, detail="Cookie not found")
    index = cookies.index(row) + 1
    filename = f"cookie-{order['order_code']}-{index}.txt"
    return Response(
        content=(row["cookie_data"].strip() + "\n").encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/order/{code}/proof")
async def upload_proof(
    request: Request,
    code: str,
    method: str = Form(...),
    transaction_id: str = Form(...),
    sender_number: str = Form(""),
    photo: UploadFile = File(...),
    user=Depends(get_current_user),
):
    if user is None:
        return RedirectResponse("/login?next=/order/" + code, status_code=303)
    order = orders_svc.get_order_by_code(code)
    if order is None or order["user_id"] != user["telegram_id"]:
        raise HTTPException(status_code=404, detail="Order not found")
    if order["status"] not in ("pending_payment", "awaiting_proof"):
        return RedirectResponse(f"/order/{code}?err=bad_status", status_code=303)
    if not transaction_id.strip():
        return RedirectResponse(f"/order/{code}?err=trxid", status_code=303)
    dest = None
    if photo is not None and photo.filename:
        ext = os.path.splitext(photo.filename)[1].lower() or ".jpg"
        if ext not in (".jpg", ".jpeg", ".png", ".webp"):
            ext = ".jpg"
        dest = os.path.join(settings.PROOF_DIR, f"{uuid.uuid4().hex}{ext}")
        with open(dest, "wb") as f:
            f.write(await photo.read())
    result = await payments_svc.submit_proof(
        order["id"], user["telegram_id"], method, transaction_id,
        sender_number=sender_number or None, photo_path=dest, bot=tg_bot.get_bot(),
    )
    if result.get("ok"):
        return RedirectResponse(f"/order/{code}?ok=proof", status_code=303)
    return RedirectResponse(f"/order/{code}?err={result.get('error', 'failed')}", status_code=303)


@router.get("/api/orders/{code}")
async def api_order(code: str, user=Depends(get_current_user)):
    if user is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    order = orders_svc.get_order_with_items(code)
    if order is None or (order["user_id"] != user["telegram_id"] and not user["is_admin"]):
        return JSONResponse({"error": "not_found"}, status_code=404)
    proof = payments_svc.get_proof_by_order(order["id"])
    credentials = None
    if order["status"] == "delivered":
        inv = inventory_svc.get_assigned_inventory(order["id"])
        titles = {it["product_id"]: it["product_title"] for it in order["items"]}
        credentials = [
            {
                "product_title": titles.get(i["product_id"], ""),
                "username": i["account_username"],
                "password": i["account_password"],
                "cookie": bool((i["cookie_data"] or "").strip()),
                "cookie_url": f"/order/{code}/cookie/{i['id']}",
            }
            for i in inv
        ]
    return {
        "order_code": order["order_code"],
        "status": order["status"],
        "status_badge": STATUS_BADGES.get(order["status"], order["status"]),
        "total_bdt": order["total_bdt"],
        "payment_method": order["payment_method"],
        "expires_at": order["expires_at"],
        "items": order["items"],
        "proof_status": proof["status"] if proof else None,
        "credentials": credentials,
        "can_proof": order["status"] in ("pending_payment", "awaiting_proof"),
    }


@router.get("/api/orders/{code}/proof/photo")
async def api_proof_photo(code: str, user=Depends(get_current_user)):
    if user is None:
        raise HTTPException(status_code=401)
    order = orders_svc.get_order_by_code(code)
    if order is None or (order["user_id"] != user["telegram_id"] and not user["is_admin"]):
        raise HTTPException(status_code=404)
    proof = payments_svc.get_proof_by_order(order["id"])
    if proof is None or not proof.get("photo_path") or not os.path.exists(proof["photo_path"]):
        raise HTTPException(status_code=404)
    return FileResponse(proof["photo_path"])


@router.get("/healthz")
async def healthz():
    return {"ok": True}
