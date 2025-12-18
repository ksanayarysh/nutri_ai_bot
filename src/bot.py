from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import CallbackQueryHandler
import base64
import os
import sqlite3
import psycopg
from psycopg.rows import dict_row
import json
import sqlite3
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from openai import OpenAI

# =====================
# config
# =====================
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL") or "gpt-4.1-mini"
DATABASE_URL = os.getenv("DATABASE_URL")
DB_PATH="nutri.db"
ADMIN_IDS = {int(x) for x in (os.getenv("ADMIN_IDS") or "").split(",") if x.isdigit()}
BOT_USERNAME = os.getenv("BOT_USERNAME") or "@nutri_helper_ai_bot"
# кому слать сообщения "создателю"
CREATOR_ID = None
if ADMIN_IDS:
    CREATOR_ID = sorted(ADMIN_IDS)[0]  # первый админ


PRICE_TEXT = "59 BRL / месяц"   # поменяй
PAYMENT_INSTRUCTIONS = (
    "Оплата:\n"
    f"- Стоимость: {PRICE_TEXT}\n"
    "- Перевод/оплата по pix: ksana32@gmail.com\n"
    "- После оплаты пришли чек/скрин в этот чат и укажи id заявки."
)


if not TELEGRAM_TOKEN:
    raise RuntimeError("check TELEGRAM_TOKEN in .env")
if not OPENAI_API_KEY:
    raise RuntimeError("check OPENAI_API_KEY in .env")

try:
    TZ = ZoneInfo("America/Sao_Paulo")
except Exception:
    TZ = ZoneInfo("UTC")

client = OpenAI(api_key=OPENAI_API_KEY)

# =====================
# access control (admins)
# =====================

def is_admin(uid: int) -> bool:
    # если ADMIN_IDS пуст, админ-режим выключен
    if not ADMIN_IDS:
        return False
    return uid in ADMIN_IDS

def pay_keyboard(req_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("я оплатил(а)", callback_data=f"paidproof:{req_id}")],
        [InlineKeyboardButton("проверить подписку", callback_data="substatus")],
    ])

def user_provided_qty(text: str) -> bool:
    if not text:
        return False
    return bool(re.search(r"\d", text))

DEFAULT_PORTIONS = {
    "огур": (100, "g"),
    "помид": (100, "g"),
    "яйц": (1, "pcs"),
    "яблок": (1, "pcs"),
    "банан": (1, "pcs"),
    "йогурт": (150, "g"),
    "сыр": (30, "g"),
}

def suggest_portion(name: str):
    n = (name or "").lower()
    for k, v in DEFAULT_PORTIONS.items():
        if k in n:
            return v
    return None


def _count(conn, q: str, params=()) -> int:
    row = conn.execute(q, params).fetchone()
    return int(list(row)[0]) if row else 0

async def cmd_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny_if_not_admin(update):
        return

    today = today_str()
    week_ago = (datetime.now(TZ).date() - timedelta(days=7)).isoformat()
    now_iso_s = now_iso()

    with db() as conn:
        users_total = _count(conn, "select count(*) from users")
        users_7d = _count(conn, "select count(*) from users where created_at >= ?", (week_ago,))
        entries_total = _count(conn, "select count(*) from entries")
        entries_today = _count(conn, "select count(*) from entries where entry_date = ?", (today,))

        subs_total = _count(conn, "select count(*) from subscriptions")
        subs_active = _count(conn, "select count(*) from subscriptions where lower(status)='active'")
        subs_inactive = _count(conn, "select count(*) from subscriptions where lower(status)!='active'")

        # активный триал: active + expires_at not null + expires_at > now
        trials_active = _count(
            conn,
            """
            select count(*)
            from subscriptions
            where lower(status)='active'
              and expires_at is not null
              and expires_at > ?
            """,
            (now_iso_s,),
        )

    await update.effective_message.reply_text(
        "админ статистика:\n"
        f"- пользователей всего: {users_total}\n"
        f"- новых за 7 дней: {users_7d}\n"
        f"- подписок всего: {subs_total}\n"
        f"- активных подписок: {subs_active}\n"
        f"- выключенных подписок: {subs_inactive}\n"
        f"- активных триалов: {trials_active}\n"
        f"- записей еды всего: {entries_total}\n"
        f"- записей сегодня: {entries_today}\n"
    )


async def cmd_admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny_if_not_admin(update):
        return

    n = 20
    if context.args:
        try:
            n = max(1, min(200, int(context.args[0])))
        except Exception:
            n = 20

    with db() as conn:
        rows = conn.execute(
            """
            select user_id, created_at, onboarding_done
            from users
            order by created_at desc
            limit ?
            """,
            (n,),
        ).fetchall()

    if not rows:
        await update.effective_message.reply_text("пользователей пока нет.")
        return

    lines = [f"последние пользователи (top {len(rows)}):"]
    for r in rows:
        lines.append(f"- {r['user_id']} | {r['created_at'][:16]} | onboarding={int(r['onboarding_done'] or 0)}")
    await update.effective_message.reply_text("\n".join(lines))

async def cmd_admin_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny_if_not_admin(update):
        return

    if not context.args:
        await update.effective_message.reply_text("формат: /admin_user <user_id>")
        return

    try:
        target = int(context.args[0])
    except Exception:
        await update.effective_message.reply_text("user_id должен быть числом.")
        return

    week_ago = (datetime.now(TZ).date() - timedelta(days=7)).isoformat()

    with db() as conn:
        u = conn.execute("select user_id, created_at, onboarding_done from users where user_id=?", (target,)).fetchone()
        s = conn.execute("select status, expires_at, created_at, updated_at from subscriptions where user_id=?", (target,)).fetchone()
        e_total = _count(conn, "select count(*) from entries where user_id=?", (target,))
        e_7d = _count(conn, "select count(*) from entries where user_id=? and entry_date >= ?", (target, week_ago))

    if not u:
        await update.effective_message.reply_text("не нашла такого пользователя.")
        return

    sub_txt = "нет"
    if s:
        sub_txt = f"{(s['status'] or '').lower()} до {fmt_dt_short(s['expires_at'])}"

    await update.effective_message.reply_text(
        f"пользователь: {target}\n"
        f"- создан: {u['created_at'][:16]}\n"
        f"- onboarding: {int(u['onboarding_done'] or 0)}\n"
        f"- подписка: {sub_txt}\n"
        f"- записей всего: {e_total}\n"
        f"- записей за 7 дней: {e_7d}\n"
    )


async def cmd_pay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    ensure_user(uid)

    # если уже есть активная подписка, не плодим заявки
    if is_subscribed(uid):
        await update.effective_message.reply_text("У тебя уже активная подписка. Можешь пользоваться ботом.")
        return

    now = now_iso()

    with db() as conn:
        # если есть pending-заявка, используем её, чтобы не спамить
        row = conn.execute(
            """
            select id, created_at
            from payment_requests
            where user_id=? and status='pending'
            order by id desc
            limit 1
            """,
            (uid,),
        ).fetchone()

        if row:
            req_id = int(row["id"])
        else:
            kind = getattr(conn, "_db_kind", "")
            if kind == "postgres":
                row2 = conn.execute(
                    """
                    insert into payment_requests(user_id, status, plan, amount, currency, created_at, updated_at)
                    values (%s, 'pending', %s, %s, %s, %s, %s)
                    returning id
                    """,
                    (uid, "monthly", 59.0, "BRL", now, now),
                ).fetchone()
                req_id = int(row2["id"]) if row2 else 0
            else:
                cur = conn.execute(
                    """
                    insert into payment_requests(user_id, status, plan, amount, currency, created_at, updated_at)
                    values (?, 'pending', ?, ?, ?, ?, ?)
                    """,
                    (uid, "monthly", 59.0, "BRL", now, now),
                )
                req_id = int(cur.lastrowid)
                conn.commit()

    await update.effective_message.reply_text(
        "Чтобы бот работал дальше, нужна подписка.\n\n"
        f"Твоя заявка: #{req_id}\n\n"
        f"{PAYMENT_INSTRUCTIONS}\n\n"
        "После оплаты нажми кнопку ниже и пришли чек.",
        reply_markup=pay_keyboard(req_id),
    )

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    await q.answer()

    uid = q.from_user.id
    ensure_user(uid)

    data = q.data or ""

    if data == "substatus":
        # просто покажем статус тем же текстом, что /sub_status
        if is_admin(uid) and ADMIN_IDS:
            await q.message.reply_text("Статус: admin (подписка не нужна).")
            return
        sub = get_subscription(uid)
        if not sub:
            await q.message.reply_text("статус: нет подписки.")
            return
        status = (sub.get("status") or "inactive").lower()
        exp = sub.get("expires_at")
        active = is_subscribed(uid)
        if status != "active":
            await q.message.reply_text("статус: подписка выключена.")
            return
        if not exp:
            await q.message.reply_text(f"статус: {'активна' if active else 'неактивна'} (forever).")
            return
        await q.message.reply_text(
            f"статус: {'активна' if active else 'неактивна'}\n"
            f"действует до: {fmt_dt_short(exp)}"
        )
        return

    if data.startswith("paidproof:"):
        try:
            req_id = int(data.split(":", 1)[1])
        except Exception:
            await q.message.reply_text("не поняла номер заявки.")
            return

        # проверим, что заявка принадлежит этому юзеру и она pending
        with db() as conn:
            row = conn.execute(
                "select id, user_id, status from payment_requests where id=?",
                (req_id,),
            ).fetchone()

        if not row or int(row["user_id"]) != uid:
            await q.message.reply_text("я не нашла такую заявку у тебя. нажми /pay заново.")
            return

        st = (row["status"] or "").lower()
        if st == "paid":
            await q.message.reply_text("эта заявка уже оплачена. если доступ не открылся, напиши администратору.")
            return

        await q.message.reply_text(
            f"Ок, приняла. Заявка #{req_id}.\n"
            "Теперь пришли сюда чек/скрин оплаты (можно фото) и укажи номер заявки в сообщении.\n"
            "После проверки я открою доступ."
        )
        return


async def cmd_paid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny_if_not_admin(update):
        return

    args = context.args or []
    if len(args) < 2:
        await update.effective_message.reply_text("формат: /paid <request_id> <days|forever>")
        return

    try:
        req_id = int(args[0])
    except Exception:
        await update.effective_message.reply_text("request_id должен быть числом.")
        return

    mode = args[1].strip().lower()
    expires_at = None

    if mode == "forever":
        expires_at = None
    else:
        try:
            days = int(mode)
            if days <= 0 or days > 36500:
                raise ValueError("bad days")
            expires_at = (datetime.now(TZ) + timedelta(days=days)).isoformat()
        except Exception:
            await update.effective_message.reply_text("второй аргумент: число дней или forever.")
            return

    with db() as conn:
        row = conn.execute(
            "select user_id, status from payment_requests where id=?",
            (req_id,),
        ).fetchone()

        if not row:
            await update.effective_message.reply_text("не нашла такую заявку.")
            return

        target_uid = int(row["user_id"])
        status = (row["status"] or "").lower()

        if status == "paid":
            await update.effective_message.reply_text("эта заявка уже помечена как paid.")
            return

        now = now_iso()

        conn.execute(
            """
            update payment_requests
            set status='paid', paid_at=?, admin_note=?, updated_at=?
            where id=?
            """,
            (now, f"approved by admin {update.effective_user.id}", now, req_id),
        )
        conn.commit()

    # выдаём подписку
    set_subscription(target_uid, active=True, expires_at=expires_at)

    await update.effective_message.reply_text(
        f"Ок. заявка #{req_id} = paid.\n"
        f"Подписка выдана пользователю {target_uid}. expires_at={fmt_dt_short(expires_at)}"
    )


async def deny_if_not_admin(update: Update) -> bool:
    uid = update.effective_user.id
    if is_admin(uid):
        return False
    await update.effective_message.reply_text("Эта команда доступна только для админа.")
    return True

# =====================
# helpers: meal labels
# =====================

MEAL_RU = {
    "breakfast": "завтрак",
    "lunch": "обед",
    "dinner": "ужин",
    "snack": "перекус",
    "other": "другое",
}

def meal_to_ru(meal: str) -> str:
    return MEAL_RU.get((meal or "").lower(), meal or "другое")

UNIT_RU = {
    "g": "г",
    "ml": "мл",
    "pcs": "шт",
    "tbsp": "ст. л.",
    "tsp": "ч. л.",
    "serving": "порц.",
}

def top_sources(src: dict[str, float], unit: str, limit: int = 3) -> str:
    if not src:
        return ""

    items = sorted(src.items(), key=lambda x: x[1], reverse=True)[:limit]
    return ", ".join(f"{name} ({fmt(val)} {unit})" for name, val in items)


def unit_to_ru(unit: str) -> str:
    return UNIT_RU.get((unit or "").lower(), unit)


# =====================
# db
# =====================

def _adapt_qmarks(sql: str) -> str:
    """Replace sqlite-style '?' placeholders with psycopg-style '%s' (outside quotes)."""
    out = []
    in_s = False
    in_d = False
    esc = False
    for ch in sql:
        if ch == "\\" and (in_s or in_d) and not esc:
            esc = True
            out.append(ch)
            continue
        if ch == "'" and not in_d and not esc:
            in_s = not in_s
        elif ch == '"' and not in_s and not esc:
            in_d = not in_d

        if ch == "?" and not in_s and not in_d:
            out.append("%s")
        else:
            out.append(ch)

        esc = False
    return "".join(out)


def db():
    database_url = os.getenv("DATABASE_URL")

    if database_url:
        # postgres (prod)
        conn = psycopg.connect(database_url)
        conn.row_factory = dict_row
        conn._db_kind = "postgres"  # internal flag

        _orig_execute = conn.execute

        def _execute(sql, params=None):
            sql2 = _adapt_qmarks(sql) if isinstance(sql, str) else sql
            if params is None:
                return _orig_execute(sql2)
            return _orig_execute(sql2, params)

        # monkeypatch for compatibility with existing sqlite-style code
        conn.execute = _execute
        return conn
    else:
        # sqlite (local dev)
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn._db_kind = "sqlite"  # internal flag
        return conn


def ensure_column(conn, table: str, col: str, col_type: str) -> None:
    """Add column if it doesn't exist (works for sqlite and postgres)."""
    kind = getattr(conn, "_db_kind", "")
    if kind == "postgres":
        row = conn.execute(
            """
            select 1
            from information_schema.columns
            where table_schema = 'public' and table_name = %s and column_name = %s
            limit 1
            """,
            (table, col),
        ).fetchone()
        if not row:
            conn.execute(f"alter table {table} add column {col} {col_type}")
    else:
        cols = [r["name"] for r in conn.execute(f"pragma table_info({table})").fetchall()]
        if col not in cols:
            conn.execute(f"alter table {table} add column {col} {col_type}")


def execute_ddl(conn, sql: str) -> None:
    """Execute DDL in a way that works on both sqlite and postgres."""
    kind = getattr(conn, "_db_kind", "")
    if kind == "postgres":
        s = sql
        s = re.sub(r"\binteger\s+primary\s+key\s+autoincrement\b", "bigserial primary key", s, flags=re.IGNORECASE)
        s = re.sub(r"\bid\s+integer\s+primary\s+key\b", "id bigserial primary key", s, flags=re.IGNORECASE)
        s = re.sub(r"\buser_id\s+integer\s+primary\s+key\b", "user_id bigint primary key", s, flags=re.IGNORECASE)
        s = re.sub(r"\buser_id\s+integer\s+not\s+null\b", "user_id bigint not null", s, flags=re.IGNORECASE)
        s = re.sub(r"\buser_id\s+integer\b", "user_id bigint", s, flags=re.IGNORECASE)
        s = re.sub(r"\bautoincrement\b", "", s, flags=re.IGNORECASE)
        conn.execute(s)
    else:
        conn.execute(sql)

def init_db() -> None:
    with db() as conn:
        execute_ddl(conn, """
        create table if not exists users (
            user_id integer primary key,
            created_at text not null,
            tz text,
            language text,

            onboarding_done integer not null default 0,
            onboarding_step text,

            goal text,
            sex text,
            age integer,
            height_cm real,
            weight_kg real,
            activity text,

            prefs_json text
        )
        """)

        ensure_column(conn, "users", "primary_issues_json", "text")

        execute_ddl(conn, """
        create table if not exists rate_limits (
            user_id integer not null,
            bucket text not null,          -- например 'ai_min', 'ai_day'
            window_start text not null,    -- iso datetime (или date для day)
            count integer not null,
            updated_at text not null,
            primary key(user_id, bucket)
        )
        """)
        execute_ddl(conn, "create index if not exists idx_rate_limits_bucket on rate_limits(bucket)")

        execute_ddl(conn, """
        create table if not exists entries (
            id integer primary key autoincrement,
            user_id integer not null,
            entry_date text not null,
            meal text not null,
            raw_text text not null,
            item_name text not null,
            qty real not null,
            unit text not null,
            calories real,
            protein real,
            fat real,
            carbs real,
            fiber real,
            confidence real,
            meta_json text,
            created_at text not null
        )
        """)
        execute_ddl(conn, "create index if not exists idx_entries_user_date on entries(user_id, entry_date)")

        execute_ddl(conn, """
        create table if not exists daily_summaries (
            id integer primary key autoincrement,
            user_id integer not null,
            entry_date text not null,
            entry_count integer not null,
            last_entry_id integer not null,
            analysis_text text not null,
            analysis_json text,
            created_at text not null,
            updated_at text not null,
            unique(user_id, entry_date)
        )
        """)
        execute_ddl(conn, "create index if not exists idx_daily_summaries_user_date on daily_summaries(user_id, entry_date)")

        execute_ddl(conn, """
        create table if not exists weekly_summaries (
            id integer primary key autoincrement,
            user_id integer not null,
            start_date text not null,
            end_date text not null,
            days_window integer not null,
            days_logged integer not null,
            entry_count integer not null,
            last_entry_id integer not null,
            analysis_text text not null,
            analysis_json text,
            created_at text not null,
            updated_at text not null,
            unique(user_id, start_date, end_date)
        )
        """)
        execute_ddl(conn, "create index if not exists idx_weekly_summaries_user_range on weekly_summaries(user_id, start_date, end_date)")

        # подписка
        execute_ddl(conn, """
        create table if not exists subscriptions (
            user_id integer primary key,
            status text not null,              -- 'active' | 'inactive'
            expires_at text,                   -- iso datetime or null for forever
            created_at text not null,
            updated_at text not null
        )
        """)

        # словарь пользователя
        execute_ddl(conn, """
        create table if not exists food_aliases (
            id integer primary key autoincrement,
            user_id integer not null,
            alias_text text not null,
            canonical_name text not null,
            default_qty real,
            default_unit text,
            macro_override_json text,
            created_at text not null,
            updated_at text not null,
            unique(user_id, alias_text)
        )
        """)
        execute_ddl(conn, "create index if not exists idx_food_aliases_user on food_aliases(user_id)")

        # targets (дневные цели)
        execute_ddl(conn, """
        create table if not exists targets (
            user_id integer primary key,
            kcal real,
            protein real,
            fat real,
            carbs real,
            fiber real,
            net_carbs real,
            diet text,                      -- NEW: 'keto' | 'lowcarb' | null
            created_at text not null,
            updated_at text not null
        )
        """)

        # если таблица уже есть со старой схемой
        ensure_column(conn, "targets", "diet", "text")

        ensure_column(conn, "entries", "fiber", "real")

        execute_ddl(conn, """
        create table if not exists payment_requests (
            id integer primary key autoincrement,
            user_id integer not null,
            status text not null,              -- 'pending' | 'paid' | 'canceled'
            plan text,                         -- например 'monthly', 'yearly', 'custom'
            amount real,
            currency text,
            created_at text not null,
            updated_at text not null,
            paid_at text,
            admin_note text
        )
        """)
        ensure_column(conn, "payment_requests", "proof_text", "text")
        ensure_column(conn, "payment_requests", "proof_file_id", "text")
        ensure_column(conn, "payment_requests", "paid_at", "text")
        ensure_column(conn, "payment_requests", "updated_at", "text")
        ensure_column(conn, "payment_requests", "admin_note", "text")

        execute_ddl(conn, "create index if not exists idx_payment_requests_user on payment_requests(user_id)")
        execute_ddl(conn, "create index if not exists idx_payment_requests_status on payment_requests(status)")

        conn.commit()

def now_iso() -> str:
    return datetime.now(TZ).isoformat()

def today_str() -> str:
    return datetime.now(TZ).date().isoformat()

def get_pending_payment_request(uid: int) -> dict | None:
    with db() as conn:
        row = conn.execute(
            """
            select id, user_id, status, created_at, updated_at, proof_text, proof_file_id
            from payment_requests
            where user_id=? and status='pending'
            order by id desc
            limit 1
            """,
            (uid,),
        ).fetchone()
    return dict(row) if row else None


async def notify_admins(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    # если ADMIN_IDS пуст, значит у тебя “все админы”, но рассылать некому, поэтому молча пропускаем
    if not ADMIN_IDS:
        return
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=text)
        except Exception:
            pass

def attach_payment_proof(uid: int, req_id: int, proof_text: str | None, proof_file_id: str | None) -> None:
    now = now_iso()
    with db() as conn:
        conn.execute(
            """
            update payment_requests
            set proof_text=coalesce(?, proof_text),
                proof_file_id=coalesce(?, proof_file_id),
                updated_at=?
            where id=? and user_id=? and status='pending'
            """,
            (proof_text, proof_file_id, now, req_id, uid),
        )
        conn.commit()


def infer_meal_by_time(dt: datetime) -> str:
    # правила:
    # до 11:00 -> breakfast
    # до 17:00 -> lunch
    # после -> dinner
    h = dt.hour
    if h < 11:
        return "breakfast"
    if h < 17:
        return "lunch"
    return "dinner"


def today_date() -> date:
    return datetime.now(TZ).date()

# =====================
# subscription gate
# =====================

def get_subscription(uid: int) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "select user_id, status, expires_at, created_at, updated_at from subscriptions where user_id=?",
            (uid,),
        ).fetchone()
    return dict(row) if row else None

def is_subscribed(uid: int) -> bool:
    if is_admin(uid):
        return True
    sub = get_subscription(uid)
    if not sub:
        return False
    if (sub.get("status") or "").lower() != "active":
        return False
    exp = sub.get("expires_at")
    if not exp:
        return True  # forever
    try:
        exp_dt = datetime.fromisoformat(exp)
    except Exception:
        return False
    return datetime.now(TZ) <= exp_dt

TRIAL_DAYS = 3
AI_LIMIT_PER_MIN = 6       # разумно: 6 ai-вызовов в минуту на пользователя
AI_LIMIT_PER_DAY = 80      # и 80 в сутки

def _floor_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)

def _date_str(dt: datetime) -> str:
    return dt.date().isoformat()

def _rl_check_and_inc(uid: int, bucket: str, window_start: str, limit: int) -> tuple[bool, int]:
    now = now_iso()
    with db() as conn:
        row = conn.execute(
            "select window_start, count from rate_limits where user_id=? and bucket=?",
            (uid, bucket),
        ).fetchone()

        if not row:
            conn.execute(
                "insert into rate_limits(user_id, bucket, window_start, count, updated_at) values(?,?,?,?,?)",
                (uid, bucket, window_start, 1, now),
            )
            conn.commit()
            return True, 1

        cur_ws = row["window_start"]
        cur_cnt = int(row["count"] or 0)

        if cur_ws != window_start:
            conn.execute(
                "update rate_limits set window_start=?, count=?, updated_at=? where user_id=? and bucket=?",
                (window_start, 1, now, uid, bucket),
            )
            conn.commit()
            return True, 1

        if cur_cnt >= limit:
            return False, cur_cnt

        conn.execute(
            "update rate_limits set count=count+1, updated_at=? where user_id=? and bucket=?",
            (now, uid, bucket),
        )
        conn.commit()
        return True, cur_cnt + 1

def get_quota(uid: int) -> dict:
    dt = datetime.now(TZ)
    ws_min = _floor_minute(dt).isoformat()
    ws_day = _date_str(dt)

    with db() as conn:
        r_min = conn.execute(
            "select window_start, count from rate_limits where user_id=? and bucket=?",
            (uid, "ai_min"),
        ).fetchone()
        r_day = conn.execute(
            "select window_start, count from rate_limits where user_id=? and bucket=?",
            (uid, "ai_day"),
        ).fetchone()

    used_min = 0
    if r_min and r_min["window_start"] == ws_min:
        used_min = int(r_min["count"] or 0)

    used_day = 0
    if r_day and r_day["window_start"] == ws_day:
        used_day = int(r_day["count"] or 0)

    rem_min = max(0, AI_LIMIT_PER_MIN - used_min)
    rem_day = max(0, AI_LIMIT_PER_DAY - used_day)

    next_min = (_floor_minute(dt) + timedelta(minutes=1)).strftime("%H:%M")
    tomorrow = (dt.date() + timedelta(days=1)).isoformat()

    return {
        "used_min": used_min,
        "rem_min": rem_min,
        "used_day": used_day,
        "rem_day": rem_day,
        "minute_window": ws_min,
        "day_window": ws_day,
        "next_min_at": next_min,
        "tomorrow": tomorrow,
    }

async def cmd_quota(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    ensure_user(uid)

    q = get_quota(uid)

    await update.effective_message.reply_text(
        "квота запросов:\n"
        f"- в минуту: использовано {q['used_min']} / {AI_LIMIT_PER_MIN}, осталось {q['rem_min']} (обновится в {q['next_min_at']})\n"
        f"- в день: использовано {q['used_day']} / {AI_LIMIT_PER_DAY}, осталось {q['rem_day']} (сброс {q['tomorrow']})"
    )

async def deny_if_rate_limited(update: Update, uid: int) -> bool:
    dt = datetime.now(TZ)

    # минутный лимит
    ws_min = _floor_minute(dt).isoformat()
    ok1, _ = _rl_check_and_inc(uid, "ai_min", ws_min, AI_LIMIT_PER_MIN)
    if not ok1:
        # когда можно снова: следующая минута
        next_min = (_floor_minute(dt) + timedelta(minutes=1)).strftime("%H:%M")
        await update.effective_message.reply_text(
            f"слишком много запросов за минуту. попробуй снова после {next_min}."
        )
        return True

    # дневной лимит
    ws_day = _date_str(dt)
    ok2, cnt_day = _rl_check_and_inc(uid, "ai_day", ws_day, AI_LIMIT_PER_DAY)
    if not ok2:
        await update.effective_message.reply_text(
            "лимит запросов на сегодня исчерпан. завтра продолжим, если мир не закончится раньше."
        )
        return True

    return False


def ensure_trial_subscription(uid: int) -> None:
    # если админ, ничего не надо
    if is_admin(uid):
        return

    sub = get_subscription(uid)
    if sub:
        return  # уже есть подписка/триал

    now = datetime.now(TZ)
    expires_at = (now + timedelta(days=TRIAL_DAYS)).isoformat()
    set_subscription(uid, active=True, expires_at=expires_at)


def set_subscription(uid: int, active: bool, expires_at: str | None) -> None:
    status = "active" if active else "inactive"
    now = now_iso()
    with db() as conn:
        conn.execute(
            """
            insert into subscriptions(user_id, status, expires_at, created_at, updated_at)
            values (?, ?, ?, ?, ?)
            on conflict(user_id) do update set
              status=excluded.status,
              expires_at=excluded.expires_at,
              updated_at=excluded.updated_at
            """,
            (uid, status, expires_at, now, now),
        )
        conn.commit()

async def deny_if_not_subscribed(update: Update) -> bool:
    uid = update.effective_user.id
    if is_subscribed(uid):
        return False
    await update.effective_message.reply_text(
        "Доступ к дневнику питания закрыт: пробный период закончился.\n"
        "Осталось бесплатно: /help, /profile, /sub_status.\n"
        "Чтобы продолжить: /pay (оплатить подписку).\n"
    )
    return True

def fmt_dt_short(dt_iso: str | None) -> str:
    if not dt_iso:
        return "—"
    try:
        d = datetime.fromisoformat(dt_iso)
        return d.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return dt_iso

# =====================
# targets (daily goals)
# =====================

def _diet_norm(s: str | None) -> str | None:
    if not s:
        return None
    t = (s or "").strip().lower()
    if t in ("keto", "кето"):
        return "keto"
    if t in ("lowcarb", "low_carb", "low-carb", "lc", "низкоугл", "низкоуглеводка", "низкоуглевод"):
        return "lowcarb"
    return None

def calc_targets_auto(diet: str, kcal: float, weight_kg: float | None) -> dict:
    diet_n = _diet_norm(diet)
    if not diet_n:
        raise ValueError("unknown diet")

    # дефолты по net carbs
    netc = 20.0 if diet_n == "keto" else 50.0

    # белок: по весу, если есть, иначе дефолт
    if weight_kg and weight_kg > 0:
        protein = round(float(weight_kg) * 1.6, 1)
    else:
        protein = 100.0

    carbs = netc  # мы трактуем carbs как "чистые" в этом режиме? нет.
    # важно: у тебя в таблице carbs = total carbs, net_carbs = net.
    # в авто-режиме логично поставить:
    # - carbs = netc (как цель по углям), а fiber отдельно не считаем
    # - net_carbs = netc
    # это честно и совместимо с твоим выводом "чистые углеводы: total_net / goal_net"
    #
    # если хочешь, можно carbs оставить None, но тогда прогресс по carbs будет "0.0".
    # поэтому ставим carbs=netc.

    fiber = None

    remaining = float(kcal) - (protein * 4.0) - (carbs * 4.0)
    fat = round(remaining / 9.0, 1)

    if fat <= 0:
        raise ValueError("negative fat")

    return {
        "kcal": float(kcal),
        "protein": float(protein),
        "fat": float(fat),
        "carbs": float(carbs),
        "fiber": fiber,
        "net_carbs": float(netc),
        "diet": diet_n,
    }

def get_targets(uid: int) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "select user_id, kcal, protein, fat, carbs, fiber, net_carbs, diet, created_at, updated_at from targets where user_id=?",
            (uid,),
        ).fetchone()
    return dict(row) if row else None

def set_targets(
    uid: int,
    kcal: float | None,
    protein: float | None,
    fat: float | None,
    carbs: float | None,
    fiber: float | None,
    net_carbs_goal: float | None,
    diet: str | None = None,
) -> None:
    now = now_iso()
    with db() as conn:
        conn.execute(
            """
            insert into targets(user_id, kcal, protein, fat, carbs, fiber, net_carbs, diet, created_at, updated_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(user_id) do update set
              kcal=excluded.kcal,
              protein=excluded.protein,
              fat=excluded.fat,
              carbs=excluded.carbs,
              fiber=excluded.fiber,
              net_carbs=excluded.net_carbs,
              diet=excluded.diet,
              updated_at=excluded.updated_at
            """,
            (uid, kcal, protein, fat, carbs, fiber, net_carbs_goal, diet, now, now),
        )

        conn.commit()

def _pct(val: float, goal: float) -> str:
    if goal <= 0:
        return "—"
    return f"{(val / goal) * 100:.0f}%"

def targets_text(t: dict | None) -> str:
    if not t:
        return "Цели: не заданы. установи: /set_targets <ккал> <белки> <жиры> <углеводы> [клетчатка] [чистые_угл]\nили авто: /set_targets keto <ккал> | /set_targets lowcarb <ккал>"
    out = (
        "Цели на день:\n"
        f"- ккал: {fmt(t.get('kcal'))}\n"
        f"- белки: {fmt(t.get('protein'))} г\n"
        f"- жиры: {fmt(t.get('fat'))} г\n"
        f"- углеводы: {fmt(t.get('carbs'))} г\n"
        f"- клетчатка: {fmt(t.get('fiber'))} г\n"
        f"- чистые углеводы: {fmt(t.get('net_carbs'))} г\n"
    )
    if t.get("diet"):
        out += f"- Режим: {t.get('diet')}\n"
    return out


# =====================
# debug per user
# =====================
USER_DEBUG: dict[int, bool] = {}

def is_debug(uid: int) -> bool:
    return USER_DEBUG.get(uid, False)

def fmt(v) -> str:
    if v is None:
        return "0.0"
    try:
        x = float(v)
    except Exception:
        return "0.0"
    if abs(x) < 1e-9:
        x = 0.0
    return f"{x:.1f}"

def net_carbs(carbs, fiber):
    try:
        c = float(carbs or 0.0)
    except Exception:
        c = 0.0
    try:
        f = float(fiber or 0.0)
    except Exception:
        f = 0.0
    n = c - f
    return n if n > 0 else 0.0

# =====================
# fiber sanity checks
# =====================

FIBER_LIKELY = (
    "чиа", "chia",
    "огур", "cucumber",
    "помид", "tomat",
    "салат", "alface", "salad",
    "брок", "broccoli", "brócolis",
    "капуст", "cabbage",
    "овощ", "vegetable", "legume",
    "сем", "seed",
    "орех", "nut",
    "ягод", "berry",
    "фасол", "feijão", "beans",
    "зерн", "cereal", "granola", "aveia", "oats",
    "цельн", "whole",
    "fruit", "fruta"
)

def suspicious_zero_fiber(name: str, fiber, carbs) -> bool:
    if fiber is None:
        return False
    try:
        f = float(fiber)
        c = 0.0 if carbs is None else float(carbs)
    except Exception:
        return False
    if f != 0.0 or c <= 0:
        return False
    n = (name or "").lower()
    return any(k in n for k in FIBER_LIKELY)

# =====================
# data quality: category + clamps
# =====================

FOOD_CATEGORIES = {
    "egg": ["яйц", "egg"],
    "meat": ["мяс", "говя", "кур", "индей", "pork", "beef", "chicken", "lingui", "linguiça", "salsicha", "sausage"],
    "fish": ["рыб", "salmon", "tuna", "atum", "salmão", "peixe"],
    "dairy": ["сыр", "cheese", "iogurte", "йогурт", "cream", "leite", "milk"],
    "vegetable": ["огур", "помид", "томат", "брок", "капуст", "cucumber", "tomat", "broccoli", "brócolis", "alface", "salad"],
    "fruit": ["ябл", "banana", "банан", "avocado", "abacate", "manga", "melão", "mamão", "papaya"],
    "fat": ["масло", "oil", "azeite", "manteiga", "butter", "ghee"],
    "grain": ["рис", "rice", "oats", "овсян", "хлеб", "bread", "macarr", "pasta", "massa"],
    "unknown": [],
}

CATEGORY_LIMITS = {
    "egg":       {"calories": (40, 120), "protein": (3, 15), "fat": (0, 12)},
    "meat":      {"calories": (80, 450), "protein": (10, 70), "fat": (0, 45)},
    "fish":      {"calories": (70, 400), "protein": (10, 70), "fat": (0, 35)},
    "dairy":     {"calories": (50, 500), "protein": (3, 45), "fat": (0, 45)},
    "vegetable": {"calories": (5, 200),  "protein": (0, 12), "fat": (0, 8)},
    "fruit":     {"calories": (20, 350), "protein": (0, 6),  "fat": (0, 20)},
    "fat":       {"calories": (20, 350), "protein": (0, 3),  "fat": (2, 40)},
    "grain":     {"calories": (50, 550), "protein": (2, 30), "fat": (0, 25)},
    "unknown":   {},
}

def detect_food_category(name: str) -> str:
    n = (name or "").lower()
    for cat, keys in FOOD_CATEGORIES.items():
        for k in keys:
            if k in n:
                return cat
    return "unknown"

def clamp(v: float | None, lo: float, hi: float) -> float | None:
    if v is None:
        return None
    try:
        x = float(v)
    except Exception:
        return None
    return max(lo, min(hi, x))

def sanitize_item(it: "AiItem") -> tuple["AiItem", list[str]]:
    notes: list[str] = []
    cat = detect_food_category(it.name)
    limits = CATEGORY_LIMITS.get(cat, {})

    def fix(field: str):
        v = getattr(it, field)
        if field in limits and v is not None:
            lo, hi = limits[field]
            nv = clamp(v, lo, hi)
            if nv is not None and nv != v:
                notes.append(f"{it.name}: {field} {v} -> {nv} (clamp {cat})")
                setattr(it, field, nv)

    fix("calories")
    fix("protein")
    fix("fat")

    if it.carbs is not None:
        try:
            c = float(it.carbs)
        except Exception:
            c = None
        if c is not None:
            f = float(it.fiber or 0.0)
            if c < f:
                notes.append(f"{it.name}: carbs {it.carbs} < fiber {f}, corrected")
                it.carbs = f

    return it, notes


def fix_totals_for_pcs(items: list) -> list[str]:
    notes = []
    for it in items:
        if (it.unit or "").lower() != "pcs":
            continue
        if it.qty is None or it.qty <= 1:
            continue

        cat = detect_food_category(it.name)
        limits = CATEGORY_LIMITS.get(cat) or {}
        if not limits:
            continue

        # проверяем calories/protein/fat как будто они "per 1 pcs"
        # и если так, масштабируем на qty
        def maybe_scale(field: str):
            v = getattr(it, field)
            if v is None:
                return
            if field not in limits:
                return
            lo, hi = limits[field]
            # если значение попадает в диапазон на 1 штуку, а qty>1,
            # то, скорее всего, ai дал per-piece
            if lo <= float(v) <= hi:
                setattr(it, field, float(v) * float(it.qty))
                notes.append(f"{it.name}: {field} умножено на qty={it.qty} (похоже было на 1 шт)")

        maybe_scale("calories")
        maybe_scale("protein")
        maybe_scale("fat")
    return notes


# =====================
# user helpers
# =====================

def ensure_user(uid: int) -> None:
    now = now_iso()
    created = False
    with db() as conn:
        row = conn.execute("select user_id from users where user_id=?", (uid,)).fetchone()
        if not row:
            conn.execute(
                "insert into users(user_id, created_at, tz, language, onboarding_done, onboarding_step, prefs_json) "
                "values (?, ?, ?, ?, 0, ?, ?)",
                (uid, now, "America/Sao_Paulo", "ru", "goal", json.dumps({}, ensure_ascii=False)),
            )
            conn.commit()
            created = True

    # выдаём trial только при первом появлении
    if created:
        ensure_trial_subscription(uid)


def get_user(uid: int) -> dict:
    with db() as conn:
        row = conn.execute("select * from users where user_id=?", (uid,)).fetchone()
        if not row:
            ensure_user(uid)
            row = conn.execute("select * from users where user_id=?", (uid,)).fetchone()
        return dict(row)

def update_user(uid: int, **fields) -> None:
    if not fields:
        return
    cols = []
    vals = []
    for k, v in fields.items():
        cols.append(f"{k}=?")
        vals.append(v)
    vals.append(uid)
    q = f"update users set {', '.join(cols)} where user_id=?"
    with db() as conn:
        conn.execute(q, tuple(vals))
        conn.commit()

def profile_text(u: dict) -> str:
    prefs = {}
    try:
        prefs = json.loads(u.get("prefs_json") or "{}")
    except Exception:
        prefs = {}
    prefs_list = [k for k, v in prefs.items() if v]
    prefs_str = ", ".join(prefs_list) if prefs_list else "—"

    issues = {}
    try:
        issues = json.loads(u.get("primary_issues_json") or "{}")
    except Exception:
        issues = {}
    issues_list = [k for k, v in issues.items() if v]
    issues_str = ", ".join(issues_list) if issues_list else "—"

    return (
        "Твой профиль:\n"
        f"- Цель: {u.get('goal') or '—'}\n"
        f"- Пол: {u.get('sex') or '—'}\n"
        f"- Возраст: {u.get('age') or '—'}\n"
        f"- Рост: {u.get('height_cm') or '—'} см\n"
        f"- Вес: {u.get('weight_kg') or '—'} кг\n"
        f"- Активность: {u.get('activity') or '—'}\n"
        f"- Предпочтения: {prefs_str}\n"
        f"- Что решить в первую очередь: {issues_str}\n"

    )

# =====================
# alias dictionary
# =====================

ALLOWED_UNITS = {"g", "ml", "pcs", "tbsp", "tsp", "serving"}

def _norm_alias(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s

def upsert_alias(uid: int, alias_text: str, canonical: str, default_qty: float | None, default_unit: str | None) -> None:
    alias_n = _norm_alias(alias_text)
    canonical_n = (canonical or "").strip()
    if not alias_n or not canonical_n:
        raise ValueError("alias/canonical empty")

    if default_unit is not None:
        default_unit = default_unit.strip().lower()
        if default_unit not in ALLOWED_UNITS:
            raise ValueError(f"unit must be one of {sorted(ALLOWED_UNITS)}")

    now = now_iso()
    with db() as conn:
        conn.execute(
            """
            insert into food_aliases(user_id, alias_text, canonical_name, default_qty, default_unit, macro_override_json, created_at, updated_at)
            values (?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(user_id, alias_text) do update set
              canonical_name=excluded.canonical_name,
              default_qty=excluded.default_qty,
              default_unit=excluded.default_unit,
              updated_at=excluded.updated_at
            """,
            (uid, alias_n, canonical_n, default_qty, default_unit, None, now, now),
        )
        conn.commit()

def delete_alias(uid: int, alias_text: str) -> int:
    alias_n = _norm_alias(alias_text)
    with db() as conn:
        cur = conn.execute("delete from food_aliases where user_id=? and alias_text=?", (uid, alias_n))
        conn.commit()
        return int(cur.rowcount or 0)

def list_aliases(uid: int) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            """
            select alias_text, canonical_name, default_qty, default_unit, updated_at
            from food_aliases
            where user_id=?
            order by alias_text asc
            """,
            (uid,),
        ).fetchall()
    return [dict(r) for r in rows]

def _parse_qty_unit(s: str) -> tuple[float | None, str | None]:
    s = (s or "").strip()
    if not s:
        return None, None
    parts = re.split(r"\s+", s)
    if not parts:
        return None, None
    try:
        qty = float(parts[0].replace(",", "."))
    except Exception:
        return None, None
    unit = parts[1].strip().lower() if len(parts) > 1 else None
    if unit and unit not in ALLOWED_UNITS:
        return qty, None
    return qty, unit

def apply_aliases_to_text(uid: int, text: str) -> tuple[str, list[str]]:
    original = text or ""
    t = original
    notes: list[str] = []

    aliases = list_aliases(uid)
    if not aliases:
        return t, notes

    aliases_sorted = sorted(aliases, key=lambda a: len(a["alias_text"] or ""), reverse=True)
    t_low = t.lower()

    for a in aliases_sorted:
        alias = a["alias_text"] or ""
        canonical = a["canonical_name"] or ""
        dq = a.get("default_qty")
        du = a.get("default_unit")

        if not alias or alias not in t_low:
            continue

        def repl(match: re.Match) -> str:
            end = match.end()
            tail = t_low[end:end+6]
            has_number_soon = any(ch.isdigit() for ch in tail)

            out = canonical
            if dq is not None and du and not has_number_soon:
                out = f"{canonical} {dq} {du}"
            return out

        pattern = re.escape(alias)
        new_t = re.sub(pattern, repl, t, flags=re.IGNORECASE)

        if new_t != t:
            if dq is not None and du:
                notes.append(f"alias: '{alias}' -> '{canonical}' (+ {dq} {du} если не было количества рядом)")
            else:
                notes.append(f"alias: '{alias}' -> '{canonical}'")
            t = new_t
            t_low = t.lower()

    return t, notes

# =====================
# onboarding
# =====================

ONBOARDING_ORDER = ["goal", "sex", "age", "height", "weight", "activity", "issues", "prefs", "done"]

def next_step(cur: str | None) -> str:
    if not cur:
        return "goal"
    try:
        i = ONBOARDING_ORDER.index(cur)
        return ONBOARDING_ORDER[min(i + 1, len(ONBOARDING_ORDER) - 1)]
    except ValueError:
        return "goal"

def normalize_text(s: str) -> str:
    return (s or "").strip().lower()

def parse_number(s: str) -> float | None:
    s = normalize_text(s).replace(",", ".")
    s = "".join(ch for ch in s if ch.isdigit() or ch == ".")
    if not s:
        return None
    try:
        return float(s)
    except Exception:
        return None

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    ensure_user(uid)

    # allow payment proof photo without subscription
    if not is_subscribed(uid):
        pending = get_pending_payment_request(uid)
        if not pending:
            await update.effective_message.reply_text(
                "Фото получено, но у тебя нет заявки на оплату.\n"
                "Сначала нажми /pay, потом пришли фото чека."
            )
            return

        photos = update.message.photo or []
        if not photos:
            return
        ph = photos[-1]
        # сохраняем file_id (проще, чем качать bytes)
        attach_payment_proof(uid, pending["id"], proof_text=None, proof_file_id=ph.file_id)

        await update.effective_message.reply_text(
            f"Принято. Фото чека привязано к заявке #{pending['id']}.\n"
            "Администратор подтвердит оплату и откроет доступ."
        )
        await notify_admins(
            context,
            f"payment proof (photo)\nuser_id={uid}\nrequest=#{pending['id']}\nfile_id={ph.file_id}"
        )
        return

    if await deny_if_not_subscribed(update):
        return
    u = get_user(uid)

    if is_onboarding_needed(u) and (u.get("onboarding_step") not in (None, "done")):
        await update.effective_message.reply_text("сначала закончим онбординг. можно /skip или /cancel.")
        return

    photos = update.message.photo or []
    if not photos:
        return

    # берём самое большое фото
    ph = photos[-1]
    file = await context.bot.get_file(ph.file_id)

    # качаем bytes
    bio = await file.download_as_bytearray()
    image_bytes = bytes(bio)

    # mime: телеграм чаще всего jpeg
    image_mime = "image/jpeg"

    meal = infer_meal_by_time(datetime.now(TZ))
    await handle_log_photo(update, u, meal, image_bytes, image_mime)

async def handle_log_photo(update: Update, u: dict, meal: str, image_bytes: bytes, image_mime: str) -> None:
    uid = u["user_id"]
    d = today_str()
    debug = is_debug(uid)

    if await deny_if_rate_limited(update, uid):
        return

    profile_hint = build_profile_hint(u)

    try:
        items, confidence, meta = ai_estimate_photo(
            image_bytes=image_bytes,
            image_mime=image_mime,
            meal_hint=meal,
            profile_hint=profile_hint
        )
    except Exception as e:
        await update.effective_message.reply_text(f"ai по фото сломался: {type(e).__name__}: {str(e)[:200]}")
        return

    if not items:
        await update.effective_message.reply_text("не смогла распознать еду на фото. попробуй другое фото или добавь текстом.")
        return

    created_at = now_iso()
    meta_json = json.dumps(meta, ensure_ascii=False)

    # raw_text для фото сделаем техническим
    raw_text = "[photo]"

    with db() as conn:
        for it in items:
            conn.execute(
                """
                insert into entries(
                    user_id, entry_date, meal, raw_text,
                    item_name, qty, unit,
                    calories, protein, fat, carbs, fiber,
                    confidence, meta_json, created_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uid, d, meal, raw_text,
                    it.name, it.qty, it.unit,
                    it.calories, it.protein, it.fat, it.carbs, it.fiber,
                    confidence, meta_json, created_at
                ),
            )
        conn.commit()

    lines = [f"добавлено по фото ({meal_to_ru(meal)}):"]
    for it in items:
        fib = float(it.fiber or 0.0)
        net = net_carbs(it.carbs, fib)
        lines.append(
            f"- {it.name} ({it.qty} {unit_to_ru(it.unit)}): {fmt(it.calories)} ккал, "
            f"белки {fmt(it.protein)} г, жиры {fmt(it.fat)} г, углеводы {fmt(it.carbs)} г "
            f"(клетч. {fmt(fib)} г, чистые {fmt(net)} г)"
        )

    if debug:
        lines.append("")
        lines.append(f"debug: уверенность={fmt(confidence)}")
        assumptions = meta.get("assumptions") or []
        notes = meta.get("notes") or ""
        if assumptions:
            lines.append("assumptions:")
            for a in assumptions[:12]:
                lines.append(f"  - {a}")
        if notes:
            lines.append(f"notes: {notes[:500]}")
        lines.append(f"profile_hint: {json.dumps(profile_hint, ensure_ascii=False)}")

    await update.effective_message.reply_text("\n".join(lines))

async def onboarding_prompt(update: Update, step: str) -> None:
    msg = update.effective_message
    if step == "goal":
        await msg.reply_text(
            "Давай быстро настроим профиль (ты можешь делать /skip на любом шаге). Больше информации поможет мне давать более полезные советы.\n"
            "1/8 Твоя цель? напиши: похудение / поддержание / набор веса"
        )
    elif step == "sex":
        await msg.reply_text("2/8 Твой пол? напиши: ж / м (или /skip)")
    elif step == "age":
        await msg.reply_text("3/8 Твой возраст? число (например: 46) (или /skip)")
    elif step == "height":
        await msg.reply_text("4/8 Твой рост в см? число (например: 163) (или /skip)")
    elif step == "weight":
        await msg.reply_text("5/8 Твой вес в кг? число (например: 63) (или /skip)")
    elif step == "activity":
        await msg.reply_text("6/8 Активность? низкая / средняя / высокая (или /skip)")
    elif step == "issues":
        await msg.reply_text(
            "7/8 Какие проблемы ты хочешь решить в первую очередь?\n"
            "Напиши через запятую (можно 1–5):\n"
            "Например, отеки, низкая энергия, запоры, вздутие, сон, тяга к сладкому, кожа, вес\n"
            "(или /skip)"
        )
    elif step == "prefs":
        await msg.reply_text(
            "8/8 Твои предпочтения одной строкой, через запятую.\n"
            "Например: low-carb, без сахара, без хлеба, без алкоголя\n"
            "(или /skip)"
        )
    elif step == "done":
        await msg.reply_text("Готово! Профиль сохранен. Теперь ты можешь писать еду обычным текстом и получать персональные рекомендации.")

def is_onboarding_needed(u: dict) -> bool:
    return int(u.get("onboarding_done") or 0) == 0

def set_prefs_from_text(text: str) -> str:
    raw = normalize_text(text)
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    prefs = {}
    for t in tokens:
        if t in ("lowcarb", "low-carb", "keto", "кето"):
            prefs["low-carb"] = True
        elif t in ("без сахара", "no sugar", "nosugar"):
            prefs["без сахара"] = True
        elif t in ("без хлеба", "no bread", "nobread"):
            prefs["без хлеба"] = True
        elif t in ("без алкоголя", "no alcohol", "noalcohol"):
            prefs["без алкоголя"] = True
        else:
            prefs[t] = True
    return json.dumps(prefs, ensure_ascii=False)

def set_issues_from_text(text: str) -> str:
    raw = normalize_text(text)
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    issues = {}

    for t in tokens:
        if any(k in t for k in ("отек", "отеки", "incha", "edema")):
            issues["отеки"] = True
        elif any(k in t for k in ("энерг", "низкая энергия", "cansa", "fadiga", "energia")):
            issues["низкая энергия"] = True
        elif any(k in t for k in ("запор", "constip", "prisão de ventre")):
            issues["запоры"] = True
        elif any(k in t for k in ("вздут", "inchaço", "bloating", "gases", "газ")):
            issues["вздутие/газы"] = True
        elif any(k in t for k in ("сон", "insônia", "sleep")):
            issues["сон"] = True
        elif any(k in t for k in ("сладк", "açúcar", "doce", "craving")):
            issues["тяга к сладкому"] = True
        elif any(k in t for k in ("кож", "pele", "skin")):
            issues["кожа"] = True
        elif any(k in t for k in ("вес", "похуд", "weight", "emagrec")):
            issues["вес"] = True
        else:
            issues[t] = True

    return json.dumps(issues, ensure_ascii=False)


async def handle_onboarding_input(update: Update, u: dict, text: str) -> bool:
    step = u.get("onboarding_step") or "goal"
    t = normalize_text(text)
    uid = u["user_id"]
    msg = update.effective_message

    if t == "/cancel":
        update_user(uid, onboarding_step=None)
        await msg.reply_text("Онбординг остановлен. продолжить можно командой /start.")
        return True

    if t in ("/skip", "skip"):
        ns = next_step(step)
        update_user(uid, onboarding_step=ns)
        await onboarding_prompt(update, ns)
        return True

    if step == "goal":
        if "пох" in t:
            update_user(uid, goal="похудение", onboarding_step=next_step(step))
        elif "подд" in t:
            update_user(uid, goal="поддержание", onboarding_step=next_step(step))
        elif "набор" in t:
            update_user(uid, goal="набор", onboarding_step=next_step(step))
        else:
            await msg.reply_text("напиши: похудение / поддержание / набор (или /skip)")
            return True

    elif step == "sex":
        if t in ("ж", "f", "female", "жен", "женский"):
            update_user(uid, sex="ж", onboarding_step=next_step(step))
        elif t in ("м", "m", "male", "муж", "мужской"):
            update_user(uid, sex="м", onboarding_step=next_step(step))
        else:
            await msg.reply_text("напиши: ж / м (или /skip)")
            return True

    elif step == "age":
        n = parse_number(t)
        if n is None or n <= 0 or n > 120:
            await msg.reply_text("возраст числом (например: 46) (или /skip)")
            return True
        update_user(uid, age=int(n), onboarding_step=next_step(step))

    elif step == "height":
        n = parse_number(t)
        if n is None or n < 80 or n > 250:
            await msg.reply_text("рост в см числом (например: 163) (или /skip)")
            return True
        update_user(uid, height_cm=float(n), onboarding_step=next_step(step))

    elif step == "weight":
        n = parse_number(t)
        if n is None or n < 25 or n > 300:
            await msg.reply_text("вес в кг числом (например: 63) (или /skip)")
            return True
        update_user(uid, weight_kg=float(n), onboarding_step=next_step(step))

    elif step == "activity":
        if t in ("low", "низкая", "низк", "1"):
            update_user(uid, activity="low", onboarding_step=next_step(step))
        elif t in ("medium", "средняя", "средн", "2"):
            update_user(uid, activity="medium", onboarding_step=next_step(step))
        elif t in ("high", "высокая", "высок", "3"):
            update_user(uid, activity="high", onboarding_step=next_step(step))
        else:
            await msg.reply_text("активность: low / medium / high (или /skip)")
            return True

    elif step == "issues":
        issues_json = set_issues_from_text(text)
        update_user(uid, primary_issues_json=issues_json, onboarding_step=next_step(step))

    elif step == "prefs":
        prefs_json = set_prefs_from_text(text)
        update_user(uid, prefs_json=prefs_json, onboarding_step="done", onboarding_done=1)
    else:
        update_user(uid, onboarding_done=1, onboarding_step="done")

    u2 = get_user(uid)
    st2 = u2.get("onboarding_step") or "done"
    await onboarding_prompt(update, st2)
    return True

# =====================
# ai logging + analysis
# =====================

@dataclass
class AiItem:
    name: str
    qty: float
    unit: str
    calories: float | None
    protein: float | None
    fat: float | None
    carbs: float | None
    fiber: float | None

def _log_json_schema():
    return {
        "name": "nutrition_log",
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "qty": {"type": "number"},
                            "unit": {"type": "string"},
                            "calories": {"type": ["number", "null"]},
                            "protein": {"type": ["number", "null"]},
                            "fat": {"type": ["number", "null"]},
                            "carbs": {"type": ["number", "null"]},
                            "fiber": {"type": ["number", "null"]},
                        },
                        "required": ["name", "qty", "unit", "calories", "protein", "fat", "carbs", "fiber"],
                    },
                },
                "confidence": {"type": "number"},
                "assumptions": {"type": "array", "items": {"type": "string"}},
                "notes": {"type": "string"},
            },
            "required": ["items", "confidence", "assumptions", "notes"],
        },
    }

def _analysis_json_schema_ru():
    return {
        "name": "daily_analysis_ru",
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "headline": {"type": "string"},
                "good": {"type": "array", "items": {"type": "string"}},
                "improve": {"type": "array", "items": {"type": "string"}},
                "plan": {"type": "array", "items": {"type": "string"}},
                "warnings": {"type": "array", "items": {"type": "string"}},
                "confidence": {"type": "number"},
            },
            "required": ["headline", "good", "improve", "plan", "warnings", "confidence"],
        },
    }

def _week_analysis_json_schema_ru():
    return {
        "name": "week_analysis_ru",
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "headline": {"type": "string"},
                "patterns": {"type": "array", "items": {"type": "string"}},
                "weak_points": {"type": "array", "items": {"type": "string"}},
                "plan": {"type": "array", "items": {"type": "string"}},
                "warnings": {"type": "array", "items": {"type": "string"}},
                "confidence": {"type": "number"},
            },
            "required": ["headline", "patterns", "weak_points", "plan", "warnings", "confidence"],
        },
    }

def build_profile_hint(u: dict) -> dict:
    prefs = {}
    try:
        prefs = json.loads(u.get("prefs_json") or "{}")
    except Exception:
        prefs = {}
    prefs_list = [k for k, v in prefs.items() if v]
    issues = {}
    try:
        issues = json.loads(u.get("primary_issues_json") or "{}")
    except Exception:
        issues = {}
    issues_list = [k for k, v in issues.items() if v]

    return {
        "goal": u.get("goal"),
        "sex": u.get("sex"),
        "age": u.get("age"),
        "height_cm": u.get("height_cm"),
        "weight_kg": u.get("weight_kg"),
        "activity": u.get("activity"),
        "prefs": prefs_list,
        "primary_issues": issues_list,
    }

def ai_estimate(text: str, meal_hint: str, profile_hint: dict) -> tuple[list[AiItem], float, dict]:
    prompt = f"""
You are a nutrition diary assistant.
Task: parse the user message into items and estimate calories and macros (including fiber).

Profile (may be partial, use only if relevant):
{json.dumps(profile_hint, ensure_ascii=False)}

Rules:
- Output JSON only, matching the provided schema.
- Units: "g", "ml", "pcs", "tbsp", "tsp", "serving"
- Macros are grams; calories are kcal.
- Fiber must be realistic:
  - Do NOT output fiber=0 unless you are confident it is essentially zero (e.g., meat, eggs, oil, cheese, plain yogurt).
  - If unsure, set fiber=null (not 0) and explain in assumptions.
- If grams/ml not provided, assume a reasonable serving size and write it in assumptions.
- Use typical nutrition averages when brand is unknown.
- Keep values plausible, avoid extreme numbers.
- All calories/macros must be TOTAL for the given qty+unit (not per 1 unit).
- If unit is "pcs" and qty > 1, totals must scale with qty.
- A number applies only to the nearest food item, not to the whole list.
- If qty for an item is unclear, set qty=1 and mention it in assumptions (do NOT copy qty from previous item).
- All item names MUST be in Russian.
- Do not use Latin letters in "name". If user wrote in English, translate to Russian.
- Units MUST be only: "g", "ml", "pcs", "tbsp", "tsp", "serving" (no bottle/cup/etc).
- Confidence is 0..1.

Meal hint: "{meal_hint}"

User message:
{text}
""".strip()

    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_schema", "json_schema": _log_json_schema()},
        temperature=0.2,
    )

    raw = resp.choices[0].message.content
    data = json.loads(raw)

    items: list[AiItem] = []
    for it in data.get("items", []):
        items.append(AiItem(
            name=str(it["name"]).strip(),
            qty=float(it["qty"]),
            unit=str(it["unit"]).strip(),
            calories=it["calories"],
            protein=it["protein"],
            fat=it["fat"],
            carbs=it["carbs"],
            fiber=it["fiber"],
        ))

    meta = {
        "confidence": float(data.get("confidence", 0.0)),
        "assumptions": list(data.get("assumptions", []) or []),
        "notes": data.get("notes", ""),
        "raw_ai": data,
        "prompt_version": "ai_mode_a_ru_week_v4_subscriptions_back_targets_v1",
        "model": OPENAI_MODEL,
    }

    for it in items:
        if suspicious_zero_fiber(it.name, it.fiber, it.carbs):
            it.fiber = None
            meta["assumptions"].append(
                f"клетчатка для '{it.name}' неизвестна (ai вернул 0, но похоже на растительную еду)."
            )

    sanity_notes: list[str] = []
    clean_items: list[AiItem] = []
    for it in items:
        it2, notes = sanitize_item(it)
        clean_items.append(it2)
        sanity_notes.extend(notes)

    items = clean_items
    scale_notes = fix_totals_for_pcs(items)
    if scale_notes:
        meta["assumptions"].extend(scale_notes)

    if sanity_notes:
        meta["assumptions"].extend(sanity_notes)

    return items, meta["confidence"], meta

def ai_estimate_photo(image_bytes: bytes, image_mime: str, meal_hint: str, profile_hint: dict) -> tuple[list[AiItem], float, dict]:
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    data_url = f"data:{image_mime};base64,{b64}"

    prompt = f"""
You are a nutrition diary assistant.
Task: analyze the food photo and output items and estimated macros (including fiber).

Profile (may be partial, use only if relevant):
{json.dumps(profile_hint, ensure_ascii=False)}

Rules:
- Output JSON only, matching the provided schema.
- Units: "g", "ml", "pcs", "tbsp", "tsp", "serving"
- If quantities are unknown from photo, choose reasonable defaults and explain in assumptions.
- Fiber must be realistic:
  - Do NOT output fiber=0 unless you are confident it is essentially zero.
  - If unsure, set fiber=null and explain in assumptions.
- Keep values plausible, avoid extreme numbers.
- All calories/macros must be TOTAL for the given qty+unit (not per 1 unit).
- If unit is "pcs" and qty > 1, totals must scale with qty.
- A number applies only to the nearest food item, not to the whole list.
- If qty for an item is unclear, set qty=1 and mention it in assumptions (do NOT copy qty from previous item).
- All item names MUST be in Russian.
- Do not use Latin letters in "name". If user wrote in English, translate to Russian.
- Units MUST be only: "g", "ml", "pcs", "tbsp", "tsp", "serving" (no bottle/cup/etc).
- Confidence is 0..1.

Meal hint: "{meal_hint}"
""".strip()

    # мультимодальный ввод: текст + изображение
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
        response_format={"type": "json_schema", "json_schema": _log_json_schema()},
        temperature=0.2,
    )

    raw = resp.choices[0].message.content
    data = json.loads(raw)

    items: list[AiItem] = []
    for it in data.get("items", []):
        items.append(AiItem(
            name=str(it["name"]).strip(),
            qty=float(it["qty"]),
            unit=str(it["unit"]).strip(),
            calories=it["calories"],
            protein=it["protein"],
            fat=it["fat"],
            carbs=it["carbs"],
            fiber=it["fiber"],
        ))

    meta = {
        "confidence": float(data.get("confidence", 0.0)),
        "assumptions": list(data.get("assumptions", []) or []),
        "notes": data.get("notes", ""),
        "raw_ai": data,
        "prompt_version": "ai_mode_photo_v1",
        "model": OPENAI_MODEL,
    }

    for it in items:
        if suspicious_zero_fiber(it.name, it.fiber, it.carbs):
            it.fiber = None
            meta["assumptions"].append(
                f"клетчатка для '{it.name}' неизвестна (ai вернул 0, но похоже на растительную еду)."
            )

    sanity_notes: list[str] = []
    clean_items: list[AiItem] = []
    for it in items:
        it2, notes = sanitize_item(it)
        clean_items.append(it2)
        sanity_notes.extend(notes)

    items = clean_items
    if sanity_notes:
        meta["assumptions"].extend(sanity_notes)

    return items, meta["confidence"], meta


def ai_analyze_day_ru(profile_hint: dict, day_payload: dict) -> tuple[str, dict]:
    prompt = f"""
ты анализируешь дневник питания в приложении.
пиши только по-русски. никакого английского.

профиль (может быть неполным):
{json.dumps(profile_hint, ensure_ascii=False)}

данные за день (цифры приблизительные):
{json.dumps(day_payload, ensure_ascii=False)}

важно:
- если клетчатка по позиции неизвестна, приложение считает ее как 0 (упрощение ради удобства).

требования:
- коротко и по делу, без морали и стыда.
- дай 3–5 пунктов "что хорошо".
- дай 3–5 пунктов "что улучшить".
- дай 3–5 пунктов "план на завтра" (очень конкретно и легко).
- "предупреждения" только если действительно важно.
- выводи json строго по схеме.
""".strip()

    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_schema", "json_schema": _analysis_json_schema_ru()},
        temperature=0.3,
    )

    raw = resp.choices[0].message.content
    data = json.loads(raw)

    lines = []
    lines.append(data["headline"].strip())

    lines.append("")
    lines.append("Что хорошо:")
    for x in (data.get("good") or [])[:6]:
        lines.append(f"- {str(x).strip()}")

    lines.append("")
    lines.append("Что улучшить:")
    for x in (data.get("improve") or [])[:6]:
        lines.append(f"- {str(x).strip()}")

    lines.append("")
    lines.append("План на завтра:")
    for x in (data.get("plan") or [])[:6]:
        lines.append(f"- {str(x).strip()}")

    w = [str(x).strip() for x in (data.get("warnings") or []) if str(x).strip()]
    if w:
        lines.append("")
        lines.append("Предупреждения:")
        for x in w[:4]:
            lines.append(f"- {x}")

    return "\n".join(lines), data

def ai_analyze_week_ru(profile_hint: dict, week_payload: dict) -> tuple[str, dict]:
    prompt = f"""
ты анализируешь неделю дневника питания.
пиши только по-русски. никакого английского.

профиль (может быть неполным):
{json.dumps(profile_hint, ensure_ascii=False)}

данные за неделю (цифры приблизительные):
{json.dumps(week_payload, ensure_ascii=False)}

важно:
- если клетчатка по позиции неизвестна, приложение считает ее как 0 (упрощение ради удобства).

требования:
- коротко и по делу, без морали и стыда.
- дай 3–6 "паттернов недели".
- дай 2–5 "слабых мест".
- дай 3–6 пунктов "план на следующую неделю" (очень конкретно и легко).
- "предупреждения" только если действительно важно.
- выводи json строго по схеме.
""".strip()

    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_schema", "json_schema": _week_analysis_json_schema_ru()},
        temperature=0.3,
    )

    raw = resp.choices[0].message.content
    data = json.loads(raw)

    lines = []
    lines.append(data["headline"].strip())

    lines.append("")
    lines.append("паттерны недели:")
    for x in (data.get("patterns") or [])[:8]:
        lines.append(f"- {str(x).strip()}")

    lines.append("")
    lines.append("слабые места:")
    for x in (data.get("weak_points") or [])[:8]:
        lines.append(f"- {str(x).strip()}")

    lines.append("")
    lines.append("план на следующую неделю:")
    for x in (data.get("plan") or [])[:8]:
        lines.append(f"- {str(x).strip()}")

    w = [str(x).strip() for x in (data.get("warnings") or []) if str(x).strip()]
    if w:
        lines.append("")
        lines.append("предупреждения:")
        for x in w[:4]:
            lines.append(f"- {x}")

    return "\n".join(lines), data

# =====================
# help text
# =====================

HELP_TEXT = """
команды (без подписки):
  /start - онбординг/перезапуск
  /help - помощь
  /profile - показать профиль
  /reset_profile - сбросить профиль и пройти онбординг заново
  /skip - пропустить шаг онбординга
  /cancel - остановить онбординг
  /sub_status - статус подписки

команды (нужна подписка):
  /debugfood - переключить debug (assumptions/notes)
  /add <meal> <text> - добавить еду (meal: breakfast/lunch/dinner/snack/other)
  /today - итог за сегодня
  /analyze - анализ дня и рекомендации
  /week - статистика за 7 дней
  /week_analyze - анализ недели
  /targets - показать цели
  /set_targets <ккал> <белки> <жиры> <углеводы> [клетчатка] [чистые_угл] - установить цели
  /remember <alias> = <canonical> | <qty unit>
  /aliases - показать словарь
  /forget <alias> - удалить алиас

команды (админ):
  /reanalyze - пересчитать анализ
  /reweek_analyze - пересчитать недельный анализ
  /reset_today - удалить все записи за сегодня
  /sub_grant <user_id> <days|forever>
  /sub_revoke <user_id>

можно просто писать сообщением, тогда meal=other (но нужна подписка).
""".strip()

# =====================
# week helpers
# =====================

def week_range_last7() -> tuple[str, str]:
    end = today_date()
    start = end - timedelta(days=6)
    return start.isoformat(), end.isoformat()

def _sum_row(row, key: str) -> float:
    try:
        return float(row[key] or 0.0)
    except Exception:
        return 0.0

def fetch_week_entries(uid: int, start_date: str, end_date: str):
    with db() as conn:
        rows = conn.execute(
            """
            select id, entry_date, calories, protein, fat, carbs, fiber
            from entries
            where user_id = ?
              and entry_date >= ?
              and entry_date <= ?
            order by id asc
            """,
            (uid, start_date, end_date),
        ).fetchall()
    return rows

def compute_week_stats(rows, start_date: str, end_date: str) -> dict:
    per_day: dict[str, dict] = {}
    last_entry_id = 0
    for r in rows:
        d = r["entry_date"]
        if d not in per_day:
            per_day[d] = {"kcal": 0.0, "protein": 0.0, "fat": 0.0, "carbs": 0.0, "fiber": 0.0, "entries": 0}
        per_day[d]["kcal"] += _sum_row(r, "calories")
        per_day[d]["protein"] += _sum_row(r, "protein")
        per_day[d]["fat"] += _sum_row(r, "fat")
        per_day[d]["carbs"] += _sum_row(r, "carbs")
        per_day[d]["fiber"] += _sum_row(r, "fiber")
        per_day[d]["entries"] += 1
        try:
            last_entry_id = max(last_entry_id, int(r["id"]))
        except Exception:
            pass

    days_window = 7
    days_logged = len(per_day)

    tot = {"kcal": 0.0, "protein": 0.0, "fat": 0.0, "carbs": 0.0, "fiber": 0.0}
    for _, s in per_day.items():
        tot["kcal"] += s["kcal"]
        tot["protein"] += s["protein"]
        tot["fat"] += s["fat"]
        tot["carbs"] += s["carbs"]
        tot["fiber"] += s["fiber"]

    denom = float(days_logged) if days_logged > 0 else 1.0
    avg = {
        "kcal": tot["kcal"] / denom,
        "protein": tot["protein"] / denom,
        "fat": tot["fat"] / denom,
        "carbs": tot["carbs"] / denom,
        "fiber": tot["fiber"] / denom,
        "net_carbs": net_carbs(tot["carbs"] / denom, tot["fiber"] / denom),
    }

    min_day = None
    max_day = None
    for d, s in per_day.items():
        entry = {
            "date": d,
            "kcal": s["kcal"],
            "protein": s["protein"],
            "fat": s["fat"],
            "carbs": s["carbs"],
            "fiber": s["fiber"],
            "net_carbs": net_carbs(s["carbs"], s["fiber"]),
            "entries": s["entries"],
        }
        if min_day is None or entry["kcal"] < min_day["kcal"]:
            min_day = entry
        if max_day is None or entry["kcal"] > max_day["kcal"]:
            max_day = entry

    return {
        "start_date": start_date,
        "end_date": end_date,
        "days_window": days_window,
        "days_logged": days_logged,
        "entry_count": len(rows),
        "last_entry_id": last_entry_id,
        "per_day": per_day,
        "totals": tot,
        "avg": avg,
        "min_day": min_day,
        "max_day": max_day,
        "targets": None,
    }

def format_week_stats_text(stats: dict) -> str:
    start_date = stats["start_date"]
    end_date = stats["end_date"]
    days_logged = stats["days_logged"]
    days_window = stats["days_window"]
    avg = stats["avg"]
    min_day = stats["min_day"]
    max_day = stats["max_day"]

    lines = []
    lines.append(f"Неделя: {start_date} – {end_date}")
    lines.append("")
    lines.append(f"Дней с записями: {days_logged} из {days_window}")
    if days_logged == 0:
        lines.append("")
        lines.append("Пока нечего считать. Начни просто писать еду или отправь фото, я распознаю.")
        return "\n".join(lines)

    t = stats.get("targets")
    if t and days_logged > 0:
        lines.append("")
        lines.append("Итог за неделю (среднее в день):")
        if t.get("kcal") is not None:
            lines.append(f"- ккал: {fmt(avg['kcal'])} / {fmt(t.get('kcal'))} ({_pct(avg['kcal'], float(t.get('kcal') or 0))})")
        if t.get("protein") is not None:
            lines.append(f"- белки: {fmt(avg['protein'])} / {fmt(t.get('protein'))} ({_pct(avg['protein'], float(t.get('protein') or 0))})")
        if t.get("fat") is not None:
            lines.append(f"- жиры: {fmt(avg['fat'])} / {fmt(t.get('fat'))} ({_pct(avg['fat'], float(t.get('fat') or 0))})")
        if t.get("carbs") is not None:
            lines.append(f"- углеводы: {fmt(avg['carbs'])} / {fmt(t.get('carbs'))} ({_pct(avg['carbs'], float(t.get('carbs') or 0))})")
        if t.get("fiber") is not None:
            lines.append(f"- клетчатка: {fmt(avg['fiber'])} / {fmt(t.get('fiber'))} ({_pct(avg['fiber'], float(t.get('fiber') or 0))})")
        if t.get("net_carbs") is not None:
            lines.append(f"- чистые углеводы: {fmt(avg['net_carbs'])} / {fmt(t.get('net_carbs'))} ({_pct(avg['net_carbs'], float(t.get('net_carbs') or 0))})")

    if min_day:
        lines.append("")
        lines.append(f"Самый легкий день: {min_day['date']} ({fmt(min_day['kcal'])} ккал)")
    if max_day:
        lines.append(f"Самый калорийный: {max_day['date']} ({fmt(max_day['kcal'])} ккал)")

    return "\n".join(lines)

# =====================
# commands
# =====================

# =====================
# help text (dynamic by status)
# =====================

def build_help_text(uid: int) -> str:
    # базовые команды видны всем
    base = [
        "Доступные команды:",
        "  /start - запуск/онбординг",
        "  /help - помощь",
        "  /myid - узнать свой user_id",
        "  /profile - показать профиль",
        "  /reset_profile - сбросить профиль и пройти онбординг заново",
        "  /skip - пропустить шаг онбординга",
        "  /cancel - остановить онбординг",
        "  /sub_status - статус подписки",
        "  /pay - оформить подписку",
        " /contact <текст> - написать создателю (поддержка)",
        " /contact - режим, следующее сообщение уйдёт создателю"
    ]

    # команды только с подпиской
    sub = [
        "",
        "Команды только по подписке:",
        "  /debugfood - переключить debug (assumptions/notes)",
        "  /today - итог за сегодня",
        "  /analyze - анализ дня и рекомендации",
        "  /week - статистика за 7 дней",
        "  /week_analyze - анализ недели",
        "  /targets - показать цели",
        "  /set_targets <ккал> <белки> <жиры> <углеводы> [клетчатка] [чистые_угл] - установить цели",
        "  /set_targets keto <ккал> - автоцели для кето",
        "  /set_targets lowcarb <ккал> - автоцели для low carb"
        "  /remember <alias> = <canonical> | <qty unit>",
        "  /aliases - показать словарь",
        "  /forget <alias> - удалить алиас",
        "  /del <номер> - удалить продукт из сегодняшнего списка",
        "  /quota - сколько доступных запросов осталось",
        "",
        "При наличии подписки еду можно просто писать сообщением, например, 2 яйца, творог 100 гр",
        "так же ты можешь добавить фото, я попробую распознать",
    ]

    # админские команды
    admin = [
        "",
        "Команды (админ):",
        "  /reanalyze - пересчитать анализ",
        "  /reweek_analyze - пересчитать недельный анализ",
        "  /reset_today - удалить все записи за сегодня",
        "  /sub_grant <user_id> <days|forever> - выдать подписку",
        "  /sub_revoke <user_id> - отключить подписку",
    ]

    lines = []
    lines.extend(base)

    # показываем подписочные команды только если подписка активна (или юзер админ)
    if is_subscribed(uid):
        lines.extend(sub)
    else:
        lines.append("")
        lines.append("Чтобы получить доступ к логированию еды, нужна активная подписка.")
        lines.append("Узнать свой user_id: /myid")

    # админские команды показываем только админам
    if is_admin(uid) and ADMIN_IDS:
        lines.extend(admin)

    return "\n".join(lines)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    ensure_user(uid)
    u = get_user(uid)

    await update.effective_message.reply_text(build_help_text(uid))

    if is_onboarding_needed(u):
        step = u.get("onboarding_step") or "goal"
        update_user(uid, onboarding_step=step)
        await onboarding_prompt(update, step)
    else:
        await update.effective_message.reply_text("Если ты хочешь удалить данные профиля и пройти онбоардинг заново: /reset_profile")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    ensure_user(uid)
    u = get_user(uid)

    await update.effective_message.reply_text(HELP_TEXT)

    if is_onboarding_needed(u):
        step = u.get("onboarding_step") or "goal"
        update_user(uid, onboarding_step=step)
        await onboarding_prompt(update, step)
    else:
        await update.effective_message.reply_text("Если ты хочешь удалить данные профиля и пройти онбоардинг заново: /reset_profile")

async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    ensure_user(uid)
    u = get_user(uid)
    await update.effective_message.reply_text(profile_text(u))

async def cmd_reset_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    ensure_user(uid)
    update_user(
        uid,
        onboarding_done=0,
        onboarding_step="goal",
        goal=None,
        sex=None,
        age=None,
        height_cm=None,
        weight_kg=None,
        activity=None,
        prefs_json=json.dumps({}, ensure_ascii=False),
    )
    await update.effective_message.reply_text("Профиль сброшен. Начинаем онбординг!")
    await onboarding_prompt(update, "goal")

async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    ensure_user(uid)
    u = get_user(uid)
    if not is_onboarding_needed(u):
        await update.effective_message.reply_text("нбординг не активен.")
        return
    step = u.get("onboarding_step") or "goal"
    ns = next_step(step)
    update_user(uid, onboarding_step=ns)
    await onboarding_prompt(update, ns)

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    ensure_user(uid)
    u = get_user(uid)
    if not is_onboarding_needed(u):
        await update.effective_message.reply_text("Онбординг не активен.")
        return
    update_user(uid, onboarding_step=None)
    await update.effective_message.reply_text("Онбординг приостановлен. Продолжить можно командой /start.")

async def cmd_sub_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    ensure_user(uid)
    if is_admin(uid) and ADMIN_IDS:
        await update.effective_message.reply_text("Статус: admin (подписка не нужна).")
        return

    sub = get_subscription(uid)
    if not sub:
        await update.effective_message.reply_text("статус: нет подписки.")
        return

    status = (sub.get("status") or "inactive").lower()
    exp = sub.get("expires_at")
    active = is_subscribed(uid)

    if status != "active":
        await update.effective_message.reply_text("статус: подписка выключена.")
        return

    if not exp:
        await update.effective_message.reply_text(f"статус: {'активна' if active else 'неактивна'} (forever).")
        return

    # если есть expires_at, это может быть trial/платная подписка, но для пользователя всё равно важно "до какого"
    if status == "active" and exp:
        await update.effective_message.reply_text(
            f"статус: {'активна' if active else 'неактивна'}\n"
            f"действует до: {fmt_dt_short(exp)}"
        )
        return

    await update.effective_message.reply_text(
        f"статус: {'активна' if active else 'неактивна'}\n"
        f"истекает: {fmt_dt_short(exp)}"
    )

async def cmd_sub_grant(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny_if_not_admin(update):
        return

    args = context.args or []
    if len(args) < 2:
        await update.effective_message.reply_text("формат: /sub_grant <user_id> <days|forever>")
        return

    try:
        target = int(args[0])
    except Exception:
        await update.effective_message.reply_text("user_id должен быть числом.")
        return

    mode = args[1].strip().lower()
    expires_at = None

    if mode == "forever":
        expires_at = None
    else:
        try:
            days = int(mode)
            if days <= 0 or days > 36500:
                raise ValueError("bad days")
            expires_at = (datetime.now(TZ) + timedelta(days=days)).isoformat()
        except Exception:
            await update.effective_message.reply_text("второй аргумент: число дней или forever.")
            return

    set_subscription(target, active=True, expires_at=expires_at)
    await update.effective_message.reply_text(
        f"выдана подписка для {target}. expires_at={fmt_dt_short(expires_at)}"
    )

async def cmd_sub_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny_if_not_admin(update):
        return

    args = context.args or []
    if len(args) < 1:
        await update.effective_message.reply_text("формат: /sub_revoke <user_id>")
        return

    try:
        target = int(args[0])
    except Exception:
        await update.effective_message.reply_text("user_id должен быть числом.")
        return

    set_subscription(target, active=False, expires_at=None)
    await update.effective_message.reply_text(f"подписка отключена для {target}.")

async def cmd_debugfood(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny_if_not_subscribed(update):
        return
    uid = update.effective_user.id
    USER_DEBUG[uid] = not USER_DEBUG.get(uid, False)
    await update.effective_message.reply_text(f"debugfood = {USER_DEBUG[uid]}")

async def cmd_targets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny_if_not_subscribed(update):
        return
    uid = update.effective_user.id
    ensure_user(uid)
    t = get_targets(uid)
    await update.effective_message.reply_text(targets_text(t))

def _warn_low_fat(fat: float | None) -> str | None:
    if fat is None:
        return None
    try:
        if float(fat) < 20:
            return "Жиры ниже 20 г — это может вызвать гормональные и энергетические проблемы."
    except Exception:
        pass
    return None


def _warn_dangerous_targets(diet: str | None, kcal: float | None) -> list[str]:
    warnings = []
    if not diet or kcal is None:
        return warnings

    try:
        kcal = float(kcal)
    except Exception:
        return warnings

    if diet == "keto":
        if kcal < 800:
            warnings.append(
                "Кето с калориями ниже 800 — высокий риск срывов, слабости и проблем со здоровьем."
            )
        elif kcal < 1000:
            warnings.append(
                "Кето ниже 1000 ккал может быть слишком жёстким режимом. Следи за самочувствием."
            )

    return warnings


def _validate_non_negative(**values) -> str | None:
    for name, val in values.items():
        if val is None:
            continue
        try:
            if float(val) < 0:
                return name
        except Exception:
            continue
    return None


async def cmd_set_targets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny_if_not_subscribed(update):
        return

    uid = update.effective_user.id
    ensure_user(uid)
    u = get_user(uid)

    args = context.args or []
    if not args:
        await update.effective_message.reply_text(
            "форматы:\n"
            "1) вручную:\n"
            "   /set_targets <ккал> <белки> <жиры> <углеводы> [клетчатка] [чистые_угл]\n"
            "   пример: /set_targets 1200 90 70 25 18 20\n\n"
            "2) авто:\n"
            "   /set_targets keto <ккал>\n"
            "   /set_targets lowcarb <ккал>\n"
        )
        return

    def _to_float(x: str) -> float:
        return float((x or "").replace(",", "."))

    diet = _diet_norm(args[0])

    # режим авто
    if diet:
        if len(args) < 2:
            await update.effective_message.reply_text("формат: /set_targets keto 1200")
            return
        try:
            kcal = _to_float(args[1])
        except Exception:
            await update.effective_message.reply_text("не смогла прочитать ккал. пример: /set_targets keto 1200")
            return

        if kcal < 200 or kcal > 6000:
            await update.effective_message.reply_text("ккал выглядит странно. поставь число 200..6000.")
            return

        weight = u.get("weight_kg")
        try:
            weight = float(weight) if weight is not None else None
        except Exception:
            weight = None

        try:
            t = calc_targets_auto(diet=diet, kcal=kcal, weight_kg=weight)
        except ValueError:
            await update.effective_message.reply_text(
                "Авторасчёт не вышел: при таких ккал жиры получаются <= 0.\n"
                "подними ккал или в профиле укажи адекватный вес."
            )
            return

        warnings = []

        w = _warn_low_fat(t["fat"])
        if w:
            warnings.append(w)

        warnings.extend(_warn_dangerous_targets(t["diet"], t["kcal"]))

        set_targets(
            uid,
            t["kcal"],
            t["protein"],
            t["fat"],
            t["carbs"],
            t["fiber"],
            t["net_carbs"],
            diet=t["diet"],
        )
        text = "Хорошо, цели сохранены.\n\n" + targets_text(get_targets(uid))

        if warnings:
            text += "\n⚠️ Внимание:\n"
            for w in warnings:
                text += f"- {w}\n"

        await update.effective_message.reply_text(text)

        return

    # режим вручную (старый)
    if len(args) < 4:
        await update.effective_message.reply_text(
            "Формат:\n"
            "/set_targets <ккал> <белки> <жиры> <углеводы> [клетчатка] [чистые_угл]\n"
            "пример:\n"
            "/set_targets 1200 90 70 25 18 20\n"
            "или авто:\n"
            "/set_targets keto 1200"
        )
        return

    try:
        kcal = _to_float(args[0])
        protein = _to_float(args[1])
        fat = _to_float(args[2])
        carbs = _to_float(args[3])
        fiber = _to_float(args[4]) if len(args) >= 5 else None
        netc = _to_float(args[5]) if len(args) >= 6 else None
    except Exception:
        await update.effective_message.reply_text("Не смогла прочитать числа. используй формат типа: 1200 90 70 25 18 20")
        return

    bad = _validate_non_negative(
        kcal=kcal,
        protein=protein,
        fat=fat,
        carbs=carbs,
        fiber=fiber,
        net_carbs=netc,
    )
    if bad:
        await update.effective_message.reply_text(
            f"Нельзя задать отрицательное значение для '{bad}'."
        )
        return

    if kcal < 200 or kcal > 6000:
        await update.effective_message.reply_text("Ккал выглядит странно. поставь число 200..6000.")
        return

    warnings = []

    w = _warn_low_fat(fat)
    if w:
        warnings.append(w)

    warnings.extend(_warn_dangerous_targets(None, kcal))

    # при ручном вводе diet сбрасываем, чтобы не было “режим keto” с чужими макросами
    set_targets(uid, kcal, protein, fat, carbs, fiber, netc, diet=None)
    text = "Хорошо, цели сохранены.\n\n" + targets_text(get_targets(uid))

    if warnings:
        text += "\n⚠️ Внимание:\n"
        for w in warnings:
            text += f"- {w}\n"

    await update.effective_message.reply_text(text)


async def cmd_remember(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny_if_not_subscribed(update):
        return

    uid = update.effective_user.id
    ensure_user(uid)

    raw = (update.effective_message.text or "").strip()
    body = raw[len("/remember"):].strip()
    if not body:
        await update.effective_message.reply_text("формат: /remember <alias> = <canonical> | <qty unit>")
        return

    if "=" not in body:
        await update.effective_message.reply_text("нужен знак '='. пример: /remember сережа помидорки = помидоры черри | 100 g")
        return

    left, right = body.split("=", 1)
    alias = left.strip()
    right = right.strip()

    canonical = right
    qty = None
    unit = None

    if "|" in right:
        canonical_part, qty_part = right.split("|", 1)
        canonical = canonical_part.strip()
        qty, unit = _parse_qty_unit(qty_part.strip())

    try:
        upsert_alias(uid, alias_text=alias, canonical=canonical, default_qty=qty, default_unit=unit)
    except Exception as e:
        await update.effective_message.reply_text(f"не получилось сохранить: {type(e).__name__}: {str(e)[:200]}")
        return

    if qty is not None and unit:
        await update.effective_message.reply_text(f"запомнила: '{_norm_alias(alias)}' -> '{canonical}' (по умолчанию {qty} {unit})")
    else:
        await update.effective_message.reply_text(f"запомнила: '{_norm_alias(alias)}' -> '{canonical}'")

async def cmd_aliases(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny_if_not_subscribed(update):
        return

    uid = update.effective_user.id
    ensure_user(uid)

    items = list_aliases(uid)
    if not items:
        await update.effective_message.reply_text("словарь пуст. добавь: /remember <alias> = <canonical>")
        return

    lines = ["словарь:"]
    for a in items[:50]:
        alias = a["alias_text"]
        canonical = a["canonical_name"]
        dq = a.get("default_qty")
        du = a.get("default_unit")
        if dq is not None and du:
            lines.append(f"- {alias} = {canonical} | {dq} {du}")
        else:
            lines.append(f"- {alias} = {canonical}")

    if len(items) > 50:
        lines.append(f"...и еще {len(items) - 50}")

    await update.effective_message.reply_text("\n".join(lines))

async def cmd_forget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny_if_not_subscribed(update):
        return

    uid = update.effective_user.id
    ensure_user(uid)

    args = context.args or []
    if not args:
        await update.effective_message.reply_text("формат: /forget <alias>")
        return

    alias = " ".join(args).strip()
    n = delete_alias(uid, alias)
    if n > 0:
        await update.effective_message.reply_text(f"удалено: '{_norm_alias(alias)}'")
    else:
        await update.effective_message.reply_text("не нашла такой алиас в словаре.")

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny_if_not_subscribed(update):
        return

    uid = update.effective_user.id
    ensure_user(uid)
    u = get_user(uid)

    if is_onboarding_needed(u) and (u.get("onboarding_step") not in (None, "done")):
        await update.effective_message.reply_text("сначала закончим онбординг. можно /skip или /cancel.")
        return

    args = context.args or []
    if not args:
        await update.effective_message.reply_text("использование: /add <meal> <text>")
        return

    meal = args[0].strip().lower()
    if meal not in ("breakfast", "lunch", "dinner", "snack", "other"):
        await update.effective_message.reply_text("meal должен быть: breakfast/lunch/dinner/snack/other")
        return

    text = " ".join(args[1:]).strip()
    if not text:
        await update.effective_message.reply_text("дай текст еды после meal.")
        return

    await handle_log(update, context, u, meal, text)

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny_if_not_subscribed(update):
        return

    uid = update.effective_user.id
    ensure_user(uid)
    d = today_str()

    with db() as conn:
        rows = conn.execute(
            """
            select id, meal, item_name, qty, unit, calories, protein, fat, carbs, fiber, confidence
            from entries
            where user_id = ? and entry_date = ?
            order by id asc
            """,
            (uid, d),
        ).fetchall()

    if not rows:
        await update.effective_message.reply_text("За сегодня нет записей. Чтобы добавить запись, просто отправь сообщение или фото в чат.")
        return

    total_kcal = total_p = total_f = total_c = 0.0
    total_fiber = 0.0

    lines = [f"Сегодня ({d}):"]

    from collections import defaultdict

    src_kcal = defaultdict(float)
    src_p = defaultdict(float)
    src_f = defaultdict(float)
    src_c = defaultdict(float)

    for i, r in enumerate(rows, start=1):
        kcal = float(r["calories"] or 0.0)
        p = float(r["protein"] or 0.0)
        f = float(r["fat"] or 0.0)
        c = float(r["carbs"] or 0.0)

        fib = float(r["fiber"] or 0.0)
        total_kcal += kcal
        total_p += p
        total_f += f
        total_c += c
        total_fiber += fib

        net = net_carbs(c, fib)

        # conf = r["confidence"]
        # conf_txt = f" (увер. {fmt(conf)})" if conf is not None else ""

        lines.append(
            f"{i}) [{meal_to_ru(r['meal'])}] {r['item_name']} ({r['qty']} {unit_to_ru(r['unit'])}): "
            f"{fmt(kcal)} ккал, белки {fmt(p)} г, жиры {fmt(f)} г, "
            f"углеводы {fmt(c)} г (клетч. {fmt(fib)} г, чистые {fmt(net)} г)"
        )

        name = r["item_name"]

        if kcal > 0:
            src_kcal[name] += kcal
        if p > 0:
            src_p[name] += p
        if f > 0:
            src_f[name] += f
        if c > 0:
            src_c[name] += c

    lines.append("")
    total_net = net_carbs(total_c, total_fiber)

    t = get_targets(uid)
    if t:
        lines.append("Итог за день:")
        if t.get("kcal") is not None:
            lines.append(f"- ккал: {fmt(total_kcal)} / {fmt(t.get('kcal'))} ({_pct(total_kcal, float(t.get('kcal') or 0))})")
            src = top_sources(src_kcal, "ккал")
            if src:
                lines.append(f"  источники: {src}")
                lines.append(f"")

        if t.get("protein") is not None:
            lines.append(
                f"- белки: {fmt(total_p)} / {fmt(t.get('protein'))} ({_pct(total_p, float(t.get('protein') or 0))})")
            src = top_sources(src_p, "г")
            if src:
                lines.append(f"  источники: {src}")
                lines.append(f"")

        if t.get("fat") is not None:
            lines.append(f"- жиры: {fmt(total_f)} / {fmt(t.get('fat'))} ({_pct(total_f, float(t.get('fat') or 0))})")
            src = top_sources(src_f, "г")
            if src:
                lines.append(f"  источники: {src}")
                lines.append(f"")

        if t.get("carbs") is not None:
            lines.append(
                f"- углеводы: {fmt(total_c)} / {fmt(t.get('carbs'))} ({_pct(total_c, float(t.get('carbs') or 0))})")
            src = top_sources(src_c, "г")
            if src:
                lines.append(f"  источники: {src}")
                lines.append(f"")

        if t.get("fiber") is not None:
            lines.append(f"- клетчатка: {fmt(total_fiber)} / {fmt(t.get('fiber'))} ({_pct(total_fiber, float(t.get('fiber') or 0))})")
        if t.get("net_carbs") is not None:
            lines.append(f"- чистые углеводы: {fmt(total_net)} / {fmt(t.get('net_carbs'))} ({_pct(total_net, float(t.get('net_carbs') or 0))})")
        lines.append("")

    await update.effective_message.reply_text("\n".join(lines))

async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny_if_not_subscribed(update):
        return

    uid = update.effective_user.id
    ensure_user(uid)

    args = context.args or []
    if not args:
        await update.effective_message.reply_text(
            "формат: /del <номер из списка сегодня>\n"
            "пример: /del 1"
        )
        return

    try:
        idx = int(args[0])
        if idx <= 0:
            raise ValueError
    except Exception:
        await update.effective_message.reply_text("номер должен быть положительным числом.")
        return

    d = today_str()

    with db() as conn:
        rows = conn.execute(
            """
            select id, meal, item_name, qty, unit
            from entries
            where user_id=? and entry_date=?
            order by id asc
            """,
            (uid, d),
        ).fetchall()

        if not rows:
            await update.effective_message.reply_text("за сегодня пусто.")
            return

        if idx > len(rows):
            await update.effective_message.reply_text(
                f"нет пункта {idx}. всего записей: {len(rows)}"
            )
            return

        row = rows[idx - 1]
        conn.execute("delete from entries where id=?", (row["id"],))
        conn.execute(
            "delete from daily_summaries where user_id=? and entry_date=?",
            (uid, d),
        )
        conn.commit()

    await update.effective_message.reply_text(
        "удалено:\n"
        f"[{meal_to_ru(row['meal'])}] {row['item_name']} ({row['qty']} {unit_to_ru(row['unit'])})"
    )


async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny_if_not_subscribed(update):
        return
    await run_analyze(update, force=False)

async def cmd_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not BOT_USERNAME:
        await update.effective_message.reply_text(
            "В .env не задан BOT_USERNAME. Добавь BOT_USERNAME=имя_бота_без_@"
        )
        return

    start_url = f"https://t.me/{BOT_USERNAME}?start=from_invite"
    pay_url = f"https://t.me/{BOT_USERNAME}?start=pay"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Start", url=start_url)],
        [InlineKeyboardButton("Оплатить / подписка", url=pay_url)],
    ])

    await update.effective_message.reply_text(
        "Вот сообщение, которое можно переслать другу:\n"
        "Нажми Start, пройди короткую настройку, и просто пиши еду текстом или фото.",
        reply_markup=kb
    )


async def cmd_reanalyze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny_if_not_admin(update):
        return
    await run_analyze(update, force=True)

async def run_analyze(update: Update, force: bool) -> None:
    uid = update.effective_user.id
    ensure_user(uid)
    u = get_user(uid)
    d = today_str()
    msg = update.effective_message

    with db() as conn:
        rows = conn.execute(
            """
            select id, meal, item_name, qty, unit, calories, protein, fat, carbs, fiber
            from entries
            where user_id = ? and entry_date = ?
            order by id asc
            """,
            (uid, d),
        ).fetchall()

    if not rows:
        await msg.reply_text("за сегодня пусто, анализировать нечего.")
        return

    entry_count = len(rows)
    last_entry_id = int(rows[-1]["id"])

    with db() as conn:
        cached = conn.execute(
            """
            select entry_count, last_entry_id, analysis_text
            from daily_summaries
            where user_id=? and entry_date=?
            """,
            (uid, d),
        ).fetchone()

    if cached and not force:
        if int(cached["entry_count"]) == entry_count and int(cached["last_entry_id"]) == last_entry_id:
            await msg.reply_text(cached["analysis_text"])
            return

    total = {"kcal": 0.0, "protein": 0.0, "fat": 0.0, "carbs": 0.0, "fiber": 0.0}
    items = []

    for r in rows:
        kcal = float(r["calories"] or 0.0)
        p = float(r["protein"] or 0.0)
        f = float(r["fat"] or 0.0)
        c = float(r["carbs"] or 0.0)
        fib = float(r["fiber"] or 0.0)

        total["kcal"] += kcal
        total["protein"] += p
        total["fat"] += f
        total["carbs"] += c
        total["fiber"] += fib

        items.append({
            "прием": meal_to_ru(r["meal"]),
            "название": r["item_name"],
            "количество": r["qty"],
            "единица": r["unit"],
            "ккал": round(kcal, 1),
            "белки": round(p, 1),
            "жиры": round(f, 1),
            "углеводы": round(c, 1),
            "клетчатка": round(fib, 1),
            "чистые_углеводы": round(net_carbs(c, fib), 1),
        })

    total_net = net_carbs(total["carbs"], total["fiber"])

    day_payload = {
        "дата": d,
        "итоги": {
            "ккал": round(total["kcal"], 1),
            "белки": round(total["protein"], 1),
            "жиры": round(total["fat"], 1),
            "углеводы": round(total["carbs"], 1),
            "клетчатка": round(total["fiber"], 1),
            "чистые_углеводы": round(total_net, 1),
        },
        "заметка": "если клетчатка по позиции неизвестна, приложение считает ее как 0",
        "продукты": items,
    }

    t = get_targets(uid)
    if t:
        day_payload["цели"] = {
            "ккал": t.get("kcal"),
            "белки": t.get("protein"),
            "жиры": t.get("fat"),
            "углеводы": t.get("carbs"),
            "клетчатка": t.get("fiber"),
            "чистые_углеводы": t.get("net_carbs"),
        }

    profile_hint = build_profile_hint(u)

    if await deny_if_rate_limited(update, uid):
        return

    try:
        analysis_text, analysis_json = ai_analyze_day_ru(profile_hint, day_payload)
    except Exception as e:
        await msg.reply_text(f"анализ не получился: {type(e).__name__}: {str(e)[:200]}")
        return

    now = now_iso()
    with db() as conn:
        conn.execute(
            """
            insert into daily_summaries(user_id, entry_date, entry_count, last_entry_id, analysis_text, analysis_json, created_at, updated_at)
            values (?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(user_id, entry_date) do update set
              entry_count=excluded.entry_count,
              last_entry_id=excluded.last_entry_id,
              analysis_text=excluded.analysis_text,
              analysis_json=excluded.analysis_json,
              updated_at=excluded.updated_at
            """,
            (
                uid, d, entry_count, last_entry_id,
                analysis_text,
                json.dumps(analysis_json, ensure_ascii=False),
                now, now
            ),
        )
        conn.commit()

    await msg.reply_text(analysis_text)

async def cmd_reset_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny_if_not_admin(update):
        return

    uid = update.effective_user.id
    d = today_str()
    with db() as conn:
        conn.execute("delete from entries where user_id=? and entry_date=?", (uid, d))
        conn.execute("delete from daily_summaries where user_id=? and entry_date=?", (uid, d))
        conn.commit()
    await update.effective_message.reply_text("сегодняшние записи удалены.")

# =====================
# week commands
# =====================

async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny_if_not_subscribed(update):
        return

    uid = update.effective_user.id
    ensure_user(uid)

    start_date, end_date = week_range_last7()
    rows = fetch_week_entries(uid, start_date, end_date)
    stats = compute_week_stats(rows, start_date, end_date)
    stats["targets"] = get_targets(uid)

    await update.effective_message.reply_text(format_week_stats_text(stats))

async def cmd_week_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny_if_not_subscribed(update):
        return
    await run_week_analyze(update, force=False)

async def cmd_reweek_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny_if_not_admin(update):
        return
    await run_week_analyze(update, force=True)

async def run_week_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE, force: bool = False) -> None:
    uid = update.effective_user.id
    ensure_user(uid)
    u = get_user(uid)
    msg = update.effective_message

    start_date, end_date = week_range_last7()
    rows = fetch_week_entries(uid, start_date, end_date)
    stats = compute_week_stats(rows, start_date, end_date)
    stats["targets"] = get_targets(uid)

    if stats["days_logged"] == 0:
        await msg.reply_text(format_week_stats_text(stats))
        return

    with db() as conn:
        cached = conn.execute(
            """
            select days_logged, entry_count, last_entry_id, analysis_text
            from weekly_summaries
            where user_id=? and start_date=? and end_date=?
            """,
            (uid, start_date, end_date),
        ).fetchone()

    if cached and not force:
        if (
            int(cached["days_logged"]) == int(stats["days_logged"])
            and int(cached["entry_count"]) == int(stats["entry_count"])
            and int(cached["last_entry_id"]) == int(stats["last_entry_id"])
        ):
            await msg.reply_text(cached["analysis_text"])
            return

    avg = stats["avg"]
    week_payload = {
        "период": {"start": start_date, "end": end_date},
        "окно_дней": stats["days_window"],
        "дней_с_записями": stats["days_logged"],
        "среднее_за_день": {
            "ккал": round(avg["kcal"], 1),
            "белки": round(avg["protein"], 1),
            "жиры": round(avg["fat"], 1),
            "углеводы": round(avg["carbs"], 1),
            "клетчатка": round(avg["fiber"], 1),
            "чистые_углеводы": round(avg["net_carbs"], 1),
        },
        "самый_легкий_день": stats["min_day"],
        "самый_калорийный_день": stats["max_day"],
        "заметка": "если клетчатка по позиции неизвестна, приложение считает ее как 0",
    }

    t = get_targets(uid)
    if t:
        week_payload["цели"] = {
            "ккал": t.get("kcal"),
            "белки": t.get("protein"),
            "жиры": t.get("fat"),
            "углеводы": t.get("carbs"),
            "клетчатка": t.get("fiber"),
            "чистые_углеводы": t.get("net_carbs"),
        }

    profile_hint = build_profile_hint(u)

    if await deny_if_rate_limited(update, uid):
        return

    try:
        analysis_text, analysis_json = ai_analyze_week_ru(profile_hint, week_payload)
    except Exception as e:
        await msg.reply_text(f"Анализ недели не получился: {type(e).__name__}: {str(e)[:200]}")
        return

    now = now_iso()
    with db() as conn:
        conn.execute(
            """
            insert into weekly_summaries(
                user_id, start_date, end_date, days_window, days_logged, entry_count, last_entry_id,
                analysis_text, analysis_json, created_at, updated_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(user_id, start_date, end_date) do update set
              days_window=excluded.days_window,
              days_logged=excluded.days_logged,
              entry_count=excluded.entry_count,
              last_entry_id=excluded.last_entry_id,
              analysis_text=excluded.analysis_text,
              analysis_json=excluded.analysis_json,
              updated_at=excluded.updated_at
            """,
            (
                uid, start_date, end_date,
                stats["days_window"], stats["days_logged"], stats["entry_count"], stats["last_entry_id"],
                format_week_stats_text(stats) + "\n\n" + analysis_text,
                json.dumps(analysis_json, ensure_ascii=False),
                now, now
            ),
        )
        conn.commit()

    await msg.reply_text(format_week_stats_text(stats) + "\n\n" + analysis_text)

# =====================
# core handler
# =====================

async def handle_log(update: Update, context: ContextTypes.DEFAULT_TYPE, u: dict, meal: str, text: str) -> None:
    uid = u["user_id"]
    d = today_str()
    debug = is_debug(uid)

    if await deny_if_rate_limited(update, uid):
        return

    replaced_text, alias_notes = apply_aliases_to_text(uid, text)
    profile_hint = build_profile_hint(u)

    try:
        items, confidence, meta = ai_estimate(text=replaced_text, meal_hint=meal, profile_hint=profile_hint)
    except Exception as e:
        await update.effective_message.reply_text(f"ai сломался: {type(e).__name__}: {str(e)[:200]}")
        return

    # если пользователь не указал количество
    if not user_provided_qty(text):
        # проверяем только одиночный продукт
        if len(items) == 1:
            it = items[0]
            suggestion = suggest_portion(it.name)

            msg = update.effective_message

            if suggestion:
                qty, unit = suggestion
                await msg.reply_text(
                    f"Ты указала продукт без количества: {it.name}.\n"
                    f"Какое количество учитывать?\n\n"
                    f"Напиши число (например: 100 г)\n"
                    f"или ответь «стандарт» — я возьму {qty} {unit}."
                )
            else:
                await msg.reply_text(
                    f"Ты указала продукт без количества: {it.name}.\n"
                    f"Напиши количество, например: 100 г или 1 шт."
                )

            # сохраняем контекст ожидания
            context.user_data["await_qty"] = {
                "meal": meal,
                "text": text,
                "item_name": it.name,
            }
            return

    if alias_notes:
        meta["assumptions"].extend(alias_notes)
        meta["notes"] = (meta.get("notes") or "") + (" | aliases applied" if meta.get("notes") else "aliases applied")

    if not items:
        await update.effective_message.reply_text("Я не знаю эту команду, попробуй что-нибудь другое. Чтобы увидеть мои возможности, набери /help")
        return

    created_at = now_iso()
    meta_json = json.dumps(meta, ensure_ascii=False)

    with db() as conn:
        for it in items:
            conn.execute(
                """
                insert into entries(
                    user_id, entry_date, meal, raw_text,
                    item_name, qty, unit,
                    calories, protein, fat, carbs, fiber,
                    confidence, meta_json, created_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uid, d, meal, text,
                    it.name, it.qty, it.unit,
                    it.calories, it.protein, it.fat, it.carbs, it.fiber,
                    confidence, meta_json, created_at
                ),
            )
        conn.commit()

    lines = [f"добавлено ({meal_to_ru(meal)}):"]

    for it in items:
        fib = float(it.fiber or 0.0)
        net = net_carbs(it.carbs, fib)

        lines.append(
            f"- {it.name} ({it.qty} {unit_to_ru(it.unit)}): {fmt(it.calories)} ккал, "
            f"белки {fmt(it.protein)} г, жиры {fmt(it.fat)} г, углеводы {fmt(it.carbs)} г "
            f"(клетч. {fmt(fib)} г, чистые {fmt(net)} г)"
        )

    if debug:
        lines.append("")
        lines.append(f"debug: уверенность={fmt(confidence)}")
        if replaced_text != text:
            lines.append(f"debug: alias_text -> {replaced_text}")
        assumptions = meta.get("assumptions") or []
        notes = meta.get("notes") or ""
        if assumptions:
            lines.append("assumptions:")
            for a in assumptions[:12]:
                lines.append(f"  - {a}")
        if notes:
            lines.append(f"notes: {notes[:500]}")
        lines.append(f"profile_hint: {json.dumps(profile_hint, ensure_ascii=False)}")

    await update.effective_message.reply_text("\n".join(lines))

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    ensure_user(uid)

    text = (update.message.text or "").strip()
    if not text:
        return

    # если пользователь в режиме "написать создателю"
    if context.user_data.get("await_creator_msg"):
        context.user_data["await_creator_msg"] = False
        if not CREATOR_ID:
            await update.effective_message.reply_text("Создатель не настроен.")
            return
        await _send_to_creator(update, context, text)
        return



    # allow payment proof without subscription
    if not is_subscribed(uid):
        pending = get_pending_payment_request(uid)

        # если есть pending-заявка и человек пишет “чек/оплат/скрин/#”
        if pending and (
            "#" in text
            or "чек" in text.lower()
            or "оплат" in text.lower()
            or "скрин" in text.lower()
            or "pix" in text.lower()
        ):
            attach_payment_proof(uid, pending["id"], proof_text=text[:2000], proof_file_id=None)
            await update.effective_message.reply_text(
                f"Принято. Заявка #{pending['id']}.\n"
                "Я передала администратору, он подтвердит оплату и откроет доступ."
            )
            await notify_admins(
                context,
                f"payment proof (text)\nuser_id={uid}\nrequest=#{pending['id']}\n\n{text[:1500]}"
            )
            return

        # иначе, без подписки обычный текст не принимаем
        await update.effective_message.reply_text(
            "Доступ закрыт: нужна подписка.\n"
            "Если ты уже оплатила, пришли сообщение со словом 'чек' или номером заявки (#...), или фото чека."
        )
        return

    # ожидание количества
    pending = context.user_data.get("await_qty")
    if pending:
        qty_text = (update.message.text or "").strip().lower()

        if qty_text in ("стандарт", "default"):
            suggestion = suggest_portion(pending["item_name"])
            if not suggestion:
                await update.effective_message.reply_text("Не знаю стандартную порцию, напиши количество числом.")
                return
            qty, unit = suggestion
            new_text = f"{pending['item_name']} {qty} {unit}"
        else:
            new_text = f"{pending['item_name']} {qty_text}"

        context.user_data.pop("await_qty", None)

        await handle_log(update, context, get_user(update.effective_user.id), pending["meal"], new_text)
        return

    u = get_user(uid)

    text = (update.message.text or "").strip()
    if not text:
        return

    if is_onboarding_needed(u) and (u.get("onboarding_step") not in (None, "done")):
        consumed = await handle_onboarding_input(update, u, text)
        if consumed:
            return

    meal = infer_meal_by_time(datetime.now(TZ))
    await handle_log(update, context, u, meal, text)

# === весь код сохранён, изменения помечены комментариями "# UX" ===

# ... [код идентичен твоей версии до секции commands] ...


# =====================
# commands
# =====================

async def cmd_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    ensure_user(uid)

    if not CREATOR_ID:
        await update.effective_message.reply_text("создатель не настроен. админы не заданы.")
        return

    text = (update.effective_message.text or "").strip()
    body = text[len("/contact"):].strip()

    # если текст сразу не дали, включаем режим: следующее сообщение уйдёт создателю
    if not body:
        context.user_data["await_creator_msg"] = True
        await update.effective_message.reply_text(
            "ок, напиши следующим сообщением, что передать создателю. "
            "я перешлю 1:1."
        )
        return

    await _send_to_creator(update, context, body)


async def _send_to_creator(update: Update, context: ContextTypes.DEFAULT_TYPE, body: str) -> None:
    uid = update.effective_user.id
    user = update.effective_user

    sender = f"{user.full_name} (@{user.username})" if user.username else user.full_name

    await context.bot.send_message(
        chat_id=CREATOR_ID,
        text=(
            "Cообщение автору бота:\n"
            f"- от: {sender}\n"
            f"- user_id: {uid}\n"
            f"- текст: {body}"
        )
    )

    await update.effective_message.reply_text("Отправлено автору бота. Если он жив, ответит.")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    ensure_user(uid)
    u = get_user(uid)

    msg = update.effective_message

    # UX: новый первый экран
    await msg.reply_text(
        "Привет! 👋\n"
        "Я помогу вести дневник питания и считать кбжу без заморочек.\n\n"
        "Можно просто написать, например:\n"
        "2 яйца и огурец"
    )

    await msg.reply_text(
        "Я:\n"
        "— Запомню, что ты ешь\n"
        "— Посчитаю итоги за день и неделю\n"
        "— Подскажу, что улучшить"
    )

    await msg.reply_text(
        "У меня есть пробный период три дня, а потом нужно будет оформить подписку, если ты захочешь продолжать пользоваться мной."
    )

    await msg.reply_text(
        "Проверить статус: /sub_status\n" 
        "Купить подписку: /pay\n"
        "Что я умею: /help"
    )

    if is_onboarding_needed(u):
        await msg.reply_text(
            "Давай быстро настроим профиль,\n"
            "чтобы расчёты были точнее.\n"
            "Можно пропускать шаги."
        )
        step = u.get("onboarding_step") or "goal"
        update_user(uid, onboarding_step=step)
        await onboarding_prompt(update, step)
    else:
        await msg.reply_text(
            "У тебя уже есть профиль 👍\n"
            "если хочешь начать заново — /reset_profile\n"
            "справка по командам — /help"
        )


# UX: новая команда /myid
async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    ensure_user(uid)

    if is_admin(uid) and ADMIN_IDS:
        await update.effective_message.reply_text(
            f"Твой user_id: {uid}\n"
            "Статус: admin"
        )
        return

    await update.effective_message.reply_text(
        f"Твой user_id: {uid}\n\n"
        "Передай его администратору,\n"
        "чтобы получить доступ."
    )


# === дальше код БЕЗ ИЗМЕНЕНИЙ ===
# cmd_profile
# cmd_reset_profile
# cmd_skip
# cmd_cancel
# cmd_sub_status
# cmd_sub_grant
# cmd_sub_revoke
# cmd_debugfood
# cmd_remember
# cmd_aliases
# cmd_forget
# cmd_add
# cmd_today
# cmd_analyze
# cmd_reanalyze
# cmd_reset_today
# cmd_week
# cmd_week_analyze
# cmd_reweek_analyze
# handle_log
# on_text
# main


def main() -> None:
    init_db()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("quota", cmd_quota))

    # UX
    app.add_handler(CommandHandler("myid", cmd_myid))

    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("reset_profile", cmd_reset_profile))
    app.add_handler(CommandHandler("skip", cmd_skip))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    app.add_handler(CommandHandler("sub_status", cmd_sub_status))
    app.add_handler(CommandHandler("sub_grant", cmd_sub_grant))
    app.add_handler(CommandHandler("sub_revoke", cmd_sub_revoke))

    app.add_handler(CommandHandler("debugfood", cmd_debugfood))
    app.add_handler(CommandHandler("remember", cmd_remember))
    app.add_handler(CommandHandler("aliases", cmd_aliases))
    app.add_handler(CommandHandler("forget", cmd_forget))

    app.add_handler(CommandHandler("del", cmd_del))

    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("analyze", cmd_analyze))
    app.add_handler(CommandHandler("reanalyze", cmd_reanalyze))
    app.add_handler(CommandHandler("reset_today", cmd_reset_today))

    app.add_handler(CommandHandler("targets", cmd_targets))
    app.add_handler(CommandHandler("set_targets", cmd_set_targets))

    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("week_analyze", cmd_week_analyze))
    app.add_handler(CommandHandler("reweek_analyze", cmd_reweek_analyze))

    app.add_handler(CommandHandler("pay", cmd_pay))
    app.add_handler(CommandHandler("paid", cmd_paid))  # админ

    app.add_handler(CallbackQueryHandler(on_callback))

    app.add_handler(CommandHandler("link", cmd_link))

    app.add_handler(CommandHandler("admin_stats", cmd_admin_stats))
    app.add_handler(CommandHandler("admin_users", cmd_admin_users))
    app.add_handler(CommandHandler("admin_user", cmd_admin_user))

    app.add_handler(CommandHandler("contact", cmd_contact))

    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, on_photo))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
