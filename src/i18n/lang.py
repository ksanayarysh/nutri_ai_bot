from typing import Optional

from src.db import db

SUPPORTED_LANGS = {"ru", "pt"}  # можно расширить потом

def _normalize_lang(raw: Optional[str]) -> str:
    if not raw:
        return "ru"
    raw = raw.lower().strip()
    if raw.startswith("pt"):
        return "pt"
    if raw.startswith("ru"):
        return "ru"
    return "ru"

def get_user_language(user_id: int) -> str:
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT language FROM users WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
    return _normalize_lang(row[0] if row else None)

def set_user_language(user_id: int, lang: str) -> None:
    lang = _normalize_lang(lang)
    if lang not in SUPPORTED_LANGS:
        lang = "ru"
    with db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users SET language = %s WHERE user_id = %s", (lang, user_id))
        conn.commit()


