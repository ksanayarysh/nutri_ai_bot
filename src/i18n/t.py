from .locales.ru import RU
from .locales.pt_br import PT_BR

# We store 'pt' in DB and use it in code.
MAP = {"ru": RU, "pt": PT_BR}

def t(key: str, lang: str = "ru", **kwargs) -> str:
    d = MAP.get(lang, RU)
    text = d.get(key) or RU.get(key) or key
    try:
        return text.format(**kwargs)
    except Exception:
        return text
