# nutribot/payments.py
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import psycopg2.extras
import httpx
try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.ext import ContextTypes
except Exception:  # pragma: no cover
    # Webhooks service doesn't need Telegram deps
    InlineKeyboardButton = InlineKeyboardMarkup = Update = object  # type: ignore
    ContextTypes = object  # type: ignore

from src.config import PAYMENT_INSTRUCTIONS
from src.db import db, now_iso
from src.subscriptions import grant_subscription, is_subscribed

# =====================
# Notes / DB expectation
# =====================
# This module implements the "new logic":
# - Bot does NOT collect "payment proof" screenshots/text.
# - Bot creates a payment with a provider (YooKassa for RU cards, Mercado Pago for Pix),
#   sends a payment link / Pix QR to the user.
# - Provider calls your webhook -> we mark payment as paid -> we grant subscription.
#
# Expected table (example):
#
# CREATE TABLE IF NOT EXISTS payments (
#   id BIGSERIAL PRIMARY KEY,
#   user_id BIGINT NOT NULL,
#   provider TEXT NOT NULL CHECK (provider IN ('yookassa','mercadopago')),
#   provider_payment_id TEXT NOT NULL,
#   status TEXT NOT NULL DEFAULT 'pending', -- pending|paid|canceled|failed
#   amount NUMERIC(10,2) NOT NULL,
#   currency TEXT NOT NULL,
#   plan_code TEXT NOT NULL,               -- 'monthly'|'lifetime'|...
#   pay_url TEXT,
#   meta JSONB,
#   created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
#   updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
#   paid_at TIMESTPTZ
# );
# CREATE UNIQUE INDEX IF NOT EXISTS ux_payments_provider_pid ON payments(provider, provider_payment_id);
# CREATE INDEX IF NOT EXISTS ix_payments_user_id ON payments(user_id);

# =====================
# Provider adapters
# =====================
# Keep these imports "soft" so the module doesn't crash in dev if you haven't wired providers yet.

def _create_yookassa_payment(*, amount: float, currency: str, description: str) -> Dict[str, Any]:
    """
    Must return:
      { "id": "<provider_payment_id>", "pay_url": "<confirmation_url>" }
    """
    try:
        from src.providers.yookassa import create_payment as create_payment_impl  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError("YooKassa provider is not wired: src/providers/yookassa.py") from e

    data = create_payment_impl(amount=amount, currency=currency, description=description)
    pid = data.get("id")
    url = data.get("confirmation_url") or data.get("pay_url")
    if not pid or not url:
        raise RuntimeError(f"Bad YooKassa response: {data}")
    return {"id": str(pid), "pay_url": str(url), "raw": data}


def _create_mercadopago_pix(*, amount: float, description: str, external_reference: str | None = None) -> Dict[str, Any]:
    """Create a Pix payment in Mercado Pago.

    Returns dict with keys:
      - id (provider_payment_id)
      - ticket_url (optional)
      - qr_code (optional)
      - qr_code_base64 (optional)
      - raw (full provider response)

    Notes:
    - Mercado Pago requires a payer.email. We use env MP_PAYER_EMAIL or a harmless placeholder.
    """
    token = os.getenv("MP_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("MP_ACCESS_TOKEN is not set")

    payer_email = os.getenv("MP_PAYER_EMAIL", "payer@example.com")

    payload: Dict[str, Any] = {
        "transaction_amount": float(amount),
        "description": description,
        "payment_method_id": "pix",
        "payer": {"email": payer_email},
    }
    if external_reference:
        payload["external_reference"] = external_reference

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    r = httpx.post("https://api.mercadopago.com/v1/payments", json=payload, headers=headers, timeout=20.0)
    r.raise_for_status()
    data = r.json()

    pid = data.get("id")
    if not pid:
        raise RuntimeError(f"Bad MercadoPago response: {data}")

    tx = (data.get("point_of_interaction") or {}).get("transaction_data") or {}
    return {
        "id": str(pid),
        "ticket_url": tx.get("ticket_url"),
        "qr_code": tx.get("qr_code"),
        "qr_code_base64": tx.get("qr_code_base64"),
        "raw": data,
    }
def _mercadopago_get_status(provider_payment_id: str) -> str:
    """Pull current payment status from Mercado Pago API."""
    token = os.getenv("MP_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("MP_ACCESS_TOKEN is not set")

    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://api.mercadopago.com/v1/payments/{provider_payment_id}"
    r = httpx.get(url, headers=headers, timeout=20.0)
    # If the id is fake (like 123456 from simulator), MP returns 404. Treat as pending.
    if r.status_code == 404:
        return "not_found"
    r.raise_for_status()
    data = r.json()
    return str(data.get("status") or "")
def create_payment(
    *,
    user_id: int,
    provider: str,
    provider_payment_id: str,
    amount: float,
    currency: str,
    plan_code: str,
    pay_url: str | None = None,
    meta: Dict[str, Any] | None = None,
) -> int:
    """Insert a payment row and return internal id."""
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO payments (user_id, provider, provider_payment_id, status, amount, currency, plan_code, pay_url, meta, created_at, updated_at)
            VALUES (%s, %s, %s, 'pending', %s, %s, %s, %s, %s, NOW(), NOW())
            RETURNING id
            """,
            (user_id, provider, provider_payment_id, float(amount), currency, plan_code, pay_url, json.dumps(meta or {})),
        )
        rid = int(cur.fetchone()[0])
        conn.commit()
        return rid


def get_payment_by_provider_id(provider: str, provider_payment_id: str) -> Optional[Dict[str, Any]]:
    with db() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT * FROM payments WHERE provider=%s AND provider_payment_id=%s",
            (provider, provider_payment_id),
        )
        return cur.fetchone()


def get_latest_pending_payment(user_id: int) -> Optional[Dict[str, Any]]:
    with db() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT *
            FROM payments
            WHERE user_id=%s AND status='pending'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (user_id,),
        )
        return cur.fetchone()


def update_payment_status(provider: str, provider_payment_id: str, status: str, *, paid_at: bool = False) -> None:
    with db() as conn:
        cur = conn.cursor()
        if paid_at:
            cur.execute(
                """
                UPDATE payments
                SET status=%s, updated_at=NOW(), paid_at=COALESCE(paid_at, NOW())
                WHERE provider=%s AND provider_payment_id=%s
                """,
                (status, provider, provider_payment_id),
            )
        else:
            cur.execute(
                """
                UPDATE payments
                SET status=%s, updated_at=NOW()
                WHERE provider=%s AND provider_payment_id=%s
                """,
                (status, provider, provider_payment_id),
            )
        conn.commit()


def _grant_by_plan(user_id: int, plan_code: str) -> None:
    # Keep it simple for MVP. Expand if you add more plans.
    if plan_code == "lifetime":
        grant_subscription(user_id, days=None, plan="lifetime")
    else:
        # default monthly
        grant_subscription(user_id, days=30, plan=plan_code or "monthly")


def mark_payment_paid(provider: str, provider_payment_id: str) -> Optional[int]:
    """Mark payment paid and grant subscription. Returns user_id if found."""
    payment = get_payment_by_provider_id(provider, provider_payment_id)
    if not payment:
        return None

    # idempotent: if already paid, just return
    if payment.get("status") == "paid":
        return int(payment["user_id"])

    update_payment_status(provider, provider_payment_id, "paid", paid_at=True)
    _grant_by_plan(int(payment["user_id"]), str(payment.get("plan_code") or "monthly"))
    return int(payment["user_id"])


# =====================
# Webhook entrypoints (call these from your FastAPI/Flask app)
# =====================

def handle_yookassa_webhook(payload: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Expected payload shape: { "event": "...", "object": { "id": "..." } }
    Returns (ok, message).
    """
    event = payload.get("event")
    obj = payload.get("object") or {}
    pid = obj.get("id")
    if not event or not pid:
        return False, "bad payload"

    if event == "payment.succeeded":
        mark_payment_paid("yookassa", str(pid))
        return True, "paid"

    # optional: reflect other statuses
    if event in ("payment.canceled", "payment.waiting_for_capture"):
        update_payment_status("yookassa", str(pid), "canceled" if event == "payment.canceled" else "pending")
    return True, f"ignored:{event}"


def handle_mercadopago_webhook(payload: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Typical MercadoPago webhook: { "type": "payment", "data": { "id": "123" } }
    We fetch payment status from MP API to be sure.
    Returns (ok, message).
    """
    typ = payload.get("type")
    data = payload.get("data") or {}
    pid = data.get("id")
    if not typ or not pid:
        return False, "bad payload"

    if typ != "payment":
        return True, f"ignored:{typ}"

    status = _mercadopago_get_status(str(pid))
    # MP uses "approved" for paid
    if status == "approved":
        mark_payment_paid("mercadopago", str(pid))
        return True, "paid"

    if status in ("rejected", "cancelled", "canceled"):
        update_payment_status("mercadopago", str(pid), "failed")
    return True, f"status:{status}"


# =====================
# UI helpers (Telegram)
# =====================

def pay_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🇷🇺 Карта РФ (ЮKassa)", callback_data="pay:yk")],
            [InlineKeyboardButton("🇧🇷 Pix (Mercado Pago)", callback_data="pay:mp")],
            [InlineKeyboardButton("Проверить оплату", callback_data="pay:check")],
        ]
    )


# =====================
# Handlers (Telegram)
# =====================

# You can tune these in config later.
PLAN_CODE = "monthly"
PRICE_RUB = 499.00
PRICE_BRL = 49.90


async def cmd_pay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return

    if is_subscribed(user.id):
        await update.message.reply_text("Доступ уже активен ✅")
        return

    pending = get_latest_pending_payment(user.id)
    if pending:
        # If there is a pending payment, show the menu anyway (user might want another method)
        text = (
            f"{PAYMENT_INSTRUCTIONS}\n\n"
            f"У тебя уже есть ожидающий платёж (ID {pending['id']}).\n"
            "Если ты уже оплатил(а), нажми «Проверить оплату»."
        )
    else:
        text = PAYMENT_INSTRUCTIONS

    await update.message.reply_text(text, reply_markup=pay_keyboard())


async def on_pay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    user_id = query.from_user.id
    data = query.data or ""

    if is_subscribed(user_id):
        await query.message.reply_text("Доступ уже активен ✅")
        return

    if data == "pay:check":
        pending = get_latest_pending_payment(user_id)
        if not pending:
            await query.message.reply_text("Не нашла ожидающих платежей. Нажми /pay и создай оплату заново.")
            return

        provider = str(pending["provider"])
        provider_pid = str(pending["provider_payment_id"])

        # For MP we can pull status; for YooKassa we usually rely on webhooks.
        if provider == "mercadopago":
            try:
                status = _mercadopago_get_status(provider_pid)
            except Exception as e:
                await query.message.reply_text(f"Не смогла проверить статус Pix: {type(e).__name__}")
                return
            if status == "approved":
                mark_payment_paid("mercadopago", provider_pid)
                await query.message.reply_text("Оплата подтверждена ✅ Доступ активирован.")
            else:
                await query.message.reply_text(f"Статус оплаты: {status}. Если оплатил(а), подожди пару минут и проверь снова.")
        else:
            # YooKassa: webhook-based; still show stored pay_url to re-open
            url = pending.get("pay_url")
            if url:
                await query.message.reply_text(
                    f"Платёж ещё не подтверждён.\n"
                    f"Ссылка на оплату:\n{url}\n\n"
                    "Если ты уже оплатил(а), подожди немного: подтверждение придёт автоматически."
                )
            else:
                await query.message.reply_text("Платёж ещё не подтверждён. Если оплатил(а), подожди немного: подтверждение придёт автоматически.")
        return

    if data == "pay:yk":
        try:
            p = _create_yookassa_payment(
                amount=PRICE_RUB,
                currency="RUB",
                description="Course subscription: monthly",
            )
        except Exception as e:
            await query.message.reply_text(f"ЮKassa не настроена: {type(e).__name__}: {str(e)[:150]}")
            return

        provider_pid = p["id"]
        pay_url = p["pay_url"]
        internal_id = create_payment(
            user_id=user_id,
            provider="yookassa",
            provider_payment_id=provider_pid,
            amount=PRICE_RUB,
            currency="RUB",
            plan_code=PLAN_CODE,
            pay_url=pay_url,
            meta={"raw": p.get("raw", {})},
        )

        await query.message.reply_text(
            f"Оплата ЮKassa создана ✅\n"
            f"ID: {internal_id}\n"
            f"Ссылка:\n{pay_url}\n\n"
            "После оплаты доступ включится автоматически."
        )
        return

    if data == "pay:mp":
        try:
            p = _create_mercadopago_pix(amount=PRICE_BRL, description="Course subscription: monthly (Pix)")
        except Exception as e:
            await query.message.reply_text(f"Mercado Pago не настроен: {type(e).__name__}: {str(e)[:150]}")
            return

        provider_pid = p["id"]
        raw = p.get("raw", {})
        ticket_url = (raw.get("ticket_url") or (raw.get("point_of_interaction") or {}).get("transaction_data", {}).get("ticket_url"))
        qr_code = (raw.get("qr_code") or (raw.get("point_of_interaction") or {}).get("transaction_data", {}).get("qr_code"))

        internal_id = create_payment(
            user_id=user_id,
            provider="mercadopago",
            provider_payment_id=provider_pid,
            amount=PRICE_BRL,
            currency="BRL",
            plan_code=PLAN_CODE,
            pay_url=ticket_url,
            meta={"raw": raw},
        )

        if ticket_url:
            await query.message.reply_text(
                f"Pix создан ✅\n"
                f"ID: {internal_id}\n"
                f"Ссылка:\n{ticket_url}\n\n"
                "После оплаты доступ включится автоматически."
            )
        elif qr_code:
            await query.message.reply_text(
                f"Pix создан ✅\n"
                f"ID: {internal_id}\n"
                f"QR (копируй и плати в банке):\n{qr_code}\n\n"
                "После оплаты доступ включится автоматически."
            )
        else:
            await query.message.reply_text(
                f"Pix создан ✅ (ID {internal_id}), но не получила ссылку/QR из ответа провайдера.\n"
                "Это бывает, если аккаунт/метод Pix не донастроен."
            )
        return
