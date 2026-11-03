import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.database import init_db
from app.services import orders as orders_svc
from app.telegram import bot as tg_bot
from app.web import routes as web_routes, admin_routes as web_admin_routes

BASE_DIR = Path(__file__).resolve().parent.parent

@asynccontextmanager
async def lifespan(app):
    init_db()
    if settings.BOT_TOKEN:
        tg_bot.application = tg_bot.create_application()
        try:
            await tg_bot.startup(tg_bot.application)
        except Exception as e:
            print(f"[main] bot startup failed: {e}")
    else:
        print("[main] BOT_TOKEN not set — website-only mode")
    sweep_task = asyncio.create_task(expiry_loop())
    try:
        await orders_svc.expire_pending_orders(tg_bot.get_bot())
    except Exception as e:
        print(f"[main] startup sweep failed: {e}")
    yield
    sweep_task.cancel()
    if tg_bot.application:
        try:
            await tg_bot.shutdown(tg_bot.application)
        except Exception:
            pass

app = FastAPI(title=f"{settings.SITE_NAME} — Account Store", lifespan=lifespan)
app.include_router(web_routes.router)
app.include_router(web_admin_routes.router)
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "web" / "static"), name="static")

@app.post("/tg/webhook/{secret}")
async def tg_webhook(secret: str, request: Request):
    if secret != settings.WEBHOOK_SECRET:
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    if tg_bot.application is None:
        return JSONResponse({"ok": False, "error": "bot not initialized"}, status_code=503)
    from telegram import Update
    update = Update.de_json(await request.json(), tg_bot.application.bot)
    tg_bot.application.update_queue.put_nowait(update)
    return JSONResponse({"ok": True})

async def expiry_loop():
    while True:
        await asyncio.sleep(60)
        try:
            await orders_svc.expire_pending_orders(tg_bot.get_bot())
        except Exception as e:
            print(f"[sweep] {e}")