from __future__ import annotations
from typing import Tuple, List
from src.config import MEAL_ALIASES

def apply_aliases_to_text(uid: int, text: str) -> Tuple[str, List[str]]:
    notes = []
    out = text
    for src, dst in MEAL_ALIASES.items():
        if src in out:
            out = out.replace(src, dst)
            notes.append(f"{src} -> {dst}")
    return out, notes
