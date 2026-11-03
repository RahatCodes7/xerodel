import re

from telegram.ext import CallbackQueryHandler, ContextTypes

from app.database import get_admin_ids
from app.services import payments as payments_svc


async def proof_callback(update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in get_admin_ids():
        await q.answer("⛔ Admins only", show_alert=True)
        return
    m = re.match(r"^pf_(acc|rej)_(\d+)$", q.data)
    if not m:
        await q.answer("Unknown action", show_alert=True)
        return
    decision = "accepted" if m.group(1) == "acc" else "rejected"
    order_id = int(m.group(2))
    await q.answer("Processing...")
    result = await payments_svc.decide(order_id, decision, q.from_user.id, bot=context.bot)
    if result.get("reason") == "already_processed":
        await q.answer("Already processed by another admin", show_alert=True)
    elif result.get("ok"):
        await q.answer(f"Order {result.get('order_code')}: {decision}")
    else:
        await q.answer(f"Error: {result.get('error', 'failed')}", show_alert=True)


def register(app):
    app.add_handler(CallbackQueryHandler(proof_callback, pattern=r"^pf_(acc|rej)_\d+$"))
