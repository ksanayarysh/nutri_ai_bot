# nutribot/handlers/messages.py
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple

from telegram import Update
from telegram.ext import ContextTypes

from src.config import MEAL_ALIASES
from src.db import db, now_iso, today_str
from src.payments import attach_payment_proof, get_pending_payment_request,notify_admins_about_payment

try:
    from nutribot.ai import estimate_from_text, estimate_from_photo  # type: ignore
except Exception:  # pragma: no cover
    estimate_from_text = None
    estimate_from_photo = None


# -------------------------
# Parsing helpers
# -------------------------

MEAL_PREFIX_RE = re.compile(
    r"^\s*(?P<meal>[A-Za-z_]+|[А-Яа-яЁё]+)\s*[:\-]\s*(?P<body>.+)\s*$"
)

TXID_RE = re.compile(r"\b([A-Za-z0-9]{10,}|\d{10,})\b")


def _is_command(text: str) -> bool:
    return text.strip().startswith("/")


def _normalize_meal(meal_raw: str) -> str:
    m = meal_raw.strip().lower()
    return MEAL_ALIASES.get(m, m)


def parse_meal_and_body(text: str) -> Tuple[str, str]:
    """
    Accepts:
      - "завтрак: яйца и сыр"
      - "lunch - chicken salad"
      - "яйца и сыр" -> other
    """
    m = MEAL_PREFIX_RE.match(text)
    if not m:
        return "other", text.strip()

    meal = _normalize_meal(m.group("meal"))
    body = m.group("body").strip()
    if not body:
        body = text.strip()
    return meal, body


# -------------------------
# DB writes (MVP)
# -------------------------

@dataclass
class Macros:
    calories: float
    protein: float
    fat: float
    carbs: float
    net_carbs: float


def insert_entry(
    user_id: int,
    day: str,
    meal_type: str,
    text: str,
    macros: Optional[Macros],
) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO entries
              (user_id, day, meal_type, text, calories, protein, fat, carbs, net_carbs, created_at)
            VALUES
              (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                day,
                meal_type,
                text,
                None if macros is None else macros.calories,
                None if macros is None else macros.protein,
                None if macros is None else macros.fat,
                None if macros is None else macros.carbs,
                None if macros is None else macros.net_carbs,
                now_iso(),
            ),
        )


def get_day_totals(user_id: int, day: str) -> Macros:
    with db() as conn:
        row = conn.execute(
            """
            SELECT
              COALESCE(SUM(calories), 0) AS calories,
              COALESCE(SUM(protein),  0) AS protein,
              COALESCE(SUM(fat),      0) AS fat,
              COALESCE(SUM(carbs),    0) AS carbs,
              COALESCE(SUM(fiber),0) AS net_carbs
            FROM entries
            WHERE user_id = ? AND day = ?
            """,
            (user_id, day),
        ).fetchone()

    return Macros(
        calories=float(row["calories"]),
        protein=float(row["protein"]),
        fat=float(row["fat"]),
        carbs=float(row["carbs"]),
        net_carbs=float(row["net_carbs"]),
    )


# -------------------------
# Payment proof routing
# -------------------------

def _get_pending_request_id(context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    rid = context.user_data.get("pending_payment_request_id")
    if isinstance(rid, int):
        return rid
    return None


async def _handle_possible_payment_proof_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> bool:
    """
    Returns True if message was treated as payment proof.
    """
    user = update.effective_user
    msg = update.message
    if not user or not msg:
        return False

    # If user clicked "I paid" callback earlier, we have the request_id cached.
    rid = _get_pending_request_id(context)

    # If not cached, but there's a pending request, we can still accept proof.
    if rid is None:
        pending = get_pending_payment_request(user.id)
        rid = int(pending["id"]) if pending else None

    if rid is None:
        return False

    # Heuristic: either user writes explicitly about payment or includes txid-like token.
    t = text.strip().lower()
    looks_like_payment = any(k in t for k in ("оплат", "pix", "чек", "квит", "payment", "paid", "transfer"))
    has_txid = TXID_RE.search(text) is not None

    if not (looks_like_payment or has_txid):
        return False

    attach_payment_proof(rid, proof_text=text.strip())
    await notify_admins_about_payment(context, rid, user.id)

    # clear cached request id (so normal food logging continues)
    context.user_data.pop("pending_payment_request_id", None)

    await msg.reply_text(
        f"Принято ✅\n"
        f"Я отправила подтверждение админам.\n"
        f"ID заявки: {rid}"
    )
    return True


async def _handle_payment_proof_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """
    Returns True if photo was treated as payment proof.
    """
    user = update.effective_user
    msg = update.message
    if not user or not msg or not msg.photo:
        return False

    rid = _get_pending_request_id(context)
    if rid is None:
        pending = get_pending_payment_request(user.id)
        rid = int(pending["id"]) if pending else None
    if rid is None:
        return False

    file_id = msg.photo[-1].file_id
    attach_payment_proof(rid, proof_file_id=file_id)
    await notify_admins_about_payment(context, rid, user.id)

    context.user_data.pop("pending_payment_request_id", None)

    await msg.reply_text(
        f"Чек получен ✅\n"
        f"Я отправила админам на проверку.\n"
        f"ID заявки: {rid}"
    )
    return True


# -------------------------
# Food logging routing (MVP)
# -------------------------

async def _log_food_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    user = update.effective_user
    msg = update.message
    if not user or not msg:
        return

    meal_type, body = parse_meal_and_body(text)
    day = today_str()

    macros: Optional[Macros] = None
    if estimate_from_text:
        try:
            est = await estimate_from_text(body)  # expected dict-like
            macros = Macros(
                calories=float(est.get("calories", 0)),
                protein=float(est.get("protein", 0)),
                fat=float(est.get("fat", 0)),
                carbs=float(est.get("carbs", 0)),
                net_carbs=float(est.get("net_carbs", est.get("carbs", 0))),
            )
        except Exception:
            macros = None

    insert_entry(user.id, day, meal_type, body, macros)

    totals = get_day_totals(user.id, day)
    if macros:
        await msg.reply_text(
            "Записано ✅\n"
            f"{meal_type}: {body}\n\n"
            f"Оценка: {macros.calories:.0f} ккал | "
            f"Б {macros.protein:.1f} / Ж {macros.fat:.1f} / У {macros.carbs:.1f} (net {macros.net_carbs:.1f})\n\n"
            f"Сегодня всего: {totals.calories:.0f} ккал | "
            f"Б {totals.protein:.1f} / Ж {totals.fat:.1f} / У {totals.carbs:.1f} (net {totals.net_carbs:.1f})"
        )
    else:
        await msg.reply_text(
            "Записано ✅\n"
            f"{meal_type}: {body}\n\n"
            "Я пока записала без КБЖУ (AI-модуль не подключён или не смог оценить)."
        )


async def _log_food_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.message
    if not user or not msg or not msg.photo:
        return

    day = today_str()
    photo = msg.photo[-1]

    macros: Optional[Macros] = None
    description = "food photo"

    if estimate_from_photo:
        try:
            est = await estimate_from_photo(context.bot, photo.file_id)  # expected dict-like
            description = str(est.get("title") or est.get("description") or "food")
            macros = Macros(
                calories=float(est.get("calories", 0)),
                protein=float(est.get("protein", 0)),
                fat=float(est.get("fat", 0)),
                carbs=float(est.get("carbs", 0)),
                net_carbs=float(est.get("net_carbs", est.get("carbs", 0))),
            )
        except Exception:
            macros = None

    insert_entry(user.id, day, "other", f"[photo] {description}", macros)

    totals = get_day_totals(user.id, day)
    if macros:
        await msg.reply_text(
            "Фото записано ✅\n"
            f"Оценка: {macros.calories:.0f} ккал | "
            f"Б {macros.protein:.1f} / Ж {macros.fat:.1f} / У {macros.carbs:.1f} (net {macros.net_carbs:.1f})\n\n"
            f"Сегодня всего: {totals.calories:.0f} ккал | "
            f"Б {totals.protein:.1f} / Ж {totals.fat:.1f} / У {totals.carbs:.1f} (net {totals.net_carbs:.1f})"
        )
    else:
        await msg.reply_text(
            "Фото записано ✅\n"
            "Пока без КБЖУ (AI-модуль не подключён или не смог оценить)."
        )


# -------------------------
# Public handlers
# -------------------------

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not msg.text:
        return

    text = msg.text

    # Ignore commands; commands live in handlers/commands.py
    if _is_command(text):
        return

    # Payment proof flow (text)
    handled = await _handle_possible_payment_proof_text(update, context, text)
    if handled:
        return

    # Otherwise treat as food log
    await _log_food_text(update, context, text)


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not msg.photo:
        return

    # Payment proof flow (photo)
    handled = await _handle_payment_proof_photo(update, context)
    if handled:
        return

    # Otherwise treat as food photo
    await _log_food_photo(update, context)


async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Optional: welcome message when bot is added to a group.
    """
    msg = update.message
    if not msg or not msg.new_chat_members:
        return

    # Don't spam; simple acknowledgement.
    await msg.reply_text(
        "Привет! Я тут, чтобы вести дневник питания и считать КБЖУ.\n"
        "Пиши что съела (например: 'завтрак: яйца и сыр') или пришли фото еды."
    )
