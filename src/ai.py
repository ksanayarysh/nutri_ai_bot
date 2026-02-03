import base64
import json
from openai import OpenAI

from src.config import OPENAI_API_KEY, OPENAI_MODEL
from src.food_structure.food import Macros as AiItem

client = OpenAI(api_key=OPENAI_API_KEY)


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

# NOTE:
# Limits are used only for *sanity clamping* (extreme outliers), NOT for scaling qty.
# Automatic scaling by qty caused double-counting and wrong totals (e.g., cucumber/egg).
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
    """Clamp only extreme outliers. Does NOT scale by qty."""
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

    # carbs must be >= fiber (if both known)
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


def maybe_flag_per_unit(items: list[AiItem]) -> list[str]:
    """Heuristic warnings only. No mutation, no scaling.

    If the model accidentally returned per-1pcs values while qty>1, we cannot know for sure.
    We only add a warning when totals look *too low* compared to a very rough minimum.
    """
    notes: list[str] = []
    for it in items:
        if (it.unit or "").lower() != "pcs":
            continue
        if it.qty is None or it.qty <= 1:
            continue

        cat = detect_food_category(it.name)
        limits = CATEGORY_LIMITS.get(cat) or {}
        if not limits:
            continue

        def maybe_warn(field: str):
            v = getattr(it, field)
            if v is None or field not in limits:
                return
            lo, _hi = limits[field]
            # if total is suspiciously below an extremely rough minimum -> likely per-unit/underestimated
            try:
                vv = float(v)
            except Exception:
                return
            if vv < (float(lo) * float(it.qty) * 0.75):
                notes.append(
                    f"{it.name}: {field}={vv} при qty={it.qty} выглядит заниженным; возможно ai вернул значение 'за 1 шт'"
                )

        maybe_warn("calories")
        maybe_warn("protein")
        maybe_warn("fat")

    return notes


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
        - All item names MUST be in the language which the user used to ask.
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
        "prompt_version": "ai_mode_a_ru_week_v5_no_qty_autoscale",
        "model": OPENAI_MODEL,
    }

    # Fix suspicious "fiber=0" for likely plant foods: set fiber unknown instead of lying.
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

    # Warn (do not mutate) if totals look suspiciously low for qty>1 pcs.
    per_unit_warnings = maybe_flag_per_unit(items)
    if per_unit_warnings:
        meta["assumptions"].extend(per_unit_warnings)

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
        "prompt_version": "ai_mode_photo_v2_no_qty_autoscale",
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

    per_unit_warnings = maybe_flag_per_unit(items)
    if per_unit_warnings:
        meta["assumptions"].extend(per_unit_warnings)

    if sanity_notes:
        meta["assumptions"].extend(sanity_notes)

    return items, meta["confidence"], meta

def ai_daily_analysis_ru(*, profile_hint: dict, day: str, totals: dict, items: list[dict]) -> dict:
    """
    totals: {"calories":..., "protein":..., "fat":..., "carbs":..., "fiber":..., "net_carbs":...}
    items: [{"meal":..., "name":..., "qty":..., "unit":..., "calories":..., "protein":..., "fat":..., "carbs":..., "fiber":...}, ...]
    """
    prompt = f"""
Ты — нутри-ассистент и ведёшь дневник питания конкретного пользователя.
Твоя задача: дать персональные, короткие и практичные рекомендации по итогам дня.

Важно:
- Используй данные из профиля, целей (targets) и списка еды. Ничего не выдумывай.
- Если в totals есть ключ "targets", сравни фактические итоги с целями:
  * укажи 1–2 главных отклонения (недобор/перебор) как приоритет.
  * советы должны прямо закрывать эти отклонения.
- Пиши по-русски, дружелюбно, без лекций и без медицинских диагнозов.
- Добавляй «картинки» в тексте: используй эмодзи в начале headline и пунктов (✅🛠📌⚠️🍽️🎯).
- Советы должны быть конкретными и выполнимыми завтра.

Профиль (может быть неполный):
{json.dumps(profile_hint, ensure_ascii=False)}

День: {day}

Съедено (список):
{json.dumps(items, ensure_ascii=False)}

Итоги (включая targets, если есть):
{json.dumps(totals, ensure_ascii=False)}

Требования к ответу:
- Верни JSON строго по схеме.
- headline: 1 короткая строка с главным выводом + эмодзи.
- good/improve/plan/warnings: короткие пункты (до ~12 слов), каждый с эмодзи в начале.
- Если данных мало или цели не заданы — скажи это и дай 1–2 универсальных совета.
""".strip()

    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_schema", "json_schema": _analysis_json_schema_ru()},
        temperature=0.4,
    )
    return json.loads(resp.choices[0].message.content)

def _case_plan_json_schema_ru():
    return {
        "name": "case_plan_ru",
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "extracted": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "sex": {"type": "string", "enum": ["male", "female", "unknown"]},
                        "age": {"type": ["number", "null"]},
                        "height_cm": {"type": ["number", "null"]},
                        "weight_kg": {"type": ["number", "null"]},
                        "activity": {"type": "string"},
                        "goal": {"type": "string"},
                        "preferences": {"type": "array", "items": {"type": "string"}},
                        "restrictions": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["sex", "age", "height_cm", "weight_kg", "activity", "goal", "preferences", "restrictions"],
                },
                "calculations": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "bmi": {"type": ["number", "null"]},
                        "bmr": {"type": ["number", "null"]},
                        "ka": {"type": ["number", "null"]},
                        "tdee": {"type": ["number", "null"]},
                        "target_kcal": {"type": ["number", "null"]},
                        "protein_g": {"type": ["number", "null"]},
                        "fat_g": {"type": ["number", "null"]},
                        "carbs_g": {"type": ["number", "null"]},
                        "notes": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["bmi", "bmr", "ka", "tdee", "target_kcal", "protein_g", "fat_g", "carbs_g", "notes"],
                },
                "menu_3days": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "day": {"type": "number"},
                            "meals": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {
                                        "name": {"type": "string"},
                                        "items": {"type": "array", "items": {"type": "string"}},
                                    },
                                    "required": ["name", "items"],
                                },
                            },
                        },
                        "required": ["day", "meals"],
                    },
                },
                "confidence": {"type": "number"},
            },
            "required": ["extracted", "calculations", "menu_3days", "confidence"],
        },
    }


def ai_case_plan_ru(*, profile_hint: dict, case_text: str) -> dict:
    prompt = f"""
Ты — нутри-ассистент. По описанию кейса нужно:
1) Извлечь: пол, возраст, рост, вес, активность, цель, предпочтения и ограничения.
2) Рассчитать:
   - ИМТ = вес(кг) / (рост(м)^2)
   - ВОО (BMR) по Mifflin–St Jeor:
       муж: 10*вес + 6.25*рост - 5*возраст + 5
       жен: 10*вес + 6.25*рост - 5*возраст - 161
   - КА: сидячий=1.2, лёгкая=1.375, умеренная=1.55, высокая=1.725
   - СПК (TDEE) = BMR * КА
   - Целевая калорийность: для "снижения жира" возьми дефицит 15% (TDEE*0.85), но не ниже 1000 ккал для мужчин, если нет иных данных.
   - БЖУ: белок 1.8 г/кг; жир 0.9 г/кг; углеводы = остаток по калориям.
3) Составить рацион на 3 дня (завтрак/обед/ужин/перекус), учитывая предпочтения и ограничения.
Важно:
- Ничего не выдумывай: если данных нет, ставь null и добавляй пояснение в notes.
- Пиши пункты меню конкретно: продукты + примерные порции (в граммах/шт), без "магии".
- Пиши так же калорийность блюд и процент от рекомендованной суточной калорийности
- напиши подробно расчет ВОО, это обязательно, распиши подробно
- Ответ строго JSON по схеме.

Профиль (может быть неполный, используй только если релевантно):
{json.dumps(profile_hint, ensure_ascii=False)}

Кейс:
{case_text}
""".strip()

    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_schema", "json_schema": _case_plan_json_schema_ru()},
        temperature=0.3,
    )
    return json.loads(resp.choices[0].message.content)
