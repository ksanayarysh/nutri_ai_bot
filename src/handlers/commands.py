# src/handlers/commands.py
from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from src.ai import ai_estimate
from src.bot import meal_to_ru
from src.db import ensure_user
from src.profile import build_profile_hint
from src.subscriptions import ensure_trial_subscription
from src.config import PRICE_TEXT

from datetime import datetime, timedelta
from src.config import TZ
from src.db import db, today_str
from src.subscriptions import is_subscribed

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
    ensure_trial_subscription(user.id)

    await update.message.reply_text(
        "Привет! 👋\n\n"
        "Я нутри-бот — помогаю вести дневник питания, "
        "считать КБЖУ и видеть закономерности.\n\n"
        "Просто напиши, что ты съела, например:\n"
        "`завтрак: яйца и сыр`\n"
        "или пришли фото еды 📸\n\n"
        "Команды:\n"
        "/today — итоги дня\n"
        "/week — итоги недели\n"
        "/pay — подписка\n",
        parse_mode="Markdown",
    )


# -------------------------
# /help
# -------------------------

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    await update.message.reply_text(
        "ℹ️ Как пользоваться ботом:\n\n"
        "• Пиши, что съела: `обед: курица и салат`\n"
        "• Или присылай фото еды 📸\n\n"
        "Я записываю всё в дневник и считаю КБЖУ.\n\n"
        "Команды:\n"
        "/today — итоги дня\n"
        "/week — итоги недели\n"
        f"/pay — подписка ({PRICE_TEXT})\n",
        parse_mode="Markdown",
    )


# -------------------------
# /today
# -------------------------

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return

    if not is_subscribed(user.id):
        await update.message.reply_text(
            "⏳ Эта функция доступна по подписке.\n"
            "Используй /pay"
        )
        return

    day = today_str()

    with db() as conn:
        cur = conn.cursor()

        # 1️⃣ Полный список еды за день
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

        # 2️⃣ Итоги
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

    if not rows:
        await update.message.reply_text("📭 За сегодня пока ничего не записано.")
        return

    # Группируем по приёму пищи
    meals: dict[str, list[str]] = {}
    i = 1
    for meal, name, qty, unit, cal, p, f, c, fi in rows:
        meals.setdefault(meal or "закуска", []).append(
            f"•{i} {name} — {qty:g} {UNIT_LABELS[unit]} "
            f"({cal:.0f} ккал, Б {p:.1f}, Ж {f:.1f}, У {c:.1f})"
        )
        i += 1

    lines = ["📋 Сегодня ты съела:"]
    for meal, items in meals.items():
        lines.append(f"\n🍽 {meal_to_ru(meal)}")
        lines.extend(items)

    net_carbs = max(carbs - fiber, 0)

    lines.append(
        "\n📊 Итоги за сегодня:\n"
        f"Ккал: {calories:.0f}\n"
        f"Белки: {protein:.1f} г\n"
        f"Жиры: {fat:.1f} г\n"
        f"Углеводы: {carbs:.1f} г\n"
        f"Чистые углеводы: {net_carbs:.1f} г"
    )

    await update.message.reply_text("\n".join(lines))


# -------------------------
# /week
# -------------------------

async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return

    if not is_subscribed(user.id):
        await update.message.reply_text(
            "⏳ Эта функция доступна по подписке.\n"
            "Используй /pay"
        )
        return

    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
              COUNT(DISTINCT entry_date),
              COALESCE(SUM(calories), 0),
              COALESCE(SUM(protein), 0),
              COALESCE(SUM(fat), 0),
              COALESCE(SUM(carbs), 0),
              COALESCE(SUM(fiber), 0)
            FROM entries
            WHERE user_id = %s
              AND entry_date::date >= CURRENT_DATE - INTERVAL '7 days'
            """,
            (user.id,),
        )
        days, calories, protein, fat, carbs, net_carbs = cur.fetchone()

    await update.message.reply_text(
        f"📈 Итоги за 7 дней:\n\n"
        f"Дней с логами: {days}\n\n"
        f"Ккал: {calories:.0f}, среднее {calories / days:.1f}\n"
        f"Белки: {protein:.1f}г, среднее {protein / days:.1f}г\n"
        f"Жиры: {fat:.1f}г, среднее {fat / days:.1f}г\n"
        f"Углеводы: {carbs:.1f}г, среднее {carbs / days:.1f}г\n"
        f"Чистые углеводы: {net_carbs:.1f}г, среднее {net_carbs / days:.1f}"
    )

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

    status = "активна ✅" if is_subscribed(user.id) else "не активна ⏳"

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
    if not user or not update.message:
        return

    if not context.args or len(context.args) < 5:
        await update.message.reply_text(
            "Формат:\n"
            "`/set_targets kcal protein fat carbs net_carbs [mode]`\n"
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
        await update.message.reply_text("Числа в целях должны быть числами. Да, это звучит очевидно.")
        return

    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO targets (user_id, calories, protein, fat, carbs, net_carbs, mode, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
              calories = EXCLUDED.calories,
              protein = EXCLUDED.protein,
              fat = EXCLUDED.fat,
              carbs = EXCLUDED.carbs,
              net_carbs = EXCLUDED.net_carbs,
              mode = EXCLUDED.mode,
              updated_at = EXCLUDED.updated_at
            """,
            (user.id, kcal, protein, fat, carbs, net_carbs, mode, datetime.now(TZ).isoformat(timespec="seconds")),
        )

    await update.message.reply_text("Цели сохранены ✅")


async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return

    if not is_subscribed(user.id):
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

    await msg.reply_text(f"🗑 Удалила запись: {name}")

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
        profile_hint = build_profile_hint({"user_id": user.id})
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

    text = f"✅ Обновила запись #{idx}: {it.name} — {it.qty:g} {it.unit} ({(it.calories or 0):.0f} ккал)"
    if warn_multi:
        text += "\n⚠️ AI распознал несколько продуктов, я обновила только первый."
    await msg.reply_text(text)
