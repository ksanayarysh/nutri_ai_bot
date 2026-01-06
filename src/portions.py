from __future__ import annotations
from typing import Optional, Tuple

def suggest_portion(name: str) -> Optional[Tuple[float, str]]:
    n = (name or "").lower()

    if "яйц" in n:
        return 2, "pcs"
    if "куриц" in n or "котлет" in n or "мяс" in n:
        return 150, "g"
    if "сыр" in n:
        return 30, "g"
    if "йогурт" in n:
        return 150, "g"
    if "орех" in n:
        return 20, "g"

    return None
