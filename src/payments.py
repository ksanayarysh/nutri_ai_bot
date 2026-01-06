# nutribot/payments.py
from __future__ import annotations

from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from src.config import ADMIN_IDS, PAYMENT_INSTRUCTIONS
from src.db import db, now_iso


# =====================
# DB helpers
# =====================

# src/payments.py (DB helpers for Postgres)

def create_payment_request(user_id: int) -> int:
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO payment_requests (user_id, status, created_at)
            VALUES (%s, 'pending', %s)
            RETURNING id
            """,
            (user_id, now_iso()),
        )
        return int(cur.fetchone()[0])


def get_pending_payment_request(user_id: int):
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, user_id, status, created_at, proof_file_id, proof_text
            FROM payment_requests
            WHERE user_id = %s
              AND status = 'pending'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (user_id,),
        )
        return cur.fetchone()  # tuple или None


def attach_payment_proof(
    request_id: int,
    *,
    proof_file_id: str | None = None,
    proof_text: str | None = None,
) -> None:
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE payment_requests
            SET proof_file_id = COALESCE(%s, proof_file_id),
                proof_text    = COALESCE(%s, proof_text)
            WHERE id = %s
            """,
            (proof_file_id, proof_text, request_id),
        )


def mark_paid(request_id: int) -> None:
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE payment_requests
            SET status = 'paid'
            WHERE id = %s
            """,
            (request_id,),
        )

# =====================
# UI helpers
# =====================

def pay_keyboard(request_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Я оплатил(а)",
                    callback_data=f"paidproof:{request_id}",
                )
            ]
        ]
    )


# =====================
# Handlers (Telegram)
# =====================

async def cmd_pay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return

    pending = get_pending_payment_request(user.id)
    if pending:
        request_id = pending["id"]
    else:
        request_id = create_payment_request(user.id)

    text = (
        f"{PAYMENT_INSTRUCTIONS}\n\n"
        f"ID заявки: `{request_id}`"
    )

    await update.message.reply_text(
        text,
        reply_markup=pay_keyboard(request_id),
        parse_mode="Markdown",
    )


async def on_paidproof_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    if not data.startswith("paidproof:"):
        return

    request_id = int(data.split(":", 1)[1])
    user_id = query.from_user.id

    await query.message.reply_text(
        "Отлично. Пришли, пожалуйста, чек:\n"
        "- фото / скрин\n"
        "- или текст (номер транзакции, комментарий)\n\n"
        f"ID заявки: {request_id}"
    )

    context.user_data["pending_payment_request_id"] = request_id


async def notify_admins_about_payment(
    context: ContextTypes.DEFAULT_TYPE,
    request_id: int,
    user_id: int,
) -> None:
    text = (
        "💳 Новый платёж\n\n"
        f"ID заявки: {request_id}\n"
        f"User ID: {user_id}\n\n"
        "Для подтверждения:\n"
        "/paid <request_id> <days|forever>"
    )

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, text)
        except Exception:
            pass
