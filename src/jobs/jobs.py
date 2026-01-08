# src/jobs.py
from __future__ import annotations

from datetime import datetime, timedelta, time
from typing import List, Dict, Any

from telegram.ext import ContextTypes

from src.config import TZ
from src.db import db, today_str, get_targets
from src.subscriptions import is_subscribed
from src.ai import ai_daily_analysis_ru
from src.profile import build_profile_hint
from src.jobs.notifications import (
    ensure_notify_tables,
    list_daily_recipients,
    list_weekly_recipients,
    should_send_daily,
    should_send_weekly,
)

# If you want per-user times later, you'd schedule more granular checks.
DAILY_JOB_NAME = "daily_report"
WEEKLY_JOB_NAME = "weekly_report"


def setup_jobs(application) -> None:
    """Call once on startup (before run_polling)."""
    ensure_notify_tables()

    jq = application.job_queue
    # We'll schedule checks every minute and send only to users whose HH:MM matches "now".
    jq.run_repeating(job_tick_daily, interval=60, first=10, name="tick_daily")
    jq.run_repeating(job_tick_weekly, interval=60, first=20, name="tick_weekly")


def _now_hhmm() -> str:
    now = datetime.now(TZ)
    return now.strftime("%H:%M")


def _today_iso() -> str:
    return datetime.now(TZ).date().isoformat()


def _week_key() -> str:
    # ISO year-week, e.g., 2026-W02
    d = datetime.now(TZ).date()
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


async def job_tick_daily(context: ContextTypes.DEFAULT_TYPE) -> None:
    hhmm = _now_hhmm()
    recipients = list_daily_recipients(hhmm)
    if not recipients:
        return

    day_iso = _today_iso()
    for uid in recipients:
        # de-dupe per day
        if not should_send_daily(DAILY_JOB_NAME, uid, day_iso):
            continue

        text = build_daily_card(uid, day_iso)
        await context.bot.send_message(chat_id=uid, text=text)


async def job_tick_weekly(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now(TZ)
    # Sunday only
    if now.weekday() != 6:
        return

    hhmm = _now_hhmm()
    recipients = list_weekly_recipients(hhmm)
    if not recipients:
        return

    week_key = _week_key()
    for uid in recipients:
        if not should_send_weekly(WEEKLY_JOB_NAME, uid, week_key):
            continue

        text = build_weekly_card(uid, week_key)
        await context.bot.send_message(chat_id=uid, text=text)


def _bar(x: float, goal: float, width: int = 10) -> str:
    if goal <= 0:
        return "░" * width
    ratio = max(0.0, min(x / goal, 1.0))
    filled = int(round(ratio * width))
    return "█" * filled + "░" * (width - filled)


def build_daily_card(user_id: int, day_iso: str) -> str:
    """Text-only 'card' that looks like a dashboard in Telegram."""
    with db() as conn:
        cur = conn.cursor()

        # totals
        cur.execute(
            '''
            SELECT
              COALESCE(SUM(calories), 0),
              COALESCE(SUM(protein), 0),
              COALESCE(SUM(fat), 0),
              COALESCE(SUM(carbs), 0),
              COALESCE(SUM(fiber), 0)
            FROM entries
            WHERE user_id=%s AND entry_date::date=%s::date
              AND item_name IS NOT NULL AND item_name <> ''
            ''',
            (user_id, day_iso),
        )
        calories, protein, fat, carbs, fiber = cur.fetchone()

        # items (for AI)
        cur.execute(
            '''
            SELECT
              COALESCE(meal, 'other') AS meal,
              COALESCE(item_name, '') AS item_name,
              COALESCE(qty, 1)        AS qty,
              COALESCE(unit, 'serving') AS unit,
              COALESCE(calories, 0)   AS calories,
              COALESCE(protein, 0)    AS protein,
              COALESCE(fat, 0)        AS fat,
              COALESCE(carbs, 0)      AS carbs,
              COALESCE(fiber, 0)      AS fiber
            FROM entries
            WHERE user_id=%s AND entry_date::date=%s::date
              AND item_name IS NOT NULL AND item_name <> ''
            ORDER BY id
            ''',
            (user_id, day_iso),
        )
        rows = cur.fetchall()

    calories = float(calories or 0)
    protein = float(protein or 0)
    fat = float(fat or 0)
    carbs = float(carbs or 0)
    fiber = float(fiber or 0)
    net = max(carbs - fiber, 0.0)

    targets = get_targets(user_id) or {}
    kcal_goal = float(targets.get("calories") or 0)
    p_goal = float(targets.get("protein") or 0)
    f_goal = float(targets.get("fat") or 0)
    nc_goal = float(targets.get("net_carbs") or 0)

    # base card
    lines = [
        "📅 Итог дня",
        f"📊 Ккал {calories:.0f}/{kcal_goal:.0f} {_bar(calories, kcal_goal)}",
        f"🥩 Белок {protein:.0f}/{p_goal:.0f} {_bar(protein, p_goal)}",
        f"🥑 Жиры {fat:.0f}/{f_goal:.0f} {_bar(fat, f_goal)}",
        f"🍬 Чистые {net:.0f}/{nc_goal:.0f} {_bar(net, nc_goal)}",
    ]

    # AI advice for subscribers only
    if is_subscribed(user_id) and rows:
        try:
            items_for_ai = []
            for meal, name, qty, unit, cal, p, f, c, fi in rows:
                items_for_ai.append({
                    "meal": meal,
                    "name": name,
                    "qty": float(qty) if qty is not None else 1.0,
                    "unit": unit,
                    "calories": float(cal or 0),
                    "protein": float(p or 0),
                    "fat": float(f or 0),
                    "carbs": float(c or 0),
                    "fiber": float(fi or 0),
                })

            totals_for_ai = {
                "calories": calories,
                "protein": protein,
                "fat": fat,
                "carbs": carbs,
                "fiber": fiber,
                "net_carbs": net,
            }

            profile_hint = build_profile_hint({"user_id": user_id})
            analysis = ai_daily_analysis_ru(
                profile_hint=profile_hint,
                day=day_iso,
                totals=totals_for_ai,
                items=items_for_ai,
            )
            headline = analysis.get("headline") or "Небольшой фокус на завтра."
            improve = analysis.get("improve") or []
            plan = analysis.get("plan") or []

            lines.append("")
            lines.append("🧠 Совет дня:")
            lines.append(f"• {headline}")
            for x in (improve[:2] + plan[:1]):
                lines.append(f"• {x}")
        except Exception:
            # don't break the job if AI fails
            pass
    else:
        lines.append("")
        lines.append("🔒 Подписка: включи /pay, чтобы получать персональные AI-советы в отчётах.")

    return "\n".join(lines)


def build_weekly_card(user_id: int, week_key: str) -> str:
    # last 7 days window
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            '''
            SELECT
              COUNT(DISTINCT entry_date::date),
              COALESCE(SUM(calories), 0),
              COALESCE(SUM(protein), 0),
              COALESCE(SUM(fat), 0),
              COALESCE(SUM(carbs), 0),
              COALESCE(SUM(fiber), 0)
            FROM entries
            WHERE user_id=%s
              AND entry_date::date >= CURRENT_DATE - INTERVAL '6 days'
              AND item_name IS NOT NULL AND item_name <> ''
            ''',
            (user_id,),
        )
        days, calories, protein, fat, carbs, fiber = cur.fetchone()

    days = int(days or 0)
    if days <= 0:
        return "📅 Отчёт за неделю\n\nПока нет логов за последние 7 дней."

    calories = float(calories or 0)
    protein = float(protein or 0)
    fat = float(fat or 0)
    carbs = float(carbs or 0)
    fiber = float(fiber or 0)
    net = max(carbs - fiber, 0.0)

    avg_kcal = calories / days
    avg_p = protein / days
    avg_net = net / days

    lines = [
        f"🗓 Отчёт за неделю ({week_key})",
        f"Дней с логами: {days}",
        "",
        f"Среднее в день:",
        f"• Ккал: {avg_kcal:.0f}",
        f"• Белок: {avg_p:.0f} г",
        f"• Чистые угл: {avg_net:.0f} г",
        "",
        "Фокус на следующую неделю:",
        "• 1) держи белок стабильным",
        "• 2) не добивай углеводы вечером",
    ]

    if not is_subscribed(user_id):
        lines.append("")
        lines.append("🔒 Подписка: /pay для более умного weekly-анализа (AI + цели).")

    return "\n".join(lines)
