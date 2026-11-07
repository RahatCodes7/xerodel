from telegram import BotCommand
from telegram.ext import ApplicationBuilder

from app.config import settings

application = None


async def _on_error(update, context):
    print(f"[bot] handler error: {context.error!r}")
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
            await app.updater.start_polling(drop_pending_updates=True)
        print("[bot] running in long-polling mode")
    else:
        try:
            await app.bot.set_webhook(f"{settings.BASE_URL}/tg/webhook/{settings.WEBHOOK_SECRET}")
            print(f"[bot] webhook set: {settings.BASE_URL}/tg/webhook/{settings.WEBHOOK_SECRET}")
        except Exception as e:
            print(f"[bot] set_webhook failed: {e}")


async def shutdown(app):
    try:
        if settings.USE_POLLING and app.updater:
            await app.updater.stop()
        else:
            await app.bot.delete_webhook()
    except Exception:
        pass
    await app.stop()
    await app.shutdown()


def get_bot():
    return application.bot if application is not None else None
