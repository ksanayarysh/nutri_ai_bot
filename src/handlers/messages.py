# nutribot/handlers/messages.py
from __future__ import annotations

from src.ai import ai_estimate
from datetime import datetime, timezone
from src.config import MEAL_ALIASES
from src.food_structure.food import Macros
import json
import re
from typing import Optional, Tuple

from telegram import Update
from telegram.ext import ContextTypes

from src.db import db, now_iso, today_str
from src.aliases import apply_aliases_to_text
from src.profile import build_profile_hint
from src.portions import suggest_portion

from src.payments import attach_payment_proof, get_pending_payment_request,notify_admins_about_payment

ADMIN_USER_ID=452738438

try:
    from nutribot.ai import estimate_from_text, estimate_from_photo  # type: ignore
except Exception:  # pragma: no cover
    estimate_from_text = None
    estimate_from_photo = None


def fmt(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x:.1f}"


def net_carbs(carbs: float | None, fiber: float | None) -> float:
    return max(0.0, float(carbs or 0.0) - float(fiber or 0.0))


def unit_to_ru(unit: str | None) -> str:
    return {
        "g": "г",
        "kg": "кг",
        "ml": "мл",
        "l": "л",
        "pcs": "шт",
        "tsp": "ч.л.",
        "tbsp": "ст.л.",
    }.get(unit, unit or "")


def meal_to_ru(meal: str) -> str:
    return {
        "breakfast": "завтрак",
        "lunch": "обед",
        "dinner": "ужин",
        "snack": "перекус",
        "other": "перекус",
    }.get(meal, meal)

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

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

_MEAL_MAP = {
    # canonical
    "breakfast": "breakfast",
    "lunch": "lunch",
    "dinner": "dinner",
    "snack": "snack",
    "other": "other",

    # RU (и твои “закуска:”)
    "завтрак": "breakfast",
    "обед": "lunch",
    "ужин": "dinner",
    "перекус": "snack",
    "закуска": "snack",
    "другое": "other",
    "прочее": "other",
}

def _f(x, default: float | None = None) -> float | None:
    if x is None:
        return default
    try:
        return float(x)
    except Exception:
        return default

def insert_entry(
    user_id: int,
    day: str,
    meal_type: str,
    text: str,
    macros: Optional["Macros"],
) -> None:
    """
    Пишет ОДНУ запись в entries:
    - meal = canonical (breakfast/lunch/dinner/snack/other)
    - raw_text = оригинальный текст
    - если macros есть: item_name/qty/unit + макросы
    - если macros нет: макросы NULL (и это нормально)
    """
    meal = _normalize_meal(meal_type)
    raw_text = (text or "").strip()

    item_name = None
    qty = None
    unit = None
    calories = None
    protein = None
    fat = None
    carbs = None
    fiber = None

    if macros is not None:
        item_name = (getattr(macros, "name", None) or "").strip() or None
        qty = _f(getattr(macros, "qty", None), 1.0)  # если qty не пришёл, пусть будет 1
        unit = (getattr(macros, "unit", None) or "serving").strip()

        calories = _f(getattr(macros, "calories", None), 0.0)
        protein = _f(getattr(macros, "protein", None), 0.0)
        fat     = _f(getattr(macros, "fat", None), 0.0)
        carbs   = _f(getattr(macros, "carbs", None), 0.0)
        fiber   = _f(getattr(macros, "fiber", None), 0.0)

    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO entries
              (user_id, entry_date, meal, raw_text,
               item_name, qty, unit,
               calories, protein, fat, carbs, fiber,
               created_at)
            VALUES
              (%s, %s, %s, %s,
               %s, %s, %s,
               %s, %s, %s, %s, %s,
               %s)
            """,
            (
                user_id, day, meal, raw_text,
                item_name, qty, unit,
                calories, protein, fat, carbs, fiber,
                _now_iso(),  # или now_iso(), если есть
            ),
        )
        conn.commit()

def get_day_totals(user_id: int, day: str) -> Macros:
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
              COALESCE(SUM(calories), 0),
              COALESCE(SUM(protein),  0),
              COALESCE(SUM(fat),      0),
              COALESCE(SUM(carbs),    0),
              COALESCE(SUM(fiber),    0)
            FROM entries
            WHERE user_id = %s AND entry_date = %s
            """,
            (user_id, day),
        )
        calories, protein, fat, carbs, fiber = cur.fetchone()

    return Macros(
        "", 0, "",
        calories=float(calories),
        protein=float(protein),
        fat=float(fat),
        carbs=float(carbs),
        fiber=float(fiber),
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

from typing import Optional

async def _log_food_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return

    meal_type, body = parse_meal_and_body(text)
    day = today_str()

    try:
        replaced_text, alias_notes = apply_aliases_to_text(user.id, text)
        profile_hint = build_profile_hint({"user_id": user.id})

        items, confidence, meta = ai_estimate(
            text=replaced_text,
            meal_hint=meal_type,
            profile_hint=profile_hint,
        )
    except Exception as e:
        await msg.reply_text(f"ai сломался: {type(e).__name__}: {str(e)[:200]}")
        return

    meta = meta or {"assumptions": []}
    meta.setdefault("assumptions", [])
    if alias_notes:
        meta["assumptions"].extend(alias_notes)

    if not items:
        await msg.reply_text("Не смогла распознать еду в сообщении. Напиши чуть подробнее.")
        return

    # сохраняем КАЖДЫЙ item отдельной строкой (это то, что нужно для /today списка)
    await _save_items_and_reply(
        update=update,
        uid=user.id,
        day=day,
        meal=_normalize_meal(meal_type),
        raw_text=body,
        items=items,
        confidence=confidence,
        meta=meta,
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
                fiber=float(est.get("net_carbs", est.get("carbs", 0))),
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
    # в самом начале on_text
    if context.user_data.get("contact_mode"):
        text = update.effective_message.text or ""
        await context.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=(
                "📩 Сообщение от пользователя\n\n"
                f"ID: {update.effective_user.id}\n"
                f"Username: @{update.effective_user.username}\n"
                f"Имя: {update.effective_user.first_name}\n\n"
                f"Текст:\n{text}"
            )
        )
        context.user_data["contact_mode"] = False
        await update.effective_message.reply_text("✅ Сообщение отправлено администратору.")
        return

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


# -------------------------
# qty parsing helpers
# -------------------------

QTY_RE = re.compile(
    r"(?P<qty>\d+(?:[.,]\d+)?)\s*(?P<unit>г|гр|g|kg|кг|мл|ml|л|l|шт|pcs|tsp|tbsp)\b",
    re.IGNORECASE,
)

def parse_qty_unit(text: str) -> Optional[Tuple[float, str]]:
    t = text.strip().lower()
    if t in {"стандарт", "standard"}:
        return None  # special handled outside
    m = QTY_RE.search(t)
    if not m:
        return None
    qty = float(m.group("qty").replace(",", "."))
    unit = m.group("unit").lower()
    # normalize units
    unit_map = {
        "гр": "g", "г": "g", "g": "g",
        "кг": "kg", "kg": "kg",
        "мл": "ml", "ml": "ml",
        "л": "l", "l": "l",
        "шт": "pcs", "pcs": "pcs",
        "tsp": "tsp", "tbsp": "tbsp",
    }
    return qty, unit_map.get(unit, unit)


def user_provided_qty(text: str) -> bool:
    return bool(QTY_RE.search(text))


# -------------------------
# IMPORTANT:
# items returned by ai_estimate are expected to have:
# it.name, it.qty, it.unit, it.calories, it.protein, it.fat, it.carbs, it.fiber
# -------------------------


async def handle_log(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    u: dict,
    meal: str,
    text: str,
) -> None:
    uid = u["user_id"]
    d = today_str()
    msg = update.effective_message
    if not msg:
        return

    # Optional: gate some features behind subscription (up to you)
    # logging itself usually should work for everyone.
    # if not is_subscribed(uid): ...

    # 1) If we were waiting for qty from previous message, handle it first
    if context.user_data.get("await_qty"):
        awaited = context.user_data["await_qty"]

        # only treat THIS message as qty answer (user typically replies right after prompt)
        parsed = parse_qty_unit(text)
        if text.strip().lower() in {"стандарт", "standard"}:
            parsed = (awaited["suggest_qty"], awaited["suggest_unit"])

        if not parsed:
            await msg.reply_text("Не вижу количество. Пример: `100 г` или `1 шт`.", parse_mode="Markdown")
            return

        qty, unit = parsed

        # continue the flow using saved context
        meal = awaited["meal"]
        original_text = awaited["text"]
        replaced_text = awaited.get("replaced_text") or original_text
        profile_hint = awaited.get("profile_hint") or {}
        confidence = awaited.get("confidence")
        meta = awaited.get("meta") or {"assumptions": []}

        # re-run AI estimate to get items (or you can just patch single item if you store it)
        items, confidence2, meta2 = ai_estimate(text=replaced_text, meal_hint=meal, profile_hint=profile_hint)
        if meta2:
            meta = meta2
        if confidence2 is not None:
            confidence = confidence2

        if not items:
            context.user_data.pop("await_qty", None)
            await msg.reply_text("Не смогла восстановить запись. Попробуй ещё раз описать продукт.")
            return

        # patch qty/unit for the single item we asked about
        # we only ask qty when len(items)==1, so:
        items[0].qty = qty
        items[0].unit = unit

        # clear await state
        context.user_data.pop("await_qty", None)

        # proceed to save
        await _save_items_and_reply(
            update=update,
            uid=uid,
            day=d,
            meal=meal,
            raw_text=original_text,
            items=items,
            confidence=confidence,
            meta=meta,
        )
        return

    # 2) Normal flow
    replaced_text, alias_notes = apply_aliases_to_text(uid, text)
    profile_hint = build_profile_hint(u)

    try:
        items, confidence, meta = ai_estimate(text=replaced_text, meal_hint=meal, profile_hint=profile_hint)
    except Exception as e:
        await msg.reply_text(f"ai сломался: {type(e).__name__}: {str(e)[:200]}")
        return

    meta = meta or {"assumptions": []}
    meta.setdefault("assumptions", [])

    # If user didn't provide qty and this is a single item -> ask for qty
    if not user_provided_qty(text) and items and len(items) == 1:
        it = items[0]
        suggestion = suggest_portion(it.name)  # expected: (qty, unit) or None

        if suggestion:
            s_qty, s_unit = suggestion
            await msg.reply_text(
                f"Ты указала продукт без количества: {it.name}.\n"
                f"Какое количество учитывать?\n\n"
                f"Напиши число (например: 100 г)\n"
                f"или ответь «стандарт» — я возьму {s_qty} {unit_to_ru(s_unit)}."
            )
        else:
            s_qty, s_unit = 0.0, "g"
            await msg.reply_text(
                f"Ты указала продукт без количества: {it.name}.\n"
                f"Напиши количество, например: 100 г или 1 шт."
            )

        # save await context
        context.user_data["await_qty"] = {
            "meal": meal,
            "text": text,
            "item_name": it.name,
            "suggest_qty": float(s_qty),
            "suggest_unit": s_unit,
            "replaced_text": replaced_text,
            "profile_hint": profile_hint,
            "confidence": confidence,
            "meta": meta,
        }
        return

    if alias_notes:
        meta["assumptions"].extend(alias_notes)
        meta["notes"] = (meta.get("notes") or "") + (" | aliases applied" if meta.get("notes") else "aliases applied")

    if not items:
        await msg.reply_text("Я не знаю эту команду. Чтобы увидеть возможности, набери /help")
        return

    await _save_items_and_reply(
        update=update,
        uid=uid,
        day=d,
        meal=meal,
        raw_text=text,
        items=items,
        confidence=confidence,
        meta=meta,
    )


async def _save_items_and_reply(
    update: Update,
    uid: int,
    day: str,
    meal: str,
    raw_text: str,
    items: list,
    confidence: Optional[float],
    meta: dict,
) -> None:
    msg = update.effective_message
    if not msg:
        return

    created_at = now_iso()
    meta_json = json.dumps(meta or {}, ensure_ascii=False)

    with db() as conn:
        cur = conn.cursor()
        for it in items:
            cur.execute(
                """
                INSERT INTO entries(
                    user_id, entry_date, meal, raw_text,
                    item_name, qty, unit,
                    calories, protein, fat, carbs, fiber,
                    confidence, meta_json, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    uid, day, meal, raw_text,
                    it.name, it.qty, it.unit,
                    it.calories, it.protein, it.fat, it.carbs, it.fiber,
                    confidence, meta_json, created_at
                ),
            )

    lines = [f"добавлено ({meal_to_ru(meal)}):"]
    for it in items:
        fib = float(it.fiber or 0.0)
        net = net_carbs(it.carbs, fib)
        lines.append(
            f"- {it.name} ({it.qty} {unit_to_ru(it.unit)}): {fmt(it.calories)} ккал, "
            f"белки {fmt(it.protein)} г, жиры {fmt(it.fat)} г, углеводы {fmt(it.carbs)} г "
            f"(клетч. {fmt(fib)} г, чистые {fmt(net)} г)"
        )

    await msg.reply_text("\n".join(lines))


async def contact_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.message
    if not user or not msg:
        return

    if not context.user_data.get("contact_mode"):
        return  # это не контактный режим

    text = msg.text or ""

    # пересылаем тебе
    await context.bot.send_message(
        chat_id=ADMIN_USER_ID,
        text=(
            "📩 Сообщение от пользователя\n\n"
            f"ID: {user.id}\n"
            f"Username: @{user.username}\n"
            f"Имя: {user.first_name}\n\n"
            f"Текст:\n{text}"
        )
    )

    context.user_data["contact_mode"] = False

    await msg.reply_text("✅ Сообщение отправлено администратору.")
