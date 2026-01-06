# src/handlers/errors.py
from __future__ import annotations

import logging
import traceback
from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes

from src.config import ADMIN_IDS

logger = logging.getLogger("nutribot.errors")


def _safe_tb(exc: BaseException, limit: int = 6) -> str:
    """
    Short traceback for logs/admins.
    """
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__, limit=limit))


async def global_error_handler(
    update: Optional[Update],
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Global PTB error handler.
    - Logs exception
    - Notifies admins
    - Shows generic message to user (if possible)
    """
    err = context.error
    if not err:
        return

    tb = _safe_tb(err)

    logger.error("Unhandled exception: %s\n%s", err, tb)

    # Notify admins (best effort)
    text = (
        "🚨 NutriBot error\n\n"
        f"{type(err).__name__}: {err}\n\n"
        f"{tb}"
    )

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, text)
        except Exception:
            pass

    # User-facing message (generic, no internals)
    try:
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "Что-то пошло не так 😕\n"
                "Я уже отправила детали админам."
            )
    except Exception:
        pass
