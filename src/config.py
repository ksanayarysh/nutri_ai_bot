# nutribot/config.py
from __future__ import annotations

import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# =====================
# config (env + constants)
# =====================
load_dotenv()

TELEGRAM_TOKEN: str | None = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY: str | None = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL: str = os.getenv("OPENAI_MODEL") or "gpt-4.1-mini"

DATABASE_URL: str | None = os.getenv("DATABASE_URL")
DB_PATH: str = os.getenv("DB_PATH") or "nutri.db"

ADMIN_IDS: set[int] = {int(x) for x in (os.getenv("ADMIN_IDS") or "").split(",") if x.strip().isdigit()}

BOT_USERNAME: str = os.getenv("BOT_USERNAME") or "@nutri_helper_ai_bot"

CREATOR_ID: int | None = sorted(ADMIN_IDS)[0] if ADMIN_IDS else None

TRIAL_DAYS = 30

MEAL_ALIASES: dict[str, str] = {
    "завтрак": "breakfast",
    "обед": "lunch",
    "ужин": "dinner",
    "перекус": "snack",
    "другое": "other",
}

PRICE_TEXT: str = os.getenv("PRICE_TEXT") or "29 BRL / месяц"

PAYMENT_INSTRUCTIONS: str = os.getenv("PAYMENT_INSTRUCTIONS") or (
    "Оплата:\n"
    f"- Стоимость: {PRICE_TEXT}\n"
    "- Перевод/оплата по pix: ksana32@gmail.com для Бразилии или написать автору бота /contact\n"
    "- После оплаты пришли чек/скрин в этот чат и укажи id заявки."
)


try:
    TZ = ZoneInfo(os.getenv("TZ") or "America/Sao_Paulo")
except Exception:
    TZ = ZoneInfo("UTC")


if not TELEGRAM_TOKEN:
    raise RuntimeError("check TELEGRAM_TOKEN in .env")
if not OPENAI_API_KEY:
    raise RuntimeError("check OPENAI_API_KEY in .env")
