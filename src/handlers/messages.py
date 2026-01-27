# nutribot/handlers/messages.py
from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Optional, Tuple

from telegram import Update
from telegram.ext import ContextTypes

from src.ai import ai_estimate, ai_estimate_photo
from src.aliases import apply_aliases_to_text
from src.config import MEAL_ALIASES
from src.db import db, today_str
from src.food_structure.food import Macros
from src.i18n.lang import get_user_language
from src.i18n.t import t
from src.profile import build_profile_hint

# Admin chat for "contact me" forwarding
ADMIN_USER_ID = 452738438


# -------------------------
# Small helpers
# -------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fmt(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{float(x):.1f}"


def net_carbs(carbs: float | None, fiber: float | None) -> float:
    return max(0.0, float(carbs or 0.0) - float(fiber or 0.0))


# Fallbacks for when translations are missing (t() will then return key)
_UNIT_FALLBACK = {
    "ru": {
        "pcs": "шт",
        "g": "г",
        "ml": "мл",
        "tbsp": "ст.л.",
        "tsp": "ч.л.",
        "serving": "порц.",
        "kg": "кг",
        "l": "л",
    },
    "pt": {
        "pcs": "un",
        "g": "g",
        "ml": "ml",
        "tbsp": "col. sopa",
        "tsp": "col. chá",
        "serving": "porção",
        "kg": "kg",
        "l": "l",
    },
}

_MEAL_FALLBACK = {
    "ru": {
        "breakfast": "завтрак",
        "lunch": "обед",
        "dinner": "ужин",
        "snack": "перекус",
        "other": "другое",
    },
    "pt": {
        "breakfast": "café da manhã",
        "lunch": "almoço",
        "dinner": "jantar",
        "snack": "lanche",
        "other": "outro",
    },
}


def unit_label(lang: str, unit: str) -> str:
    unit = (unit or "").strip() or "serving"
    val = t(f"unit.{unit}", lang)
    if val != f"unit.{unit}":
        return val
    return (_UNIT_FALLBACK.get(lang) or _UNIT_FALLBACK["ru"]).get(unit, unit)


def meal_label(lang: str, meal: str) -> str:
    meal = (meal or "other").lower().strip()
    val = t(f"meal.{meal}", lang)
    if val != f"meal.{meal}":
        return val
    return (_MEAL_FALLBACK.get(lang) or _MEAL_FALLBACK["ru"]).get(meal, meal)


# -------------------------
# Parsing helpers
# -------------------------

MEAL_PREFIX_RE = re.compile(
    r"^\s*(?P<meal>[A-Za-z_]+|[А-Яа-яЁё]+)\s*[:\-]\s*(?P<body>.+)\s*$"
)


def _is_command(text: str) -> bool:
    return text.strip().startswith("/")


def _normalize_meal(meal_raw: str) -> str:
    m = (meal_raw or "").strip().lower()
    return MEAL_ALIASES.get(m, m)


def parse_meal_and_body(text: str) -> Tuple[str, str]:
    """Accepts:
      - "завтрак: яйца и сыр"
      - "lunch - chicken salad"
      - "яйца и сыр" -> other
    """
    m = MEAL_PREFIX_RE.match(text)
    if not m:
        return "other", text.strip()

    meal = _normalize_meal(m.group("meal"))
    body = (m.group("body") or "").strip()
    if not body:
        body = text.strip()
    return meal, body


# -------------------------
# DB helpers
# -------------------------

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
    macros: Optional[Macros],
) -> None:
    """Writes ONE entry into entries.
    If macros is None -> save only raw_text and keep macros NULL (valid).
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
        qty = _f(getattr(macros, "qty", None), 1.0)
        unit = (getattr(macros, "unit", None) or "serving").strip()

        calories = _f(getattr(macros, "calories", None), 0.0)
        protein = _f(getattr(macros, "protein", None), 0.0)
        fat = _f(getattr(macros, "fat", None), 0.0)
        carbs = _f(getattr(macros, "carbs", None), 0.0)
        fiber = _f(getattr(macros, "fiber", None), 0.0)

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
                user_id,
                day,
                meal,
                raw_text,
                item_name,
                qty,
                unit,
                calories,
                protein,
                fat,
                carbs,
                fiber,
                _now_iso(),
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
# qty parsing helpers
# -------------------------

QTY_RE = re.compile(
    r"(?P<qty>\d+(?:[.,]\d+)?)\s*(?P<unit>г|гр|g|kg|кг|мл|ml|л|l|шт|pcs|tsp|tbsp)\b",
    re.IGNORECASE,
)


def parse_qty_unit(text: str) -> Optional[Tuple[float, str]]:
    tt = text.strip().lower()
    if tt in {"стандарт", "standard"}:
        return None
    m = QTY_RE.search(tt)
    if not m:
        return None
    qty = float(m.group("qty").replace(",", "."))
    unit = m.group("unit").lower()
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
# Core save + reply
# -------------------------

async def _save_items_and_reply(
    update: Update,
    uid: int,
    day: str,
    meal: str,
    raw_text: str,
    items: list,
    confidence: float | None = None,
    meta: dict | None = None,
) -> None:
    """Save each item as a separate entry, then reply with a localized summary."""
    msg = update.effective_message
    if not msg:
        return

    lang = get_user_language(uid)

    # Write each item to DB
    for it in items:
        insert_entry(
            user_id=uid,
            day=day,
            meal_type=meal,
            text=raw_text,
            macros=it,
        )

    # Human-friendly reply (localized)
    lines = [t("log.added_header", lang, meal=meal_label(lang, meal))]
    for it in items:
        fib = float(getattr(it, "fiber", 0.0) or 0.0)
        net = net_carbs(getattr(it, "carbs", 0.0), fib)
        lines.append(
            t(
                "log.item_line",
                lang,
                name=getattr(it, "name", "") or "",
                qty=f"{float(getattr(it, 'qty', 1.0) or 1.0):g}",
                unit=unit_label(lang, getattr(it, "unit", "serving") or "serving"),
                kcal=fmt(getattr(it, "calories", None)),
                protein=fmt(getattr(it, "protein", None)),
                fat=fmt(getattr(it, "fat", None)),
                carbs=fmt(getattr(it, "carbs", None)),
                fiber=fmt(fib),
                net=fmt(net),
            )
        )

    await msg.reply_text("\n".join(lines))


async def save_items_and_reply(
    update: Update,
    uid: int,
    day: str,
    meal: str,
    raw_text: str,
    items: list,
    confidence: float | None = None,
    meta: dict | None = None,
) -> None:
    """Public alias for legacy/new flows."""
    await _save_items_and_reply(
        update=update,
        uid=uid,
        day=day,
        meal=meal,
        raw_text=raw_text,
        items=items,
        confidence=confidence,
        meta=meta,
    )


# -------------------------
# Food logging routing (MVP)
# -------------------------

async def _estimate_macros_from_telegram_photo(
    bot,
    file_id: str,
    meal_hint: str,
    profile_hint: dict,
) -> tuple[list, float, dict]:
    """Download Telegram photo and call ai_estimate_photo(image_bytes, ...)."""
    tg_file = await bot.get_file(file_id)
    data = await tg_file.download_as_bytearray()
    image_bytes = bytes(data)

    path = (tg_file.file_path or "").lower()
    if path.endswith(".png"):
        image_mime = "image/png"
    elif path.endswith(".webp"):
        image_mime = "image/webp"
    else:
        image_mime = "image/jpeg"

    items, confidence, meta = ai_estimate_photo(
        image_bytes=image_bytes,
        image_mime=image_mime,
        meal_hint=meal_hint,
        profile_hint=profile_hint,
    )
    return items, confidence, meta


async def _log_food_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return

    meal_type, body = parse_meal_and_body(text)
    day = today_str()

    replaced_text, alias_notes = apply_aliases_to_text(user.id, text)
    lang = get_user_language(user.id)
    profile_hint = build_profile_hint({"user_id": user.id, "language": lang})

    try:
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
        await msg.reply_text(t("log.cant_parse", lang))
        return

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

    caption = (msg.caption or "").strip()
    if caption:
        meal_type, body = parse_meal_and_body(caption)
    else:
        meal_type, body = "other", "food photo"

    lang = get_user_language(user.id)
    profile_hint = build_profile_hint({"user_id": user.id, "language": lang})

    try:
        items, confidence, meta = await _estimate_macros_from_telegram_photo(
            context.bot,
            photo.file_id,
            meal_hint=meal_type,
            profile_hint=profile_hint,
        )
    except Exception:
        insert_entry(
            user_id=user.id,
            day=day,
            meal_type=meal_type,
            text=body,
            macros=None,
        )
        await msg.reply_text(t("photo.saved_no_macros", lang))
        return

    if not items:
        insert_entry(
            user_id=user.id,
            day=day,
            meal_type=meal_type,
            text=body,
            macros=None,
        )
        await msg.reply_text(t("photo.saved_need_caption", lang))
        return

    meta = meta or {"assumptions": []}
    meta.setdefault("assumptions", [])

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


# -------------------------
# Public handlers
# -------------------------

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # contact mode: forward to admin then exit
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
            ),
        )
        context.user_data["contact_mode"] = False
        await update.effective_message.reply_text("✅ Сообщение отправлено администратору.")
        return

    msg = update.message
    if not msg or not msg.text:
        return

    text = msg.text

    # commands live in handlers/commands.py
    if _is_command(text):
        return

    await _log_food_text(update, context, text)


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not msg.photo:
        return
    await _log_food_photo(update, context)


async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not msg.new_chat_members:
        return

    user = update.effective_user
    lang = get_user_language(user.id) if user else "ru"
    await msg.reply_text(t("welcome.text", lang))


async def handle_log(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    u: dict,
    meal: str,
    text: str,
) -> None:
    """Legacy-compatible handler used by some older flows.
    Keeps the 'ask for qty' UX and writes entries one-per-item.
    """
    uid = u["user_id"]
    day = today_str()
    msg = update.effective_message
    if not msg:
        return

    lang = get_user_language(uid)

    # 1) Awaiting qty from previous prompt
    if context.user_data.get("await_qty"):
        awaited = context.user_data["await_qty"]

        parsed = parse_qty_unit(text)
        if text.strip().lower() in {"стандарт", "standard"}:
            parsed = (awaited["suggest_qty"], awaited["suggest_unit"])

        if not parsed:
            await msg.reply_text(t("log.qty_missing", lang), parse_mode="Markdown")
            return

        qty, unit = parsed

        meal = awaited["meal"]
        original_text = awaited["text"]
        replaced_text = awaited.get("replaced_text") or original_text
        profile_hint = awaited.get("profile_hint") or {}
        confidence = awaited.get("confidence")
        meta = awaited.get("meta") or {"assumptions": []}

        items, confidence2, meta2 = ai_estimate(text=replaced_text, meal_hint=meal, profile_hint=profile_hint)
        if meta2:
            meta = meta2
        if confidence2 is not None:
            confidence = confidence2

        if not items:
            context.user_data.pop("await_qty", None)
            await msg.reply_text(t("log.restore_failed", lang))
            return

        items[0].qty = qty
        items[0].unit = unit

        context.user_data.pop("await_qty", None)

        await save_items_and_reply(
            update=update,
            uid=uid,
            day=day,
            meal=meal,
            raw_text=original_text,
            items=items,
            confidence=confidence,
            meta=meta,
        )
        return

    # 2) Normal flow (legacy path)
    replaced_text, alias_notes = apply_aliases_to_text(uid, text)
    profile_hint = build_profile_hint({"user_id": uid, "language": lang})

    try:
        items, confidence, meta = ai_estimate(text=replaced_text, meal_hint=meal, profile_hint=profile_hint)
    except Exception as e:
        await msg.reply_text(f"ai сломался: {type(e).__name__}: {str(e)[:200]}")
        return

    meta = meta or {"assumptions": []}
    meta.setdefault("assumptions", [])
    if alias_notes:
        meta["assumptions"].extend(alias_notes)

    if not items:
        await msg.reply_text(t("log.cant_parse", lang))
        return

    await save_items_and_reply(
        update=update,
        uid=uid,
        day=day,
        meal=_normalize_meal(meal),
        raw_text=text,
        items=items,
        confidence=confidence,
        meta=meta,
    )
