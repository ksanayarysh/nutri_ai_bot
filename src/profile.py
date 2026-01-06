from __future__ import annotations

def build_profile_hint(u: dict) -> dict:
    return {
        "language": u.get("language") or "ru",
        "goal": u.get("goal"),
        "tz": u.get("tz"),
    }
