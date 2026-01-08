# src/main.py
from __future__ import annotations

import logging

from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from src.config import TELEGRAM_TOKEN
from src.db import init_db
from src.jobs.jobs import setup_jobs
from src.logging_setup import setup_logging
from src.payments import on_paidproof_callback, cmd_pay
from src.handlers.commands import cmd_start, cmd_help, cmd_today, cmd_week, cmd_profile, cmd_analyze, cmd_set_targets, \
    cmd_del, cmd_edit, cmd_analyze_today, cmd_myid, cmd_contact, cmd_cancel, cmd_goals, cmd_progress, cmd_streak, \
    cmd_notify, cmd_notify_time, cmd_notify_weekly
from src.handlers.messages import on_text, on_photo, on_new_members

try:
    from src.handlers.errors import global_error_handler
except Exception:
    global_error_handler = None


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("nutribot")


def build_app():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # 1) callbacks
    app.add_handler(CallbackQueryHandler(on_paidproof_callback, pattern=r"^paidproof:\d+$"))

    # 2) commands
    app.add_handler(CommandHandler("pay", cmd_pay))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("week", cmd_week))

    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("set_targets", cmd_set_targets))
    app.add_handler(CommandHandler("analyze_week", cmd_analyze))
    app.add_handler(CommandHandler("analyze_today", cmd_analyze_today))
    app.add_handler(CommandHandler("goals", cmd_goals))

    app.add_handler(CommandHandler("del", cmd_del))
    app.add_handler(CommandHandler("edit", cmd_edit))

    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("contact", cmd_contact))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    app.add_handler(CommandHandler("progress", cmd_progress))
    app.add_handler(CommandHandler("streak", cmd_streak))

    app.add_handler(CommandHandler("notify", cmd_notify))
    app.add_handler(CommandHandler("notify_time", cmd_notify_time))
    app.add_handler(CommandHandler("notify_weekly", cmd_notify_weekly))
    app.add_handler(CommandHandler("progress", cmd_progress))
    app.add_handler(CommandHandler("streak", cmd_streak))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # 3) messages
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))

    # 4) global error handler
    if global_error_handler:
        app.add_error_handler(global_error_handler)

    return app


def main() -> None:
    init_db()
    setup_logging()
    app = build_app()
    logger.info("Bot started")
    setup_jobs(app)
    app.run_polling(allowed_updates=None)


if __name__ == "__main__":
    main()
