from .locales.ru import RU
from .locales.pt_br import PT_BR

MAP = {"ru": RU, "pt_BR": PT_BR}

def t(key: str, lang: str = "ru", **kwargs) -> str:
    d = MAP.get(lang, RU)
    text = d.get(key) or RU.get(key) or key
    return text.format(**kwargs)
