import asyncio
import os
import socket
import uuid

from telegram import BotCommand
from telegram.error import BadRequest, Conflict, NetworkError, TimedOut
from telegram.ext import ApplicationBuilder

from app.config import settings
from app.database import claim_bot_poller, release_bot_poller, renew_bot_poller

application = None

POLLER_TTL = 45
POLLER_TICK = 10
poller_owner = f"{socket.gethostname()[:24]}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
_poller_task = None


def _renew_or_keep(owner):
    try:
        return renew_bot_poller(owner)
    except Exception as e:
        print(f"[bot] could not renew the bot lease ({e}) — continuing to poll")
        return True


async def _supervise_polling(app):
    """Hold the single getUpdates lease; a second replica stays website-only."""
    polling = False
    waiting_logged = False
    while True:
        try:
            if not polling:
                try:
                    claimed = claim_bot_poller(poller_owner, POLLER_TTL)
                except Exception as e:
                    # A database hiccup must not leave the shop bot deaf.
                    print(f"[bot] could not read the bot lease ({e}) — polling anyway")
                    claimed = True
                if claimed:
                    await app.updater.start_polling(drop_pending_updates=True)
                    polling = True
                    waiting_logged = False
                    print(f"[bot] long-polling started (lease {poller_owner})")
                elif not waiting_logged:
                    print("[bot] another instance holds the bot lease — website-only, will retry")
                    waiting_logged = True
            elif not _renew_or_keep(poller_owner):
                print("[bot] bot lease taken over by another instance — stopping polling")
                polling = False
                try:
                    await app.updater.stop()
                except Exception:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[bot] polling supervisor error: {e}")
            polling = False
        await asyncio.sleep(POLLER_TICK)


async def _on_error(update, context):
    err = context.error
    if isinstance(err, Conflict):
        print(
            "[bot] CONFLICT: another process polls this token outside the lease "
            "(another host/service sharing the token, or the same token with its own "
            "database). Keep exactly one bot host, or point both at the same DATABASE_URL."
        )
        return
    if isinstance(err, BadRequest) and "query is too old" in str(err).lower():
        # A stale button tap replayed after a Conflict restart: harmless, don't alert anyone.
        print(f"[bot] stale callback query ignored: {err}")
        return
    print(f"[bot] handler error: {err!r}")
    if isinstance(err, (BadRequest, NetworkError, TimedOut)):
        return
    try:
        message = update.effective_message if update else None
        if message is not None:
            await message.reply_text(
                "⚠️ Something went wrong on our side. Please try again, or send /start to reset."
            )
    except Exception:
        pass


def create_application():
    app = ApplicationBuilder().token(settings.BOT_TOKEN).build()
    from app.telegram import handlers_admin, handlers_channel, handlers_user

    handlers_user.register(app)
    handlers_admin.register(app)
    handlers_channel.register(app)
    app.add_error_handler(_on_error)
    return app


async def startup(app):
    global _poller_task
    await app.initialize()
    await app.start()
    try:
        await app.bot.set_my_commands(
            [
                BotCommand("start", "Start"),
                BotCommand("browse", "Browse products"),
                BotCommand("orders", "My orders and status"),
                BotCommand("proof", "Submit payment proof"),
                BotCommand("help", "How to order"),
                BotCommand("cancel", "Cancel the current step"),
                BotCommand("admin", "Admin menu (admins only)"),
            ]
        )
    except Exception as e:
        print(f"[bot] set_my_commands failed: {e}")
    if settings.USE_POLLING:
        if app.updater:
            _poller_task = asyncio.create_task(_supervise_polling(app))
            print("[bot] running in long-polling mode")
    else:
        try:
            await app.bot.set_webhook(f"{settings.BASE_URL}/tg/webhook/{settings.WEBHOOK_SECRET}")
            print(f"[bot] webhook set: {settings.BASE_URL}/tg/webhook/{settings.WEBHOOK_SECRET}")
        except Exception as e:
            print(f"[bot] set_webhook failed: {e}")


async def shutdown(app):
    global _poller_task
    if _poller_task is not None:
        _poller_task.cancel()
        try:
            await _poller_task
        except (asyncio.CancelledError, Exception):
            pass
        _poller_task = None
    try:
        if settings.USE_POLLING and app.updater:
            if app.updater.running:
                await app.updater.stop()
        else:
            await app.bot.delete_webhook()
    except Exception:
        pass
    if settings.USE_POLLING:
        try:
            release_bot_poller(poller_owner)
        except Exception as e:
            print(f"[bot] lease release failed: {e}")
    await app.stop()
    await app.shutdown()


def get_bot():
    return application.bot if application is not None else None
