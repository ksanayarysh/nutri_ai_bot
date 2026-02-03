# src/handlers/commands.py
from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from src.ai import ai_estimate, ai_daily_analysis_ru, ai_case_plan_ru, ai_weekly_analysis_ru
from src.db import ensure_user, get_targets
from src.handlers.messages import unit_label, meal_label
from src.i18n.lang import get_user_language, _normalize_lang, SUPPORTED_LANGS, set_user_language
from src.i18n.t import t
from src.profile import build_profile_hint
from src.config import ADMIN_IDS

from datetime import datetime, timedelta, timezone
from src.config import TZ
from src.db import db, today_str
from src.subscriptions import is_subscribed_user, is_subscribed
from src.jobs.notifications import (
    ensure_notify_settings,
    set_daily_enabled,
    set_weekly_enabled,
    set_daily_time_hhmm,
)


UNIT_LABELS = {
    "pcs": "шт",
    "g": "г",
    "ml": "мл",
    "tbsp": "ст.л.",
    "tsp": "ч.л.",
    "serving": "порц.",
}


# -------------------------
# /start
# -------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return

    ensure_user(user.id, user.username, user.first_name)

    lang = get_user_language(user.id)

    await update.message.reply_text(
        t("start.text", lang),
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg:
        return

    user = update.effective_user
    lang = get_user_language(user.id) if user else "ru"

    await msg.reply_text(t("help.text", lang))


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return

    lang = get_user_language(user.id)  # <-- важно

    if not is_subscribed_user(user.id):
        await update.message.reply_text(t("today.paywall", lang))
        return

    day = today_str()

    with db() as conn:
        cur = conn.cursor()

        cur.execute(
            """
            SELECT
              COALESCE(meal, 'other')      AS meal,
              COALESCE(item_name, '')      AS item_name,
              COALESCE(qty, 1)             AS qty,
              COALESCE(unit, 'serving')    AS unit,
              COALESCE(calories, 0)        AS calories,
              COALESCE(protein, 0)         AS protein,
              COALESCE(fat, 0)             AS fat,
              COALESCE(carbs, 0)           AS carbs,
              COALESCE(fiber, 0)           AS fiber
            FROM entries
            WHERE user_id = %s AND entry_date = %s
            ORDER BY meal, id
            """,
            (user.id, day),
        )
        rows = cur.fetchall()

        cur.execute(
            """
            SELECT
              COALESCE(SUM(calories), 0),
              COALESCE(SUM(protein), 0),
              COALESCE(SUM(fat), 0),
              COALESCE(SUM(carbs), 0),
              COALESCE(SUM(fiber), 0)
            FROM entries
            WHERE user_id = %s AND entry_date = %s
            """,
            (user.id, day),
        )
        calories, protein, fat, carbs, fiber = cur.fetchone()

    # вместо txt = TODAY_TEXT.get(...) ...
    if not rows:
        await update.message.reply_text(t("today.empty", lang))
        return

    meals: dict[str, list[str]] = {}
    i = 1
    for meal, name, qty, unit, cal, p, f, c, fi in rows:
        meals.setdefault(meal or "other", []).append(
            t(
                "today.item_line",
                lang,
                i=i,
                name=name,
                qty=f"{float(qty):g}",
                unit=unit_label(lang, unit),
                cal=f"{float(cal):.0f}",
                p=f"{float(p):.1f}",
                f=f"{float(f):.1f}",
                c=f"{float(c):.1f}",
            )
        )
        i += 1

    lines = [t("today.title", lang)]
    for meal, items in meals.items():
        lines.append(t("today.meal_header", lang, meal=meal_label(lang, meal)))
        lines.extend(items)

    net = max(float(carbs or 0.0) - float(fiber or 0.0), 0.0)

    lines.append(
        t("today.totals_title", lang)
        + "\n"
        + t(
            "today.totals_lines",
            lang,
            kcal=f"{float(calories):.0f}",
            protein=f"{float(protein):.1f}",
            fat=f"{float(fat):.1f}",
            carbs=f"{float(carbs):.1f}",
            net=f"{float(net):.1f}",
        )
    )

    await update.message.reply_text("\n".join(lines))


# -------------------------
# /week
# -------------------------

async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return

    lang = get_user_language(user.id)

    if not is_subscribed_user(user.id):
        await update.message.reply_text(t("week.paywall", lang))
        return

    # last 7 days, including today
    now = datetime.now(TZ)
    days_list = [(now - timedelta(days=i)).date().isoformat() for i in range(7)]

    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            '''
            SELECT
              COUNT(DISTINCT entry_date) AS days_with_logs,
              COALESCE(SUM(calories), 0) AS calories,
              COALESCE(SUM(protein), 0)  AS protein,
              COALESCE(SUM(fat), 0)      AS fat,
              COALESCE(SUM(carbs), 0)    AS carbs,
              COALESCE(SUM(fiber), 0)    AS fiber
            FROM entries
            WHERE user_id = %s AND entry_date = ANY(%s)
            ''',
            (user.id, days_list),
        )
        days_with_logs, calories, protein, fat, carbs, fiber = cur.fetchone()

    days = int(days_with_logs or 0)
    if days <= 0:
        await update.message.reply_text(t("week.empty", lang))
        return

    net = max(float(carbs or 0.0) - float(fiber or 0.0), 0.0)

    def _line(label_key: str, total: str, avg: str) -> str:
        return t("week.line", lang, label=t(label_key, lang), total=total, avg=avg)

    # базовый текстовый summary (читаемый формат, построчно)
    lines = []
    lines.append(t("week.title", lang))
    lines.append(f"📅 Дней с записями: {days}")
    lines.append("")
    lines.append(f"🔥 Ккал: {float(calories):.0f} (ср {float(calories)/days:.0f}/день)")
    lines.append(f"🥩 Белки: {float(protein):.1f} г (ср {float(protein)/days:.1f}/день)")
    lines.append(f"🧈 Жиры: {float(fat):.1f} г (ср {float(fat)/days:.1f}/день)")
    lines.append(f"🥔 Углеводы: {float(carbs):.1f} г (ср {float(carbs)/days:.1f}/день)")
    lines.append(f"🍬 Чистые: {float(net):.1f} г (ср {float(net)/days:.1f}/день)")
    lines.append(f"🥬 Клетчатка: {float(fiber):.1f} г (ср {float(fiber)/days:.1f}/день)")

    # --- AI-недельный анализ
    # (как дневной, с микроэлементами) ---
    try:
        profile_hint = build_profile_hint({"user_id": user.id, "language": lang})
    except Exception:
        profile_hint = {}

    targets = None
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT kcal, protein, fat, carbs, fiber, diet FROM targets WHERE user_id=%s",
            (user.id,),
        )
        row = cur.fetchone()
        if row:
            targets = {
                "kcal": float(row[0]) if row[0] is not None else None,
                "protein": float(row[1]) if row[1] is not None else None,
                "fat": float(row[2]) if row[2] is not None else None,
                "carbs": float(row[3]) if row[3] is not None else None,
                "fiber": float(row[4]) if row[4] is not None else None,
                "diet": row[5],
            }

    totals_payload = {
        "days_with_logs": days,
        "totals": {
            "calories": float(calories),
            "protein": float(protein),
            "fat": float(fat),
            "carbs": float(carbs),
            "fiber": float(fiber),
            "net_carbs": float(net),
        },
        "avg_per_day": {
            "calories": float(calories)/days,
            "protein": float(protein)/days,
            "fat": float(fat)/days,
            "carbs": float(carbs)/days,
            "fiber": float(fiber)/days,
            "net_carbs": float(net)/days,
        },
    }
    if targets:
        totals_payload["targets"] = targets

    analysis = None
    try:
        analysis = ai_weekly_analysis_ru(profile_hint=profile_hint, period_label=t("week.title", lang), totals=totals_payload, food_examples=[])
    except Exception:
        analysis = None

    if isinstance(analysis, dict):
        lines.append("")
        lines.append(analysis.get("headline", "🗓️ Анализ недели"))
        lines.append("")
        micro = analysis.get("micronutrients")
        if isinstance(micro, dict) and micro:
            lines.append("")
            lines.append("🧬 Микроэлементы:")
            order = [
                ("iron", "🩸 Железо"),
                ("zinc", "🧷 Цинк"),
                ("magnesium", "🧲 Магний"),
                ("iodine", "🧂 Йод"),
                ("selenium", "🌰 Селен"),
                ("vitamin_b12", "💊 Витамин B12"),
                ("calcium", "🥛 Кальций"),
                ("antioxidants", "🍓 Антиоксиданты"),
                ("omega_3", "🐟 Омега-3"),
            ]
            for k, label in order:
                v = micro.get(k) or "—"
                lines.append(f"{label}: {v}")

        for key, title in [("good", "✅ Что хорошо:"), ("improve", "🛠 Что улучшить:"), ("plan", "📌 План:"), ("warnings", "⚠️ Предупреждения:")]:
            arr = analysis.get(key) or []
            if arr:
                lines.append("")
                lines.append(title)
                lines.extend([f"• {x}" for x in arr])

    await update.message.reply_text("".join(lines))


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return

    with db() as conn:
        cur = conn.cursor()

        # subscription status (trial/subscription)
        cur.execute(
            "SELECT trial_until, subscribed_until FROM users WHERE user_id = %s",
            (user.id,),
        )
        row = cur.fetchone()
        trial_until = row[0] if row else None
        subscribed_until = row[1] if row else None

        # targets
        cur.execute(
            """
            SELECT kcal, protein, fat, carbs, fiber, diet, updated_at
            FROM targets
            WHERE user_id = %s
            """,
            (user.id,),
        )
        t = cur.fetchone()

    status = "активна ✅" if is_subscribed_user(user.id) else "не активна ⏳"

    lines = [
        "👤 Профиль",
        "",
        f"Подписка: {status}",
    ]

    if subscribed_until:
        lines.append(f"Оплачено до: {subscribed_until}")
    elif trial_until:
        lines.append(f"Бесплатный период до: {trial_until}")

    if t:
        calories, protein, fat, carbs, fiber, mode, updated_at = t
        lines += [
            "",
            "🎯 Цели:",
            f"- Режим: {mode or '—'}",
            f"- Ккал: {float(calories):.0f}" if calories is not None else "- Ккал: —",
            f"- Белки: {float(protein):.1f} г" if protein is not None else "- Белки: —",
            f"- Жиры: {float(fat):.1f} г" if fat is not None else "- Жиры: —",
            f"- Углеводы: {float(carbs):.1f} г" if carbs is not None else "- Углеводы: —",
            f"- Клетчатка: {float(fiber):.1f} г" if fiber is not None else "- Чистые углеводы: —",
            f"- Обновлено: {updated_at or '—'}",
        ]
    else:
        lines += [
            "",
            "🎯 Цели пока не заданы.",
            "Задай так:",
            "`/set_targets 1400 90 70 30 20 keto`",
            "(ккал, белки, жиры, угли, net, режим)",
        ]

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_set_targets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.message
    if not user or not msg:
        return

    if not context.args or len(context.args) < 5:
        await msg.reply_text(
            "Формат:\n"
            "`/set_targets калории белок жир углеводы чистые углеводы [режим]`\n"
            "Пример:\n"
            "`/set_targets 1400 90 70 30 20 keto`",
            parse_mode="Markdown",
        )
        return

    try:
        kcal = float(context.args[0])
        protein = float(context.args[1])
        fat = float(context.args[2])
        carbs = float(context.args[3])
        net_carbs = float(context.args[4])
        mode = context.args[5] if len(context.args) > 5 else None
    except ValueError:
        await msg.reply_text("Числа в целях должны быть числами. Да, это звучит очевидно.")
        return

    # минимальная sanity-проверка
    if kcal <= 0 or protein < 0 or fat < 0 or carbs < 0 or net_carbs < 0:
        await msg.reply_text("Цели должны быть положительными (ккал > 0, остальные >= 0).")
        return
    if net_carbs > carbs:
        await msg.reply_text("net_carbs не может быть больше carbs.")
        return

    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO targets (user_id, kcal, protein, fat, carbs, net_carbs, diet, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
              kcal = EXCLUDED.kcal,
              protein = EXCLUDED.protein,
              fat = EXCLUDED.fat,
              carbs = EXCLUDED.carbs,
              net_carbs = EXCLUDED.net_carbs,
              diet = EXCLUDED.diet,
              updated_at = EXCLUDED.updated_at
            """,
            (user.id, kcal, protein, fat, carbs, net_carbs, mode, datetime.now(TZ).isoformat(timespec="seconds")),
        )
        conn.commit()

    await msg.reply_text("Цели сохранены ✅\nПосмотреть: /goals")


async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return

    if not is_subscribed_user(user.id):
        await update.message.reply_text("⏳ Анализ доступен по подписке. Используй /pay")
        return

    days = 7
    if context.args:
        try:
            days = max(3, min(60, int(context.args[0])))
        except ValueError:
            pass

    since = (datetime.now(TZ).date() - timedelta(days=days)).isoformat()

    with db() as conn:
        cur = conn.cursor()

        # totals for period
        cur.execute(
            """
            SELECT
              COUNT(DISTINCT entry_date) as days_logged,
              COALESCE(SUM(calories), 0),
              COALESCE(SUM(protein), 0),
              COALESCE(SUM(fat), 0),
              COALESCE(SUM(carbs), 0),
              COALESCE(SUM(fiber), 0)
            FROM entries
            WHERE user_id = %s AND entry_date >= %s
            """,
            (user.id, since),
        )
        days_logged, cal, p, f, c, nc = cur.fetchone()

        # targets (optional)
        cur.execute(
            """
            SELECT kcal, protein, fat, carbs, net_carbs, diet
            FROM targets
            WHERE user_id = %s
            """,
            (user.id,),
        )
        t = cur.fetchone()

    days_logged = int(days_logged or 0)
    if days_logged == 0:
        await update.message.reply_text(f"За последние {days} дней нет записей. Мне анализировать пустоту?")
        return

    # averages per logged day (more honest than per calendar day)
    avg_cal = float(cal) / days_logged
    avg_p = float(p) / days_logged
    avg_f = float(f) / days_logged
    avg_c = float(c) / days_logged
    avg_nc = float(nc) / days_logged

    lines = [
        f"🧠 Анализ за {days} дней",
        f"Дней с логами: {days_logged}",
        "",
        "Среднее за день (по дням с логами):",
        f"- Ккал: {avg_cal:.0f}",
        f"- Белки: {avg_p:.1f} г",
        f"- Жиры: {avg_f:.1f} г",
        f"- Углеводы: {avg_c:.1f} г",
        f"- Чистые углеводы: {avg_nc:.1f} г",
    ]

    if t:
        tkcal, tp, tf, tc, tnc, mode = t
        lines += [
            "",
            "🎯 Сравнение с целями:",
        ]

        def _cmp(name: str, avg: float, target: float | None):
            if target is None:
                return f"- {name}: цель не задана"
            diff = avg - float(target)
            sign = "+" if diff >= 0 else ""
            return f"- {name}: {avg:.1f} vs {float(target):.1f} (Δ {sign}{diff:.1f})"

        lines.append(_cmp("Ккал", avg_cal, tkcal))
        lines.append(_cmp("Белки", avg_p, tp))
        lines.append(_cmp("Жиры", avg_f, tf))
        lines.append(_cmp("Углеводы", avg_c, tc))
        lines.append(_cmp("Net carbs", avg_nc, tnc))
        if mode:
            lines.append(f"- Режим: {mode}")

        # simple “coach style” flags (safe, non-medical)
        lines += ["", "🟡 Наблюдения:"]
        if tp is not None and avg_p < float(tp) * 0.8:
            lines.append("• Белка стабильно ниже цели. Это обычно бьёт по сытости и мышцам.")
        if tnc is not None and avg_nc > float(tnc) * 1.2:
            lines.append("• Net carbs выше цели. Если ты в lowcarb/keto, это объясняет качели аппетита.")
        if tkcal is not None and avg_cal > float(tkcal) * 1.2:
            lines.append("• Калории выше цели. Не трагедия, но это объясняет, почему вес не двигается.")
    else:
        lines += [
            "",
            "🎯 Цели не заданы, поэтому сравнение отключено.",
            "Задай цели: `/set_targets 1400 90 70 30 20 keto`",
        ]

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.message
    if not user or not msg:
        return

    if not context.args:
        await msg.reply_text("❌ Укажи номер записи. Например: /del 1")
        return

    try:
        idx = int(context.args[0])
        if idx <= 0:
            raise ValueError
    except ValueError:
        await msg.reply_text("❌ Номер должен быть положительным числом.")
        return

    day = today_str()

    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, item_name
            FROM entries
            WHERE user_id = %s
              AND entry_date = %s
              AND item_name IS NOT NULL
            ORDER BY id
            """,
            (user.id, day),
        )
        rows = cur.fetchall()

        if idx > len(rows):
            await msg.reply_text("❌ Нет записи с таким номером.")
            return

        entry_id, name = rows[idx - 1]

        cur.execute(
            "DELETE FROM entries WHERE id = %s",
            (entry_id,),
        )
        conn.commit()

    lang = get_user_language(user.id)

    await msg.reply_text(t("log.deleted", lang, name=name))

async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.message
    if not user or not msg:
        return

    if len(context.args) < 2:
        await msg.reply_text("❌ Формат: /edit <номер> <новый текст>\nНапример: /edit 1 яйцо 2 шт")
        return

    # 1) номер
    try:
        idx = int(context.args[0])
        if idx <= 0:
            raise ValueError
    except ValueError:
        await msg.reply_text("❌ Номер должен быть положительным числом.")
        return

    new_text = " ".join(context.args[1:]).strip()
    if not new_text:
        await msg.reply_text("❌ Новый текст пустой.")
        return

    day = today_str()

    # 2) найти запись по номеру (как в /del)
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, meal, raw_text
            FROM entries
            WHERE user_id = %s
              AND entry_date = %s
              AND item_name IS NOT NULL
            ORDER BY id
            """,
            (user.id, day),
        )
        rows = cur.fetchall()

        if idx > len(rows):
            await msg.reply_text("❌ Нет записи с таким номером.")
            return

        entry_id, meal, old_raw = rows[idx - 1]

    # 3) прогнать ai_estimate по новому тексту (ожидаем 1 item)
    try:
        lang = get_user_language(user.id)
        profile_hint = build_profile_hint({"user_id": user.id, "language": lang})

        items, confidence, meta = ai_estimate(
            text=new_text,
            meal_hint=meal or "other",
            profile_hint=profile_hint,
        )
    except Exception as e:
        await msg.reply_text(f"ai сломался: {type(e).__name__}: {str(e)[:200]}")
        return

    if not items:
        await msg.reply_text("❌ Не смогла распознать еду. Попробуй написать иначе.")
        return

    # Если AI вернул несколько items, это двусмысленно. Берём первый и предупреждаем.
    it = items[0]
    warn_multi = len(items) > 1

    # 4) обновить запись
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE entries
            SET raw_text = %s,
                item_name = %s,
                qty = %s,
                unit = %s,
                calories = %s,
                protein = %s,
                fat = %s,
                carbs = %s,
                fiber = %s
            WHERE id = %s AND user_id = %s
            """,
            (
                new_text,
                it.name,
                it.qty,
                it.unit,
                it.calories,
                it.protein,
                it.fat,
                it.carbs,
                it.fiber,
                entry_id,
                user.id,
            ),
        )
        conn.commit()

    text = f"✅ Обновила запись #{idx}: {it.name} — {it.qty:g} {UNIT_LABELS[it.unit]} ({(it.calories or 0):.0f} ккал)"
    if warn_multi:
        text += "\n⚠️ AI распознал несколько продуктов, я обновила только первый."
    await msg.reply_text(text)


async def cmd_analyze_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.message
    if not user or not msg:
        return

    if not is_subscribed_user(user.id):
        await msg.reply_text("⏳ Эта функция доступна по подписке.\nИспользуй /pay")
        return

    day = today_str()

    with db() as conn:
        cur = conn.cursor()

        cur.execute(
            """
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
            WHERE user_id = %s
              AND entry_date = %s
              AND item_name IS NOT NULL
              AND item_name <> ''
            ORDER BY id
            """,
            (user.id, day),
        )
        rows = cur.fetchall()

        cur.execute(
            """
            SELECT
              COALESCE(SUM(calories), 0),
              COALESCE(SUM(protein), 0),
              COALESCE(SUM(fat), 0),
              COALESCE(SUM(carbs), 0),
              COALESCE(SUM(fiber), 0)
            FROM entries
            WHERE user_id = %s AND entry_date = %s
              AND item_name IS NOT NULL AND item_name <> ''
            """,
            (user.id, day),
        )
        calories, protein, fat, carbs, fiber = cur.fetchone()

    if not rows:
        await msg.reply_text("📭 За сегодня пока нет записей для анализа.")
        return

    calories = float(calories or 0)
    protein = float(protein or 0)
    fat = float(fat or 0)
    carbs = float(carbs or 0)
    fiber = float(fiber or 0)
    net_carbs = max(carbs - fiber, 0.0)

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
        "net_carbs": float(net_carbs),
    }

    # ✅ ДОБАВЛЯЕМ TARGETS ДЛЯ ПЕРСОНАЛЬНОГО АНАЛИЗА
    targets = get_targets(user.id)  # dict | None
    if targets:
        totals_for_ai["targets"] = {
            "calories": targets.get("calories"),
            "protein": targets.get("protein"),
            "fat": targets.get("fat"),
            "carbs": targets.get("carbs"),
            "net_carbs": targets.get("net_carbs"),
            "mode": targets.get("mode"),
        }

    try:
        lang = get_user_language(user.id)
        profile_hint = build_profile_hint({"user_id": user.id, "language": lang})

        analysis = ai_daily_analysis_ru(
            profile_hint=profile_hint,
            day=day,
            totals=totals_for_ai,
            items=items_for_ai,
        )
    except Exception as e:
        await msg.reply_text(f"ai сломался: {type(e).__name__}: {str(e)[:200]}")
        return

    lines = ["🧠 Анализ дня (AI):", f"• {analysis['headline']}"]

    good = analysis.get("good") or []
    improve = analysis.get("improve") or []
    plan = analysis.get("plan") or []
    warnings = analysis.get("warnings") or []

    if good:
        lines.append("\n✅ Что хорошо:")
        lines.extend([f"• {x}" for x in good])

    if improve:
        lines.append("\n🛠 Что улучшить:")
        lines.extend([f"• {x}" for x in improve])

    if plan:
        lines.append("\n📌 План на завтра:")
        lines.extend([f"• {x}" for x in plan])

    if warnings:
        lines.append("\n⚠️ Предупреждения:")
        lines.extend([f"• {x}" for x in warnings])

    # ✅ БЛОК "ПРОГРЕСС VS ЦЕЛЬ"
    if targets:
        def _pct(a: float, b: float) -> str:
            if not b or b <= 0:
                return "-"
            return f"{(a / b) * 100:.0f}%"

        t_cal = float(targets.get("calories") or 0)
        t_p   = float(targets.get("protein") or 0)
        t_f   = float(targets.get("fat") or 0)
        t_c   = float(targets.get("carbs") or 0)
        t_nc  = float(targets.get("net_carbs") or 0)
        mode = targets.get("mode")

        lines.append("\n🎯 Прогресс vs цель" + (f" ({mode})" if mode else "") + ":")

        lines.append(f"Ккал: {calories:.0f}/{t_cal:.0f} ({_pct(calories, t_cal)})" if t_cal else f"Ккал: {calories:.0f}")
        lines.append(f"Белки: {protein:.0f}/{t_p:.0f} г ({_pct(protein, t_p)})" if t_p else f"Белки: {protein:.0f} г")
        lines.append(f"Жиры: {fat:.0f}/{t_f:.0f} г ({_pct(fat, t_f)})" if t_f else f"Жиры: {fat:.0f} г")
        lines.append(f"Углеводы: {carbs:.0f}/{t_c:.0f} г ({_pct(carbs, t_c)})" if t_c else f"Углеводы: {carbs:.0f} г")
        lines.append(f"Чистые: {net_carbs:.0f}/{t_nc:.0f} г ({_pct(net_carbs, t_nc)})" if t_nc else f"Чистые: {net_carbs:.0f} г")
    else:
        lines.append("\n🎯 Чтобы советы были персональнее, задай цели: /set_targets ... (или посмотри /goals)")

    lines.append(
        "\n📊 Итоги:\n"
        f"Ккал: {calories:.0f}\n"
        f"Белки: {protein:.1f} г\n"
        f"Жиры: {fat:.1f} г\n"
        f"Углеводы: {carbs:.1f} г\n"
        f"Клетчатка: {fiber:.1f} г\n"
        f"Чистые углеводы: {net_carbs:.1f} г"
    )

    await msg.reply_text("\n".join(lines))

async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return

    await update.message.reply_text(
        f"🆔 Твой ID:\n\n"
        f"`{user.id}`\n\n"
        f"Используй этот ID при оплате или напиши его администратору.",
        parse_mode="Markdown"
    )


async def cmd_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    context.user_data["contact_mode"] = True

    await update.message.reply_text(
        "📩 Напиши сообщение, и я перешлю его администратору.\n"
        "Чтобы отменить — напиши /cancel"
    )

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("contact_mode", None)
    if update.message:
        await update.message.reply_text("❌ Отменено.")

async def cmd_goals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.message
    if not user or not msg:
        return

    targets = get_targets(user.id)

    if not targets:
        await msg.reply_text(
            "🎯 Цели не заданы.\n\n"
            "Задай их командой:\n"
            "`/set_targets kcal protein fat carbs net_carbs [mode]`\n\n"
            "Пример:\n"
            "`/set_targets 1400 90 70 30 20 keto`",
            parse_mode="Markdown",
        )
        return

    lines = [
        "🎯 Твои цели:",
        f"Ккал: {targets.get('calories')}",
        f"Белки: {targets.get('protein')} г",
        f"Жиры: {targets.get('fat')} г",
        f"Углеводы: {targets.get('carbs')} г",
        f"Чистые углеводы: {targets.get('net_carbs')} г",
    ]

    if targets.get("mode"):
        lines.append(f"Режим: {targets['mode']}")

    await msg.reply_text("\n".join(lines))

# -------------------------
# /notify (daily auto-report)
# -------------------------

async def cmd_notify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.message
    if not user or not msg:
        return

    if not context.args:
        await msg.reply_text("Формат: /notify on|off")
        return

    arg = context.args[0].strip().lower()
    if arg not in {"on", "off"}:
        await msg.reply_text("Формат: /notify on|off")
        return

    ensure_notify_settings(user.id)
    set_daily_enabled(user.id, enabled=(arg == "on"))
    await msg.reply_text("Готово ✅ Ежедневные уведомления: " + ("включены" if arg == "on" else "выключены"))


# -------------------------
# /notify_weekly
# -------------------------

async def cmd_notify_weekly(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.message
    if not user or not msg:
        return

    if not context.args:
        await msg.reply_text("Формат: /notify_weekly on|off")
        return

    arg = context.args[0].strip().lower()
    if arg not in {"on", "off"}:
        await msg.reply_text("Формат: /notify_weekly on|off")
        return

    ensure_notify_settings(user.id)
    set_weekly_enabled(user.id, enabled=(arg == "on"))
    await msg.reply_text("Готово ✅ Еженедельные уведомления: " + ("включены" if arg == "on" else "выключены"))


# -------------------------
# /notify_time HH:MM
# -------------------------

async def cmd_notify_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.message
    if not user or not msg:
        return

    if not context.args:
        await msg.reply_text("Формат: /notify_time 21:00")
        return

    hhmm = context.args[0].strip()
    ensure_notify_settings(user.id)

    try:
        set_daily_time_hhmm(user.id, hhmm)
    except ValueError:
        await msg.reply_text("Неверный формат времени. Пример: 21:00")
        return

    await msg.reply_text(f"Ок ✅ Буду присылать ежедневный отчёт в {hhmm} (по твоему часовому поясу).")


# -------------------------
# /streak (subscription)
# -------------------------

async def cmd_streak(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.message
    if not user or not msg:
        return

    if not is_subscribed_user(user.id):
        await msg.reply_text("⏳ Эта функция доступна по подписке.\nИспользуй /pay")
        return

    day0 = today_str()
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            '''
            SELECT entry_date::date, COUNT(*) as n
            FROM entries
            WHERE user_id = %s
            GROUP BY entry_date::date
            ORDER BY entry_date::date DESC
            ''',
            (user.id,),
        )
        rows = cur.fetchall()

    if not rows:
        await msg.reply_text("Пока нет ни одного дня с логами.")
        return

    # compute consecutive streak ending today (or yesterday if no logs today)
    dates = [r[0] for r in rows]
    today = datetime.now(TZ).date()
    start = today if today in dates else (today - timedelta(days=1) if (today - timedelta(days=1)) in dates else None)
    if start is None:
        await msg.reply_text("Серии пока нет (нет логов сегодня/вчера).")
        return

    streak = 0
    d = start
    s = set(dates)
    while d in s:
        streak += 1
        d = d - timedelta(days=1)

    await msg.reply_text(f"🔥 Серия: {streak} дн. подряд (последний день: {start.isoformat()}).")


# -------------------------
# /progress (subscription)
# -------------------------

def _bar(x: float, goal: float, width: int = 10) -> str:
    if goal <= 0:
        return "░" * width
    ratio = max(0.0, min(x / goal, 1.0))
    filled = int(round(ratio * width))
    return "█" * filled + "░" * (width - filled)

async def cmd_progress(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.message
    if not user or not msg:
        return

    if not is_subscribed_user(user.id):
        await msg.reply_text("⏳ Эта функция доступна по подписке.\nИспользуй /pay")
        return

    targets = get_targets(user.id)
    if not targets:
        await msg.reply_text("Сначала задай цели: /set_targets ...")
        return

    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            '''
            SELECT entry_date::date,
                   COALESCE(SUM(calories),0),
                   COALESCE(SUM(protein),0),
                   COALESCE(SUM(fat),0),
                   COALESCE(SUM(carbs),0),
                   COALESCE(SUM(fiber),0)
            FROM entries
            WHERE user_id = %s
              AND entry_date::date >= CURRENT_DATE - INTERVAL '6 days'
            GROUP BY entry_date::date
            ORDER BY entry_date::date ASC
            ''',
            (user.id,),
        )
        rows = cur.fetchall()

    if not rows:
        await msg.reply_text("За последние 7 дней нет логов.")
        return

    kcal_goal = float(targets.get("calories") or 0)
    p_goal = float(targets.get("protein") or 0)
    f_goal = float(targets.get("fat") or 0)
    c_goal = float(targets.get("carbs") or 0)
    nc_goal = float(targets.get("net_carbs") or 0)

    days_in_goal = 0
    lines = ["📈 Прогресс за 7 дней (по целям):"]

    for d, kcal, p, f, c, fib in rows:
        kcal = float(kcal)
        p = float(p)
        f = float(f)
        c = float(c)
        fib = float(fib)
        net = max(c - fib, 0.0)

        ok = True
        if kcal_goal > 0 and kcal > kcal_goal: ok = False
        if p_goal > 0 and p < p_goal * 0.85: ok = False  # мягко: 85% нормы
        if nc_goal > 0 and net > nc_goal: ok = False

        if ok:
            days_in_goal += 1

        lines.append(
            f"• {d.strftime('%d.%m')}: "
            f"ккал {kcal:.0f}/{kcal_goal:.0f} {_bar(kcal,kcal_goal)} | "
            f"Б {p:.0f}/{p_goal:.0f} {_bar(p,p_goal)} | "
            f"net {net:.0f}/{nc_goal:.0f} {_bar(net,nc_goal)}"
        )

    lines.append(f"\n✅ Дней близко к цели: {days_in_goal}/{len(rows)}")
    await msg.reply_text("\n".join(lines))

def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

async def cmd_grant(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.message
    if not user or not msg:
        return

    if not _is_admin(user.id):
        await msg.reply_text("Нет. Это админская команда 👑")
        return

    if not context.args or len(context.args) < 2:
        await msg.reply_text(
            "Формат:\n"
            "/grant <user_id> <days|forever> [plan]\n\n"
            "Примеры:\n"
            "/grant 452738438 30 basic\n"
            "/grant 452738438 forever premium"
        )
        return

    try:
        target_user_id = int(context.args[0])
    except ValueError:
        await msg.reply_text("user_id должен быть числом. Да, это важно.")
        return

    period = context.args[1].lower()
    plan = context.args[2] if len(context.args) > 2 else None

    now = datetime.now(timezone.utc)

    if period == "forever":
        expires_at = None
    else:
        try:
            days = int(period)
            if days <= 0:
                await msg.reply_text("days должен быть > 0 (или используй forever).")
                return
            expires_at = now + timedelta(days=days)
        except ValueError:
            await msg.reply_text("Второй аргумент: число дней или 'forever'.")
            return

    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO subscriptions (user_id, status, plan, expires_at, created_at, updated_at)
            VALUES (%s, 'active', %s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
              status = 'active',
              plan = COALESCE(EXCLUDED.plan, subscriptions.plan),
              expires_at = EXCLUDED.expires_at,
              updated_at = EXCLUDED.updated_at
            """,
            (target_user_id, plan, expires_at, now, now),
        )
        conn.commit()

    human_expires = "навсегда" if expires_at is None else expires_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    await msg.reply_text(f"✅ Доступ выдан user_id={target_user_id}, план={plan or '—'}, до: {human_expires}")

    # опционально: уведомить пользователя
    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text=f"✅ Подписка активирована ({plan or 'подписка'}). Срок: {human_expires}."
        )
    except Exception:
        # если пользователь не писал боту или блокнул, просто молчим
        pass

async def cmd_sub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.message
    if not user or not msg:
        return

    if not _is_admin(user.id):
        await msg.reply_text("Нет. Это админская команда 👑")
        return

    if not context.args:
        await msg.reply_text("Формат: /sub <user_id>\nПример: /sub 452738438")
        return

    try:
        target_user_id = int(context.args[0])
    except ValueError:
        await msg.reply_text("user_id должен быть числом.")
        return

    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT status, plan, expires_at, created_at, updated_at
            FROM subscriptions
            WHERE user_id = %s
            """,
            (target_user_id,),
        )
        row = cur.fetchone()

    if not row:
        await msg.reply_text(f"Подписка: нет записи для user_id={target_user_id}")
        return

    status, plan, expires_at, created_at, updated_at = row
    now = datetime.now(timezone.utc)

    if status == "active" and (expires_at is None or expires_at > now):
        state = "✅ ACTIVE"
    elif status == "active":
        state = "⏳ EXPIRED"
    else:
        state = f"❌ {status.upper()}"

    exp_txt = "навсегда" if expires_at is None else str(expires_at)
    await msg.reply_text(
        f"👤 user_id={target_user_id}\n"
        f"Статус: {state}\n"
        f"План: {plan or '—'}\n"
        f"Действует до: {exp_txt}\n"
        f"Создано: {created_at}\n"
        f"Обновлено: {updated_at}"
    )

async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.message
    if not user or not msg:
        return

    # на всякий случай, чтобы запись точно была
    ensure_user(user.id, user.username, user.first_name)

    args = context.args or []

    # если язык не задан явно, пытаемся подсказать дефолт по Telegram language_code (pt/ru)
    tg_guess = _normalize_lang(getattr(user, "language_code", None))

    if not args:
        current = get_user_language(user.id)

        if current == "pt":
            await msg.reply_text(
                "Idioma atual: PT-BR 🇧🇷\n\n"
                "Para trocar:\n"
                "• /lang ru (Русский)\n"
                "• /lang pt (Português)"
            )
        else:
            await msg.reply_text(
                "Текущий язык: RU 🇷🇺\n\n"
                "Чтобы сменить:\n"
                "• /lang pt (Português)\n"
                "• /lang ru (Русский)"
            )
        return

    requested = _normalize_lang(args[0])

    if requested not in SUPPORTED_LANGS:
        # мягко объясняем, что ты опять написала ерунду
        current = get_user_language(user.id)
        if current == "pt":
            await msg.reply_text("Idioma inválido. Use /lang pt ou /lang ru.")
        else:
            await msg.reply_text("Неверный язык. Используй /lang pt или /lang ru.")
        return

    set_user_language(user.id, requested)

    # подтверждение на новом языке
    if requested == "pt":
        await msg.reply_text("Pronto! Agora o idioma é PT-BR 🇧🇷")
    else:
        await msg.reply_text("Готово! Теперь язык — русский 🇷🇺")


async def cmd_case(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.message
    if not user or not msg:
        return

    if not is_subscribed_user(user.id):
        await msg.reply_text(
            "🔒 Команда /case доступна только по подписке.\n"
            "Оформить доступ: /pay"
        )
        return

    # берём весь текст после /case (включая многострочник)
    full = (msg.text or "").strip()
    case_text = full.replace("/case", "", 1).strip()

    if not case_text:
        await msg.reply_text(
            "Формат:\n"
            "/case <описание кейса>\n\n"
            "Пример:\n"
            "/case Мужчина, 35 лет, рост 180 см, вес 85 кг...\n"
        )
        return

    lang = get_user_language(user.id)
    profile_hint = build_profile_hint({"user_id": user.id, "language": lang})

    try:
        data = ai_case_plan_ru(profile_hint=profile_hint, case_text=case_text)
    except Exception as e:
        await msg.reply_text(f"ai сломался: {type(e).__name__}: {str(e)[:200]}")
        return

    # красивый рендер в текст (без таблиц, чтобы Telegram не устраивал цирк)
    ex = data.get("extracted", {})
    calc = data.get("calculations", {})
    menu = data.get("menu_3days", [])
    conf = float(data.get("confidence", 0.0))

    def _fmt(x, nd=0):
        if x is None:
            return "—"
        try:
            return f"{float(x):.{nd}f}"
        except Exception:
            return "—"

    lines = []
    lines.append("📌 Кейс: расчёты и рацион (3 дня)")
    lines.append("")
    lines.append("👤 Данные:")
    lines.append(f"- Пол: { 'мужчина' if ex.get('sex')=='male' else ('женщина' if ex.get('sex')=='female' else '—') }")
    lines.append(f"- Возраст: {_fmt(ex.get('age'), 0)}")
    lines.append(f"- Рост: {_fmt(ex.get('height_cm'), 0)} см")
    lines.append(f"- Вес: {_fmt(ex.get('weight_kg'), 0)} кг")
    lines.append(f"- Активность: {ex.get('activity') or '—'}")
    lines.append(f"- Цель: {ex.get('goal') or '—'}")

    prefs = ex.get("preferences") or []
    restr = ex.get("restrictions") or []
    if prefs:
        lines.append(f"- Предпочтения: {', '.join(prefs)}")
    if restr:
        lines.append(f"- Ограничения: {', '.join(restr)}")

    lines.append("")
    lines.append("🧮 Расчёты:")
    lines.append(f"- ИМТ: {_fmt(calc.get('bmi'), 1)}")
    lines.append(f"- ВОО (BMR): {_fmt(calc.get('bmr'), 0)} ккал")
    lines.append(f"- КА: {_fmt(calc.get('ka'), 3)}")
    lines.append(f"- СПК (TDEE): {_fmt(calc.get('tdee'), 0)} ккал")
    lines.append(f"- Целевая калорийность: {_fmt(calc.get('target_kcal'), 0)} ккал")
    lines.append(f"- Белок: {_fmt(calc.get('protein_g'), 0)} г")
    lines.append(f"- Жиры: {_fmt(calc.get('fat_g'), 0)} г")
    lines.append(f"- Углеводы: {_fmt(calc.get('carbs_g'), 0)} г")

    notes = calc.get("notes") or []
    if notes:
        lines.append("")
        lines.append("📝 Примечания:")
        lines.extend([f"• {n}" for n in notes[:6]])

    lines.append("")
    lines.append("🍽 Рацион на 3 дня:")
    for day in menu:
        d = day.get("day")
        lines.append(f"\nДень {int(d) if isinstance(d,(int,float)) else d}:")
        for m in (day.get("meals") or []):
            lines.append(f"• {m.get('name')}:")
            for it in (m.get("items") or []):
                lines.append(f"  - {it}")

    lines.append(f"\n🤖 Уверенность AI: {conf:.2f}")

    await msg.reply_text("\n".join(lines))
